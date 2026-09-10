"""Real Win32 tests with gated Python children, never provider sessions."""

import os
import subprocess
import sys
import time

import psutil
import pytest

from core.workspace_windows_job import WindowsJob


def test_partial_suspension_is_rolled_back(monkeypatch):
    job = object.__new__(WindowsJob)
    job._suspended = []
    processes = {}
    class Process:
        def __init__(self, pid):
            self.pid = pid
            self.paused = False
            processes[pid] = self
        def is_running(self):
            return True
        def status(self):
            return psutil.STATUS_RUNNING
        def suspend(self):
            if self.pid == 2:
                raise psutil.AccessDenied(self.pid)
            self.paused = True
        def resume(self):
            self.paused = False
    monkeypatch.setattr(psutil, "Process", Process)
    monkeypatch.setattr(job, "process_ids", lambda: {1, 2})
    monkeypatch.setattr(job, "_contains", lambda pid: True)
    with pytest.raises(psutil.AccessDenied):
        job.suspend()
    assert not job.suspended and not processes[1].paused


def test_non_windows_refuses():
    if os.name == "nt":
        pytest.skip("Windows has job objects")
    with pytest.raises(OSError, match="require Windows"):
        WindowsJob()


@pytest.mark.skipif(os.name != "nt", reason="Real Windows kernel required")
@pytest.mark.parametrize("operation", ["terminate", "close", "suspend"])
def test_job_owns_descendant_after_leader_exit(tmp_path, operation):
    marker = tmp_path / "child.pid"
    peer = """
import subprocess,sys
from pathlib import Path
if sys.stdin.readline().strip() != 'start':
    sys.exit(2)
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
Path(sys.argv[1]).write_text(str(child.pid))
"""
    job = WindowsJob()
    # Bypass the venv redirector, which could spawn before the gate is assigned.
    executable = getattr(sys, "_base_executable", sys.executable)
    leader = subprocess.Popen([executable, "-c", peer, str(marker)], stdin=subprocess.PIPE)
    child = None
    try:
        job.assign(leader.pid)
        assert job.active_processes() == 1
        assert not marker.exists()
        leader.communicate(b"start\n", timeout=10)
        assert leader.returncode == 0
        child = psutil.Process(int(marker.read_text()))
        assert child.is_running()
        assert job.active_processes() == 1
        if operation == "suspend":
            assert job.process_ids() == {child.pid}
            assert job.suspend() and job.suspended
            assert child.status() == psutil.STATUS_STOPPED
            assert psutil.Process(os.getpid()).status() != psutil.STATUS_STOPPED
            assert job.suspend()  # No additional suspension count on repeat.
            job.resume()
            assert not job.suspended and child.status() != psutil.STATUS_STOPPED
            job.terminate()
        else:
            getattr(job, operation)()
        child.wait(timeout=10)
        if operation == "terminate":
            deadline = time.monotonic() + 5
            while job.active_processes() and time.monotonic() < deadline:
                time.sleep(.01)
            assert job.active_processes() == 0
    finally:
        job.close()
        if leader.poll() is None:
            leader.kill()
        leader.wait(timeout=10)
        if child is not None and child.is_running():
            child.kill()
            child.wait(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="Real Windows kernel required")
def test_closed_and_invalid_assignment_fail():
    job = WindowsJob()
    try:
        for pid in (0, -1, True, os.getpid()):
            with pytest.raises(ValueError):
                job.assign(pid)
        assert job.active_processes() == 0
    finally:
        job.close()
    job.close()
    with pytest.raises(OSError, match="closed"):
        job.active_processes()
    with pytest.raises(OSError, match="closed"):
        job.assign(123)
