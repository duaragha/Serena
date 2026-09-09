"""Frozen panes must not outlive the backend that froze them.

An idle pane is frozen with SIGSTOP and thawed with SIGCONT, and which panes
are frozen is known only to the backend process holding the Terminal objects.
Nothing ran at shutdown, so restarting the backend while any pane was frozen
left the child stopped forever: orphaned to init, no controlling terminal,
waiting in do_signal_stop for a SIGCONT whose only sender had exited. Reopening
that chat then started a second `codex resume` against a rollout the first one
still held.

Three of these were live on this machine when it was found, aged 1h17m to
1h34m, two of them `codex resume` for chats in the sidebar.

The recovery has to work on GROUPS. Freezing uses killpg, so the whole group
stops together, and inside one the node wrapper commonly sits in S while the
real binary sits in T -- keying off the stopped process alone left the wrapper
holding the rollout.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from ui import pty_terminal

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="reads /proc"
)

# The sweep only looks at processes whose command line names an agent.
MARKER_SOURCE = "import time  # codex\ntime.sleep(120)\n"


def _marker_pids() -> list[int]:
    out = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        if "# codex" in pty_terminal._agent_cmdline(pid):
            out.append(pid)
    return out


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class _Orphan:
    """A real process group: own session, no tty, reparented to init."""

    def __init__(self, stop: bool = True):
        # Snapshot first: several of these exist at once, and picking any
        # matching pid would let one instance clean up another's process.
        before = set(_marker_pids())
        # sh forks the python child and exits immediately, so init adopts it.
        # setsid detaches the session, which is what removes the tty.
        subprocess.run(
            ["setsid", "sh", "-c", f'{sys.executable} -c "{MARKER_SOURCE}" &'],
            check=True,
            timeout=30,
        )
        self.pid = self._find(before)
        # "Exits immediately" is not "before this line": wait until init has
        # actually adopted the child, or the sweep correctly refuses it as a
        # process with a living parent and the test flakes.
        assert _wait_for(lambda: pty_terminal._proc_field(self.pid, 4) == 1), "never orphaned"
        if stop:
            os.killpg(os.getpgid(self.pid), signal.SIGSTOP)
            assert _wait_for(lambda: pty_terminal._is_stopped(self.pid)), "did not stop"

    def _find(self, before: set[int]) -> int:
        found: list[int] = []

        def look() -> bool:
            found[:] = sorted(set(_marker_pids()) - before)
            return bool(found)

        assert _wait_for(look), "helper process never appeared"
        return found[0]

    def alive(self) -> bool:
        try:
            os.kill(self.pid, 0)
        except OSError:
            return False
        return True

    def cleanup(self) -> None:
        for sig in (signal.SIGCONT, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(self.pid), sig)
            except OSError:
                pass


@pytest.fixture
def orphan():
    made: list[_Orphan] = []

    def make(stop: bool = True) -> _Orphan:
        item = _Orphan(stop=stop)
        made.append(item)
        return item

    yield make
    for item in made:
        item.cleanup()


def test_a_frozen_orphaned_group_is_found_and_reaped(orphan) -> None:
    victim = orphan(stop=True)

    assert os.getpgid(victim.pid) in pty_terminal._stranded_groups()

    reaped = pty_terminal.sweep_stranded_agents()

    assert os.getpgid(victim.pid) in reaped
    assert _wait_for(lambda: not victim.alive()), "the stranded process survived the sweep"


def test_a_running_orphan_is_left_alone(orphan) -> None:
    """Only FROZEN groups are casualties. A detached agent still doing work is
    not the backend's to kill."""
    working = orphan(stop=False)

    assert os.getpgid(working.pid) not in pty_terminal._stranded_groups()

    pty_terminal.sweep_stranded_agents()

    time.sleep(0.3)
    assert working.alive(), "the sweep killed a process that was still running"


def test_a_frozen_process_with_a_living_parent_is_left_alone() -> None:
    """A process this backend froze on purpose has a living parent -- us. Only
    a group whose leader was reparented to init has actually been stranded."""
    child = subprocess.Popen(
        [sys.executable, "-c", MARKER_SOURCE], start_new_session=True
    )
    try:
        os.killpg(os.getpgid(child.pid), signal.SIGSTOP)
        assert _wait_for(lambda: pty_terminal._is_stopped(child.pid))

        assert os.getpgid(child.pid) not in pty_terminal._stranded_groups()
    finally:
        for sig in (signal.SIGCONT, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(child.pid), sig)
            except OSError:
                pass
        child.wait(timeout=10)


def test_the_stat_reader_survives_a_command_name_with_spaces_and_parens() -> None:
    """/proc/<pid>/stat's comm field is unquoted and can contain ')', so the
    fields are read relative to the LAST one."""
    assert pty_terminal._proc_field(os.getpid(), 4) == os.getppid()
    assert pty_terminal._proc_field(os.getpid(), 5) == os.getpgrp()
    assert pty_terminal._proc_field(2 ** 30, 4) is None
    assert pty_terminal._is_stopped(2 ** 30) is False


def test_shutdown_thaws_before_terminating(monkeypatch) -> None:
    """terminate() escalates SIGHUP -> SIGINT -> SIGTERM, and a stopped
    process runs no handler for any of them, so the order matters."""
    calls: list[str] = []

    monkeypatch.setattr(pty_terminal, "thaw_all", lambda: calls.append("thaw") or 0)
    monkeypatch.setattr(pty_terminal, "kill_all", lambda: calls.append("kill") or 0)

    pty_terminal.shutdown_all()

    assert calls == ["thaw", "kill"]
