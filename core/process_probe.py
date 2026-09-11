"""Read-only process existence checks with POSIX-compatible exceptions."""

from __future__ import annotations

import errno
import os
import sys

import psutil


def probe_process(pid: int) -> None:
    """Raise if absent; never signal Windows processes to query existence.

    Windows signal zero is CTRL_C_EVENT, not the POSIX existence probe.
    Callers retain their own permission-denial and birth-token policies.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ProcessLookupError(errno.ESRCH, "process probe requires a positive integer PID")
    if sys.platform != "win32":
        os.kill(pid, 0)
        return
    if not psutil.pid_exists(pid):
        raise ProcessLookupError(errno.ESRCH, "process does not exist", pid)
