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
