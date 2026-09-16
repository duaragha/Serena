"""User-owned cgroup placement and memory reclaim for native pane owners.

A frozen owner still holds its whole heap resident. Pushing those pages out
needs ``memory.reclaim`` on a cgroup this user owns, and a child spawned by the
backend service sits in the service's own cgroup, which also holds the backend
and every awake owner. Each native owner therefore runs in its own transient
user scope, exactly like terminal panes do, and reclaim only ever writes to a
scope whose name proves Serena created it.

The unit name carries the host pid in the same shape terminal panes use, so the
startup reaper stops scopes whose host is gone without any extra bookkeeping.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import subprocess
import threading
import uuid

_PROBE_TIMEOUT = 5.0
_supported: bool | None = None
_probe_lock = threading.Lock()

# systemd appends ".scope" to the unit name we pass.
OWNED_SCOPE = re.compile(r"^serena-(?:pty|pane)-\d+-[0-9a-f]+\.scope$")


def scope_argv(argv: list[str]) -> list[str]:
    """Wrap *argv* so it execs inside a new user scope with the same pid."""
    return [
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
        f"--unit=serena-pane-{os.getpid()}-{uuid.uuid4().hex[:8]}",
        # Without a description systemd logs the full argv, prompts included.
        "--description=Serena native pane",
        "--",
        *argv,
    ]


def scope_supported() -> bool:
    """Probe once whether the user manager will create scopes for us."""
    global _supported
    with _probe_lock:
        if _supported is not None:
            return _supported
        _supported = False
        if os.name == "nt" or shutil.which("systemd-run") is None:
            return _supported
        if not (os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("DBUS_SESSION_BUS_ADDRESS")):
            return _supported
        try:
            probe = subprocess.run(scope_argv(["true"]), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=_PROBE_TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError):
            return _supported
        _supported = probe.returncode == 0
        return _supported


def owned_cgroup(pid: int) -> str | None:
    """The Serena-created scope holding *pid*, never any other cgroup."""
    try:
        with open(f"/proc/{pid}/cgroup", encoding="utf-8") as handle:
            for line in handle:
                hierarchy, _, rest = line.strip().partition(":")
                if hierarchy != "0":
                    continue
                path = rest.partition(":")[2]
                directory = os.path.join("/sys/fs/cgroup", path.lstrip("/"))
                if OWNED_SCOPE.match(os.path.basename(directory)) and os.path.isdir(directory):
                    return directory
    except OSError:
        return None
    return None


def _memory_mb(cgroup: str) -> float:
    try:
        with open(os.path.join(cgroup, "memory.current"), encoding="utf-8") as handle:
            return int(handle.read().strip()) / (1024 * 1024)
    except (OSError, ValueError):
        return 0.0


def reclaim(pid: int) -> float | None:
    """Push a frozen owner's pages out; MB released, or None when impossible.

    Waking stays a SIGCONT: pages fault back on demand, so this trades idle
    memory for refault I/O on the first interaction, not wake latency.
    """
    cgroup = owned_cgroup(pid)
    if cgroup is None:
        return None
    before = _memory_mb(cgroup)
    try:
        with open(os.path.join(cgroup, "memory.reclaim"), "w", encoding="utf-8") as handle:
            handle.write("4G")
    except OSError as error:
        # EAGAIN means the kernel freed less than asked, which is the normal
        # outcome of a pass that did real work.
        if error.errno != errno.EAGAIN:
            return None
    return max(0.0, before - _memory_mb(cgroup))
