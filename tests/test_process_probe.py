"""Liveness queries cannot send Windows console events or terminate workers."""

import subprocess
import sys

import pytest

from core import process_probe
from core.work_jobs import process_start_token
from fleet.store import _pid_exists, _process_alive


@pytest.mark.parametrize("exists", [True, False])
def test_windows_probe_never_signals(monkeypatch, exists):
    monkeypatch.setattr(process_probe.sys, "platform", "win32")
    monkeypatch.setattr(process_probe.psutil, "pid_exists", lambda pid: exists)
    def forbidden(*args):
        pytest.fail("a liveness query sent a signal")
    monkeypatch.setattr(process_probe.os, "kill", forbidden)
    if exists:
        process_probe.probe_process(123)
    else:
        with pytest.raises(ProcessLookupError):
            process_probe.probe_process(123)


@pytest.mark.parametrize("pid", [0, -1, True, None, "123"])
def test_invalid_pid_cannot_target_a_process_group(pid):
    with pytest.raises(ProcessLookupError):
        process_probe.probe_process(pid)


def test_real_worker_survives_repeated_liveness_queries():
    # This test creates and owns its only target. No installed process is used.
    with subprocess.Popen(
        [sys.executable, "-u", "-c", "import time; print('ready'); time.sleep(30)"],
        stdout=subprocess.PIPE, text=True,
    ) as child:
        try:
            assert child.stdout.readline().strip() == "ready"
            token = process_start_token(child.pid)
            assert token and not token.startswith("pid:")
            for _ in range(20):
                process_probe.probe_process(child.pid)
                assert _pid_exists(child.pid)
                assert _process_alive(child.pid, token)
                assert not _process_alive(child.pid, token + "wrong")
                assert child.poll() is None
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
        with pytest.raises(ProcessLookupError):
            process_probe.probe_process(child.pid)
