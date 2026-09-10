"""Real Win32 tests with gated Python children, never provider sessions."""

import os
import subprocess
import sys
import time

import psutil
import pytest

from core.workspace_windows_job import WindowsJob


def test_non_windows_refuses():
    if os.name == "nt":
        pytest.skip("Windows has job objects")
    with pytest.raises(OSError, match="require Windows"):
        WindowsJob()


@pytest.mark.skipif(os.name != "nt", reason="Real Windows kernel required")
@pytest.mark.parametrize("operation", ["terminate", "close"])
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
