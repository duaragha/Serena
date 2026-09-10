"""Real Win32 tests with gated Python children, never provider sessions."""

import os
import subprocess
import sys
import time
from types import SimpleNamespace

import psutil
import pytest

from core.workspace_windows_job import WindowsJob


def test_partial_suspension_is_rolled_back(monkeypatch):
    job = object.__new__(WindowsJob)
    job._suspended = []
    resumed, closed = [], []
    job._api = SimpleNamespace(ResumeThread=lambda h: resumed.append(h), CloseHandle=lambda h: closed.append(h))
    def suspend(pid, tid):
        if tid == 2:
            raise psutil.AccessDenied(pid)
        return tid
    monkeypatch.setattr(job, "_threads", lambda: {(1, 1), (1, 2)})
    monkeypatch.setattr(job, "_thread_alive", lambda h: True)
    monkeypatch.setattr(job, "_suspend_thread", suspend)
    with pytest.raises(psutil.AccessDenied):
        job.suspend()
    assert not job.suspended and resumed == closed == [1]


def test_pre_suspended_helper_is_never_resumed_by_us(monkeypatch):
    job = object.__new__(WindowsJob)
    job._suspended = []
    counts = {1: 0, 2: 1}
    closed = []
    def suspend(h):
        counts[h] += 1
        return counts[h] - 1
    def resume(h):
        counts[h] -= 1
        return counts[h] + 1
    job._api = SimpleNamespace(OpenThread=lambda rights, inherit, tid: tid,
        GetProcessIdOfThread=lambda h: 1, SuspendThread=suspend, ResumeThread=resume,
        CloseHandle=lambda h: closed.append(h))
    monkeypatch.setattr(job, "_threads", lambda: {(1, 1), (1, 2)})
    monkeypatch.setattr(job, "_thread_alive", lambda h: True)
    monkeypatch.setattr(job, "_contains", lambda pid: True)
    assert job.suspend()
    assert counts == {1: 1, 2: 2}
    assert job.suspend() and counts == {1: 1, 2: 2}
    job.resume()
    assert counts == {1: 0, 2: 1} and sorted(closed) == [1, 2]


def test_job_refuses_unsettled_thread_tree(monkeypatch):
    job = object.__new__(WindowsJob)
    job._suspended = []
    suspended, resumed, closed = [], [], []
    def suspend(pid, tid):
        suspended.append(tid)
        return tid
    job._api = SimpleNamespace(ResumeThread=lambda h: resumed.append(h), CloseHandle=lambda h: closed.append(h))
    monkeypatch.setattr(job, "_threads", lambda: {(1, len(suspended) + 1)})
    monkeypatch.setattr(job, "_thread_alive", lambda h: True)
    monkeypatch.setattr(job, "_suspend_thread", suspend)
    with pytest.raises(OSError, match="did not settle"):
        job.suspend()
    assert suspended == list(range(1, 9)) and resumed == closed == suspended[::-1]
    assert not job.suspended


def test_exited_thread_handle_is_closed_without_resuming_reused_tid(monkeypatch):
    job = object.__new__(WindowsJob)
    job._suspended = [(1, 2, 123)]
    resumed, closed = [], []
    job._api = SimpleNamespace(ResumeThread=lambda h: resumed.append(h), CloseHandle=lambda h: closed.append(h))
    monkeypatch.setattr(job, "_thread_alive", lambda h: False)
    job.resume()
    assert resumed == [] and closed == [123] and not job.suspended


def test_mismatched_thread_owner_is_not_suspended(monkeypatch):
    job = object.__new__(WindowsJob)
    suspended, closed = [], []
    job._api = SimpleNamespace(OpenThread=lambda *args: 123,
        GetProcessIdOfThread=lambda h: 99, SuspendThread=lambda h: suspended.append(h),
        CloseHandle=lambda h: closed.append(h))
    monkeypatch.setattr(job, "_contains", lambda pid: True)
    with pytest.raises(OSError, match="membership changed"):
        job._suspend_thread(1, 2)
    assert not suspended and closed == [123]


def test_failed_resume_keeps_its_handle_for_retry(monkeypatch):
    job = object.__new__(WindowsJob)
    job._suspended = [(1, 2, 123), (1, 3, 456)]
    closed = []
    def resume(handle):
        if handle == 123:
            raise OSError("denied")
        return 1
    job._api = SimpleNamespace(ResumeThread=resume, CloseHandle=lambda h: closed.append(h))
    monkeypatch.setattr(job, "_thread_alive", lambda h: True)
    with pytest.raises(OSError, match="fully resume"):
        job.resume()
    assert job._suspended == [(1, 2, 123)] and closed == [456]
    job._api.ResumeThread = lambda h: 1
    job.resume()
    assert not job.suspended and closed == [456, 123]


def test_exiting_process_is_ignored_but_live_access_failure_is_not(monkeypatch):
    job = object.__new__(WindowsJob)
    monkeypatch.setattr(job, "process_ids", lambda: {1})
    def fail(pid):
        raise OSError("OpenProcess failed")
    monkeypatch.setattr(job, "_contains", fail)
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: False)
    assert job._threads() == set()
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)
    with pytest.raises(OSError, match="OpenProcess"):
        job._threads()


def test_exiting_thread_during_open_does_not_cancel_pause(monkeypatch):
    job = object.__new__(WindowsJob)
    job._suspended = []
    snapshots = iter([{(1, 1), (1, 2)}, {(1, 1)}, {(1, 1)}])
    monkeypatch.setattr(job, "_threads", lambda: next(snapshots))
    monkeypatch.setattr(job, "_thread_alive", lambda h: True)
    def suspend(pid, tid):
        if tid == 2:
            raise OSError("thread exited")
        return tid
    monkeypatch.setattr(job, "_suspend_thread", suspend)
    assert job.suspend() and job._suspended == [(1, 1, 1)]


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
