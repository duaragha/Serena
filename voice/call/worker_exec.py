"""Exec one adopted Claude worker with Linux parent-death containment."""

from __future__ import annotations

import ctypes
import os
import signal
import sys


def _arm_parent_death() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        expected_parent = int(os.environ["SERENA_WORKER_GATE_PID"])
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            return False
        return os.getppid() == expected_parent
    except (OSError, TypeError, ValueError, KeyError):
        return False


def main() -> int:
    if len(sys.argv) < 2 or not _arm_parent_death():
        return 78
    if sys.stdin.buffer.read(1) != b"E":
        return 75
    try:
        os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
    except OSError:
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
