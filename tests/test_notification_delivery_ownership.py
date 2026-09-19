"""Private notification DBs and fake transports; never send an actual notice."""

import gc
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest

from core.control_plane import ControlPlaneStore
from core.notification_authority import (
    NotificationAuthority,
    NotificationPolicy,
    NotificationRequest,
)


def _queued(tmp_path):
    hour = datetime.now().hour
    authority = NotificationAuthority(tmp_path / "notices.sqlite3",
        control_store=ControlPlaneStore(tmp_path / "control.sqlite3"),
        policy=NotificationPolicy(quiet_start_hour=hour, quiet_end_hour=(hour + 1) % 24))
    notice = authority.request(NotificationRequest(kind="test.private", summary="private race probe",
                                                   dedupe_key="one-obligation"))
    assert notice.decision == "deferred"
    authority.policy = NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0)
    with authority._connect() as db:
        db.execute("UPDATE notifications SET deliver_after=0")
    return authority, notice.notification_id


def test_concurrent_thread_cannot_send_an_owned_notice(tmp_path):
    authority, notice_id = _queued(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def sender(request):
        calls.append(request)
        entered.set()
        assert release.wait(5)
        return True
    authority._senders = {"voice": sender}
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(authority.redeliver, notice_id)
        try:
            assert entered.wait(5)
            duplicate = pool.submit(authority.redeliver, notice_id).result(timeout=3)
            assert duplicate.decision == "deferred"
            assert duplicate.reason == "delivery already owned"
        finally:
            release.set()
        assert first.result(timeout=5).sent
    assert len(calls) == 1
    assert authority.redeliver(notice_id) is None


def test_other_process_cannot_send_an_owned_notice(tmp_path):
    authority, notice_id = _queued(tmp_path)
    ready, release = tmp_path / "ready", tmp_path / "release"
    source = (
        "import time; from pathlib import Path\n"
        "from core.notification_authority import NotificationAuthority, NotificationPolicy\n"
        "from core.control_plane import ControlPlaneStore\n"
        f"ready=Path({str(ready)!r}); release=Path({str(release)!r})\n"
        "def sender(request):\n"
        " ready.touch(); deadline=time.monotonic()+10\n"
        " while not release.exists() and time.monotonic()<deadline: time.sleep(.02)\n"
        " assert release.exists(); return True\n"
        f"a=NotificationAuthority(Path({str(authority.path)!r}), "
        f"control_store=ControlPlaneStore(Path({str(tmp_path / 'control.sqlite3')!r})), "
        "policy=NotificationPolicy(quiet_start_hour=0,quiet_end_hour=0),senders={'voice':sender})\n"
        f"assert a.redeliver({notice_id!r}).sent\n"
    )
    child = subprocess.Popen([sys.executable, "-c", source], cwd=Path(__file__).resolve().parents[1],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    calls = []
    authority._senders = {"voice": lambda request: calls.append(request) or True}
    try:
        deadline = time.monotonic() + 8
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists()
        assert authority.redeliver(notice_id).decision == "deferred"
        assert calls == []
        release.touch()
        output, errors = child.communicate(timeout=5)
        assert child.returncode == 0, errors
        assert authority.redeliver(notice_id) is None
    finally:
        release.touch()
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_notification_connection_is_closed_after_scope(tmp_path):
    authority, _ = _queued(tmp_path)
    with authority._connect() as connection:
        connection.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_owned_body_timeout_is_not_misreported_as_lock_contention(tmp_path, monkeypatch):
    authority, notice_id = _queued(tmp_path)
    def fail(*args, **kwargs):
        raise TimeoutError("receipt storage timeout")
    monkeypatch.setattr(authority, "_deliver_owned", fail)
    with pytest.raises(TimeoutError, match="receipt storage timeout"):
        authority.redeliver(notice_id)


@pytest.mark.skipif(not os.path.isdir('/proc/self/fd'), reason='Linux descriptor accounting')
def test_notification_reads_do_not_retain_descriptors_until_gc(tmp_path):
    authority, _ = _queued(tmp_path)
    gc.collect()
    enabled = gc.isenabled()
    retained = []
    baseline = len(os.listdir('/proc/self/fd'))
    gc.disable()
    try:
        for _ in range(30):
            with authority._connect() as db:
                db.execute('SELECT 1').fetchone()
            retained.append(db)
        assert len(os.listdir('/proc/self/fd')) <= baseline
    finally:
        if enabled:
            gc.enable()


# ---- answering him is not interrupting him -------------------------------


def _authority_in_quiet_hours(tmp_path, sent):
    """An authority whose quiet hours are open right now, with a live sender."""

    hour = datetime.now().hour
    return NotificationAuthority(
        tmp_path / "quiet.sqlite3",
        control_store=ControlPlaneStore(tmp_path / "quiet-control.sqlite3"),
        policy=NotificationPolicy(quiet_start_hour=hour, quiet_end_hour=(hour + 1) % 24),
        senders={"imessage": lambda request: sent.append(request.summary) or True},
    )


def test_quiet_hours_still_hold_an_unprompted_notice(tmp_path):
    sent = []
    authority = _authority_in_quiet_hours(tmp_path, sent)

    result = authority.request(NotificationRequest(
        kind="task.update", summary="#9 is still stuck and i can't move it",
        channel="imessage", dedupe_key="nudge:9:blocked"))

    assert result.decision == "deferred"
    assert result.reason == "quiet hours"
    assert sent == []


def test_an_answer_to_something_he_asked_for_is_not_held(tmp_path):
    """#1054 failed at 23:56 and the notice was parked until 08:00.

    He was awake, on that line, asking about it. Holding the answer until
    morning is indistinguishable from the bot being broken.
    """

    sent = []
    authority = _authority_in_quiet_hours(tmp_path, sent)

    result = authority.request(NotificationRequest(
        kind="task.update", summary="#1054 failed: test gate failed after integration",
        channel="imessage", dedupe_key="task:1054:blocked", answers_request=True))

    assert result.decision == "sent", result.reason
    assert sent == ["#1054 failed: test gate failed after integration"]


def test_answering_is_not_a_general_escape(tmp_path):
    """It skips quiet hours only. Dedupe still applies, so it cannot spam."""

    sent = []
    authority = _authority_in_quiet_hours(tmp_path, sent)
    request = NotificationRequest(
        kind="task.update", summary="#1054 failed: test gate failed after integration",
        channel="imessage", dedupe_key="task:1054:blocked", answers_request=True)

    assert authority.request(request).decision == "sent"
    assert authority.request(request).decision == "suppressed"
    assert len(sent) == 1


# ---- the notice goes out through this install's CLI, not another one -------


def test_the_chats_binary_prefers_this_installs_entry_point(tmp_path, monkeypatch):
    """The runtime sent its notices through a different Serena's CLI.

    _chats_binary looked for a sibling named exactly "chats". Windows entry
    points are chats.exe, so on the PC the sibling never matched, PATH won, and
    the deployed runtime shelled out to a user-site chats running the synced
    dev tree instead of its own code.
    """

    from core import notification_senders as senders

    scripts = tmp_path / "runtime" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("", encoding="utf-8")
    mine = scripts / "chats.exe"
    mine.write_text("", encoding="utf-8")
    other = tmp_path / "elsewhere" / "chats.exe"
    other.parent.mkdir()
    other.write_text("", encoding="utf-8")

    monkeypatch.setattr(senders, "_BINARY_SUFFIXES", ("", ".exe"))
    monkeypatch.setattr(senders.sys, "executable", str(scripts / "python.exe"))
    monkeypatch.setattr(senders.shutil, "which", lambda _name: str(other))

    assert senders._chats_binary() == str(mine)


def test_path_is_still_used_when_this_install_has_no_cli(tmp_path, monkeypatch):
    from core import notification_senders as senders

    scripts = tmp_path / "bare"
    scripts.mkdir()
    (scripts / "python").write_text("", encoding="utf-8")
    fallback = tmp_path / "onpath" / "chats"
    fallback.parent.mkdir()
    fallback.write_text("", encoding="utf-8")

    monkeypatch.setattr(senders, "_BINARY_SUFFIXES", ("",))
    monkeypatch.setattr(senders.sys, "executable", str(scripts / "python"))
    monkeypatch.setattr(senders, "HOME", tmp_path / "nohome")
    monkeypatch.setattr(senders.shutil, "which", lambda _name: str(fallback))

    assert senders._chats_binary() == str(fallback)


def test_a_missing_cli_is_reported_as_no_binary(tmp_path, monkeypatch):
    """Returning a path that is not there sends the notice nowhere, silently."""

    from core import notification_senders as senders

    monkeypatch.setattr(senders, "_BINARY_SUFFIXES", ("",))
    monkeypatch.setattr(senders.sys, "executable", str(tmp_path / "gone" / "python"))
    monkeypatch.setattr(senders, "HOME", tmp_path / "nohome")
    monkeypatch.setattr(senders.shutil, "which", lambda _name: str(tmp_path / "ghost"))

    assert senders._chats_binary() is None


# ---- a notice must not crash on a machine with no unix sockets ------------


def test_no_unix_sockets_is_not_a_crash(monkeypatch, tmp_path):
    """Windows has no AF_UNIX, and asking for one raises AttributeError.

    The senders caught OSError, so on the PC -- the machine that actually runs
    Fleet -- a terminal run notice raised "module 'socket' has no attribute
    'AF_UNIX'" straight out of the code whose whole job is telling him what
    happened. It showed up in a real run's event log.
    """

    from core import notification_senders as senders

    target = tmp_path / "brain-events.sock"
    target.write_text("", encoding="utf-8")
    monkeypatch.setattr(senders, "UNIX_DATAGRAMS_AVAILABLE", False)

    assert senders.overlay_datagram({"type": "fleet_notice"}, target) is False


def test_a_missing_listener_is_not_a_crash(tmp_path):
    from core import notification_senders as senders

    assert senders.overlay_datagram({"x": 1}, tmp_path / "absent.sock") is False


def test_an_oversized_payload_is_refused_not_raised(monkeypatch, tmp_path):
    from core import notification_senders as senders

    target = tmp_path / "brain-events.sock"
    target.write_text("", encoding="utf-8")
    monkeypatch.setattr(senders, "UNIX_DATAGRAMS_AVAILABLE", True)

    assert senders.overlay_datagram({"text": "x" * 70_000}, target) is False


def test_fleet_and_the_work_supervisor_share_this_one_sender():
    """Three copies of the same unguarded socket is how two of them stayed broken."""

    import inspect

    from core import voice_work_supervisor
    from fleet import supervisor

    for module, name in ((supervisor, "_send_spoken_notice"),
                         (voice_work_supervisor, "_send_overlay_event")):
        source = inspect.getsource(getattr(module, name))
        assert "overlay_datagram" in source, name
        assert "AF_UNIX" not in source, name


def test_fleet_texts_him_through_the_shared_binary_resolver():
    import inspect

    from fleet import supervisor

    source = inspect.getsource(supervisor._send_raghav_text)
    assert "_chats_binary" in source
    assert "shutil.which" not in source
