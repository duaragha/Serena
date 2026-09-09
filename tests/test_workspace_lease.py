import os
import subprocess
import sys

import pytest

from core.workspace_lease import SessionLease, SessionOwnedError


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
