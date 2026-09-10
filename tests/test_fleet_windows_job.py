"""Actual Windows process-tree tests; never use the production database."""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from fleet.workers import _stream_process
from test_fleet_workers import _request


pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows Job Object contract")


@pytest.mark.parametrize("exit_code,cancel", [(0, False), (1, False), (1, True)])
def test_replay_transport_reaps_private_pipe_child_after_helper_exit(tmp_path, monkeypatch, exit_code, cancel):
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handles = []
    monkeypatch.setenv("SERENA_FLEET_EVENT_DIR", str(tmp_path / "events"))
    source = (
        "import sys,subprocess,os,time\n"
        "sys.stdin.read()\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "print(child.pid,flush=True)\n"
        f"time.sleep({60 if cancel else 1})\n"
        f"os._exit({exit_code})\n"
    )

    def observe(line):
        handle = api.OpenProcess(0x100001, False, int(line.strip()))  # SYNCHRONIZE | TERMINATE
        assert handle, ctypes.get_last_error()
        handles.append(handle)

    try:
        result = _stream_process(
            [sys.executable, "-c", source], request=_request(tmp_path, "codex"),
            parse_stdout=observe, cancel_requested=lambda: cancel and bool(handles),
            on_event=lambda *args: None, cleanup_exited_group=True,
        )
        assert result.exit_code == exit_code
        assert result.cancelled is cancel
        assert len(handles) == 1
        assert api.WaitForSingleObject(handles[0], 5000) == 0, "helper child survived replay cleanup"
    finally:
        for handle in handles:
            api.TerminateProcess(handle, 1)  # exact handle, even on assertion failure
            api.CloseHandle(handle)


def test_owner_kill_closes_noninherited_job_handle(tmp_path):
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    marker = tmp_path / "owned-child.pid"
    source = (
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        "from fleet.windows_job import HelperJob\n"
        "child=subprocess.Popen([sys.executable,'-c','import sys,time; sys.stdin.read(); time.sleep(60)'],"
        "stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "job=HelperJob(child)\n"
        "child.stdin.close()\n"
        f"Path({str(marker)!r}).write_text(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    owner = subprocess.Popen([sys.executable, "-c", source], cwd=Path(__file__).resolve().parents[1],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    handle = None
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and owner.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "job owner never published its child"
        handle = api.OpenProcess(0x100001, False, int(marker.read_text()))
        assert handle, ctypes.get_last_error()
        owner.kill()
        owner.wait(timeout=5)
        assert api.WaitForSingleObject(handle, 5000) == 0, "child inherited a job handle and survived owner death"
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)
        owner.stderr.close()
        if handle:
            api.TerminateProcess(handle, 1)
            api.CloseHandle(handle)


def test_assignment_failure_never_releases_helper_request(tmp_path, monkeypatch):
    from fleet import windows_job

    def refuse(process):
        raise OSError("job assignment refused")

    monkeypatch.setattr(windows_job, "HelperJob", refuse)
    monkeypatch.setenv("SERENA_FLEET_EVENT_DIR", str(tmp_path / "events"))
    marker = tmp_path / "must-not-start"
    source = f"import sys; from pathlib import Path; sys.stdin.read(); Path({str(marker)!r}).touch()"
    with pytest.raises(OSError, match="job assignment refused"):
        _stream_process(
            [sys.executable, "-c", source], request=_request(tmp_path, "codex"),
            parse_stdout=lambda _: None, cancel_requested=lambda: False,
            on_event=lambda *args: None, cleanup_exited_group=True,
        )
    assert not marker.exists()
