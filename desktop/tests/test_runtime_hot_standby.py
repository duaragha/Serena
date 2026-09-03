"""Exercise real process-group pause/resume without rebuilding the child."""

import os
import signal
import subprocess
import time

import pytest


app_gtk = pytest.importorskip("desktop.app_gtk")
ChatsApp = app_gtk.ChatsApp


def _process_state(pid: int) -> str:
    with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("State:"):
                return line.split()[1]
    return ""


def _wait_for_state(pid: int, expected_stopped: bool, timeout: float = 1.0) -> float:
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        stopped = _process_state(pid) in {"T", "t"}
        if stopped == expected_stopped:
            return (time.perf_counter() - started) * 1000
        time.sleep(0.001)
    raise AssertionError("process did not change standby state")


def test_process_group_hot_standby_resumes_in_low_milliseconds():
    process = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        ChatsApp._terminate_runtime_pid(process.pid, signal.SIGSTOP)
        _wait_for_state(process.pid, True)

        ChatsApp._terminate_runtime_pid(process.pid, signal.SIGCONT)
        wake_ms = _wait_for_state(process.pid, False)

        assert process.pid == os.getpgid(process.pid)
        assert wake_ms < 100
        assert process.poll() is None
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
