import os
import signal
import subprocess
import sys
import time
from contextlib import suppress

import pytest

from core.workspace_lease import SessionLease, SessionOwnedError, _runtime_alive


def test_same_session_is_exclusive_but_distinct_sessions_are_independent(tmp_path):
    first = SessionLease("exact", directory=tmp_path)
    other = SessionLease("different", directory=tmp_path)
    try:
        with pytest.raises(SessionOwnedError):
            SessionLease("exact", directory=tmp_path)
    finally:
        first.release()
        other.release()
    again = SessionLease("exact", directory=tmp_path)
    again.release()


def test_live_child_keeps_session_unavailable_after_owner_releases(tmp_path):
    lease = SessionLease("exact", directory=tmp_path)
    lease.launching()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        lease.bind(child.pid)
        assert "process_group" not in lease.record
        lease.release()
        with pytest.raises(SessionOwnedError, match="may still be running"):
            SessionLease("exact", directory=tmp_path)
    finally:
        child.terminate()
        child.wait(timeout=5)
    recovered = SessionLease("exact", directory=tmp_path)
    recovered.release()


def test_unknown_launch_outcome_never_authorizes_another_writer(tmp_path):
    lease = SessionLease("exact", directory=tmp_path)
    lease.launching()
    with pytest.raises(SessionOwnedError):
        lease.cancel_before_launch()
    lease.release()
    with pytest.raises(SessionOwnedError, match="may still be running"):
        SessionLease("exact", directory=tmp_path)


def test_transfer_keeps_exact_runtime_and_target_exclusive(tmp_path, monkeypatch):
    source = SessionLease("source", directory=tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=os.name != "nt")
    target = None
    try:
        source.launching()
        source.bind(child.pid)
        before = dict(source.record)
        save = source._save
        checked = []
        def verify_both_locks_before_retirement():
            for sid in ("source", "target"):
                with pytest.raises(SessionOwnedError):
                    SessionLease(sid, directory=tmp_path)
            checked.append(True)
            save()
        monkeypatch.setattr(source, "_save", verify_both_locks_before_retirement)
        target = source.transfer_after_transition("target")
        assert checked == [True]
        assert source.closed and child.poll() is None
        assert target.record["child"] == before["child"]
        assert target.record.get("process_group") == before.get("process_group")
        reopened = SessionLease("source", directory=tmp_path)
        reopened.release()
        with pytest.raises(SessionOwnedError):
            SessionLease("target", directory=tmp_path)
        target.release()
        with pytest.raises(SessionOwnedError, match="may still be running"):
            SessionLease("target", directory=tmp_path)
    finally:
        child.terminate()
        child.wait(timeout=5)
        source.release()
        if target:
            target.release()
    recovered = SessionLease("target", directory=tmp_path)
    recovered.release()


@pytest.mark.parametrize("failure", ["occupied", "same", "source-write"])
def test_transfer_failure_never_releases_source_runtime(tmp_path, monkeypatch, failure):
    source = SessionLease("source", directory=tmp_path)
    occupied = SessionLease("target", directory=tmp_path) if failure == "occupied" else None
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        source.launching()
        source.bind(child.pid)
        before = dict(source.record)
        if failure == "source-write":
            def broken_save():
                raise OSError("disk unavailable")
            monkeypatch.setattr(source, "_save", broken_save)
        with pytest.raises((SessionOwnedError, OSError)):
            source.transfer_after_transition("source" if failure == "same" else "target")
        assert not source.closed and source.record == before and child.poll() is None
        with pytest.raises(SessionOwnedError):
            SessionLease("source", directory=tmp_path)
        if failure == "source-write":
            with pytest.raises(SessionOwnedError, match="may still be running"):
                SessionLease("target", directory=tmp_path)
    finally:
        monkeypatch.undo()
        child.terminate()
        child.wait(timeout=5)
        source.release()
        if occupied:
            occupied.release()


def test_transfer_refuses_unbound_or_closed_lease(tmp_path):
    source = SessionLease("source", directory=tmp_path)
    before = set(tmp_path.iterdir())
    with pytest.raises(SessionOwnedError, match="bound"):
        source.transfer_after_transition("target")
    source.release()
    with pytest.raises(SessionOwnedError, match="bound"):
        source.transfer_after_transition("target")
    assert set(tmp_path.iterdir()) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX process group containment")
def test_exited_leader_does_not_release_surviving_tool_group(tmp_path):
    code = """
import subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(child.pid, flush=True)
sys.stdin.readline()
"""
    leader = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True, start_new_session=True)
    lease = SessionLease("exact", directory=tmp_path)
    try:
        assert int(leader.stdout.readline()) > 0
        lease.launching()
        lease.bind(leader.pid)
        assert lease.record["process_group"] == leader.pid
        leader.stdin.close()
        leader.wait(timeout=5)
        lease.release()
        with pytest.raises(SessionOwnedError, match="may still be running"):
            SessionLease("exact", directory=tmp_path)
    finally:
        with suppress(ProcessLookupError):
            os.killpg(leader.pid, signal.SIGKILL)
        leader.wait(timeout=5)
        leader.stdout.close()
        lease.release()
    deadline = time.monotonic() + 5
    while _runtime_alive(lease.record) and time.monotonic() < deadline:
        time.sleep(0.01)
    recovered = SessionLease("exact", directory=tmp_path)
    recovered.release()


def test_vanished_child_keeps_launch_ambiguous(tmp_path, monkeypatch):
    import psutil
    def vanished(pid):
        raise psutil.NoSuchProcess(pid)
    lease = SessionLease("exact", directory=tmp_path)
    lease.launching()
    monkeypatch.setattr("core.workspace_lease._identity", vanished)
    with pytest.raises(SessionOwnedError, match="ownership"):
        lease.bind(123)
    lease.release()
    with pytest.raises(SessionOwnedError, match="may still be running"):
        SessionLease("exact", directory=tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process group inspection")
def test_group_inspection_failure_never_proves_exit(monkeypatch):
    def denied(pid):
        raise PermissionError("Cannot inspect process")
    monkeypatch.setattr("core.workspace_lease.psutil.pids", lambda: [123])
    monkeypatch.setattr("core.workspace_lease.os.getpgid", denied)
    assert _runtime_alive({"process_group": 456})
    assert _runtime_alive({"process_group": -1})
    assert _runtime_alive({"process_group": "invalid"})


def test_lock_is_enforced_across_real_processes(tmp_path):
    lease = SessionLease("exact", directory=tmp_path)
    code = """
import sys
from pathlib import Path
from core.workspace_lease import SessionLease, SessionOwnedError
try:
    lease = SessionLease('exact', directory=Path(sys.argv[1]))
except SessionOwnedError:
    sys.exit(0)
lease.release()
sys.exit(9)
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path)],
            env=dict(os.environ),
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr.decode()
    finally:
        lease.release()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable preflight")
def test_missing_terminal_command_does_not_poison_session_lease(tmp_path, monkeypatch):
    from ui import pty_terminal

    monkeypatch.setenv("SERENA_RUNTIME_LEASE_DIR", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        pty_terminal.spawn(
            [str(tmp_path / "missing-command")], cwd=str(tmp_path), session_id="retryable"
        )
    lease = SessionLease("retryable")
    lease.release()


@pytest.mark.skipif(
    os.name == "nt", reason="Real POSIX PTY proof; Windows lock path has separate CI requirement"
)
def test_pty_and_custom_session_share_exclusive_ownership(tmp_path, monkeypatch):
    from ui import pty_terminal

    monkeypatch.setenv("SERENA_RUNTIME_LEASE_DIR", str(tmp_path))
    monkeypatch.setattr(pty_terminal, "_systemd_scope_supported", lambda: False)
    owner = SessionLease("held")
    try:
        with pytest.raises(SessionOwnedError):
            pty_terminal.spawn(["/bin/sleep", "10"], cwd=str(tmp_path), session_id="held")
    finally:
        owner.release()
    tid = pty_terminal.spawn(["/bin/sleep", "10"], cwd=str(tmp_path), session_id="held")
    try:
        with pytest.raises(SessionOwnedError):
            SessionLease("held")
        pty_terminal.register_session("held", tid)
        pid = pty_terminal.get(tid).proc.pid
        assert pty_terminal.migrate_session("held", "durable", tid)
        assert pty_terminal.get(tid).proc.pid == pid
        with pytest.raises(SessionOwnedError):
            SessionLease("durable")
    finally:
        pty_terminal.kill(tid)
    lease = SessionLease("durable")
    lease.release()
