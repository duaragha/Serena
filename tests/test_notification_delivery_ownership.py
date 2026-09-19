"""Private notification DBs and fake transports; never send an actual notice."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import gc
import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from core.control_plane import ControlPlaneStore
from core.notification_authority import NotificationAuthority, NotificationPolicy, NotificationRequest


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
    repeat = authority.request(request)
    assert repeat.decision == "suppressed"
    assert len(sent) == 1
