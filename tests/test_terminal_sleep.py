"""Idle panes must sleep, working panes must not.

The GTK shell froze idle runtimes and pushed their pages out, which is what kept
memory sane with many panes open. Moving to Electron lost the driver: the client
only ever named the focused pane and its linked sibling, and only polled while a
split was open, so in practice nothing but the sibling ever slept.

Sleeping the wrong pane is worse than not sleeping at all, so the guards matter
as much as the saving: never freeze a runtime that is mid-turn, holds a work
reservation, or is still producing output.
"""

from __future__ import annotations

import errno
import os
import time
from pathlib import Path

import pytest

from ui import pty_terminal


@pytest.fixture
def terminal():
    if pty_terminal._IS_WINDOWS:
        pytest.skip("SIGSTOP-based sleep is POSIX only")
    tid = pty_terminal.spawn(["cat"], str(Path.home()), cols=80, rows=24)
    yield tid
    pty_terminal.kill(tid)


def _make_pausable(tid: str) -> None:
    """Clear the startup grace so a test does not have to sleep through it."""

    term = pty_terminal.get(tid)
    term.started_at = time.monotonic() - 3600
    term.last_activity = time.monotonic() - 3600


def test_an_idle_runtime_sleeps_and_wakes(terminal):
    _make_pausable(terminal)

    assert pty_terminal.pause(terminal) is True
    assert pty_terminal.get_runtime_state(terminal) == "paused"

    assert pty_terminal.resume(terminal) is True
    assert pty_terminal.get_runtime_state(terminal) == "live"


def test_a_runtime_mid_turn_is_never_frozen(terminal):
    _make_pausable(terminal)
    pty_terminal.mark_turn_started(terminal)

    assert pty_terminal.pause(terminal) is False
    assert pty_terminal.get_runtime_state(terminal) == "live"

    pty_terminal.mark_turn_finished(terminal)
    assert pty_terminal.pause(terminal) is True


def test_a_reserved_runtime_is_never_frozen():
    """Work reservations are codex-only, so this one needs its own runtime."""

    if pty_terminal._IS_WINDOWS:
        pytest.skip("SIGSTOP-based sleep is POSIX only")
    terminal = pty_terminal.spawn(
        ["cat"], str(Path.home()), cols=80, rows=24, agent="codex"
    )
    try:
        _run_reserved_runtime_checks(terminal)
    finally:
        pty_terminal.kill(terminal)


def _run_reserved_runtime_checks(terminal: str) -> None:
    _make_pausable(terminal)
    ok, reason = pty_terminal.reserve_work(terminal, "item-1")
    assert ok, reason

    assert pty_terminal.pause(terminal) is False

    pty_terminal.release_work(terminal, "item-1")
    assert pty_terminal.pause(terminal) is True


def test_an_explicitly_protected_runtime_is_never_frozen(terminal):
    """A half-typed message is the one sleep a user would actually notice."""

    _make_pausable(terminal)
    assert pty_terminal.pause(terminal, protected=True) is False
    assert pty_terminal.pause(terminal, protected=False) is True


def test_a_freshly_spawned_runtime_is_left_alone(terminal):
    """Prewarm exists so a pane is not frozen before it has finished starting."""

    assert pty_terminal.pause(terminal, prewarm_seconds=60) is False


def test_a_runtime_that_is_still_talking_survives_the_idle_sweep(terminal):
    """Output counts as activity, or a busy agent gets frozen mid-thought."""

    _make_pausable(terminal)
    pty_terminal.write(terminal, b"hello\n")
    # Let the echo come back, which is what refreshes last_activity.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if pty_terminal.read_available(terminal, timeout=0.1):
            break

    assert pty_terminal.pause(terminal, min_idle_seconds=30) is False
    # With no idle floor the same runtime is a fair candidate.
    assert pty_terminal.pause(terminal, min_idle_seconds=0) is True


def test_waking_clears_the_reclaim_flag_so_it_can_be_reclaimed_again(terminal):
    _make_pausable(terminal)
    assert pty_terminal.pause(terminal) is True
    pty_terminal.get(terminal).reclaimed = True

    pty_terminal.resume(terminal)

    assert pty_terminal.get(terminal).reclaimed is False


def test_reclaim_refuses_a_runtime_that_is_not_asleep(terminal):
    """Only a frozen runtime has cold pages worth pushing out."""

    assert pty_terminal.reclaim_memory(terminal) == 0.0
    assert pty_terminal.get(terminal).reclaimed is False


def test_reclaim_reports_zero_when_the_cgroup_is_not_writable(terminal, monkeypatch):
    """A plain fork inherits a root-owned session scope on most systems.

    The freeze still works there, so this must report nothing rather than
    claiming memory it did not release.
    """

    monkeypatch.setattr(pty_terminal, "_reclaim_supported", None, raising=False)
    _make_pausable(terminal)
    assert pty_terminal.pause(terminal) is True

    freed = pty_terminal.reclaim_memory(terminal)

    assert freed >= 0.0
    if pty_terminal._reclaim_supported is False:
        assert freed == 0.0


def test_every_open_runtime_is_a_sleep_candidate(terminal):
    """The registry is what the sweep walks; a pane must not be invisible."""

    assert terminal in pty_terminal.live_terminal_ids()


# ── user-owned cgroup placement ─────────────────────────────────────────────
# memory.reclaim is only writable in a cgroup this user owns. A plain fork
# inherits the login session scope, which logind owns, so the reclaim that
# makes sleeping worthwhile fails there with EPERM. Each child therefore gets
# its own transient user scope.


def _requires_posix() -> None:
    if pty_terminal._IS_WINDOWS:
        pytest.skip("transient user scopes are POSIX only")


def _settled_comm(pid: int, timeout: float = 5.0) -> str:
    """Wait out the D-Bus round trip and return what the pid finally is.

    systemd-run holds the pid for the few ms it takes to create the scope and
    then execs the real command in place. Production never has to care -- the
    prewarm floor is a thousand times longer -- but a test that reads /proc the
    instant spawn() returns is racing that exec.
    """

    deadline = time.monotonic() + timeout
    comm = ""
    while time.monotonic() < deadline:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as handle:
            comm = handle.read().strip()
        if comm != "systemd-run":
            return comm
        time.sleep(0.002)
    return comm


def test_the_scope_wrapper_keeps_the_real_command_intact():
    """The agent argv has to survive verbatim after the -- separator."""

    _requires_posix()
    wrapped = pty_terminal._scope_argv(["claude", "--dangerously-skip-permissions"])

    assert wrapped[0] == "systemd-run"
    assert "--user" in wrapped and "--scope" in wrapped
    # --collect keeps a failed scope from lingering in the user manager.
    assert "--collect" in wrapped
    assert wrapped[wrapped.index("--") + 1:] == [
        "claude",
        "--dangerously-skip-permissions",
    ]


def test_each_scope_gets_its_own_unit_name():
    """Reusing a unit name would collide with a pane that is still open."""

    _requires_posix()
    first = pty_terminal._scope_argv(["cat"])
    second = pty_terminal._scope_argv(["cat"])

    units = [arg for arg in first + second if arg.startswith("--unit=")]
    assert len(units) == 2
    assert units[0] != units[1]


def test_scope_support_is_false_without_systemd(monkeypatch):
    """A machine with no systemd-run must fall back, not raise."""

    _requires_posix()
    monkeypatch.setattr(pty_terminal, "_scope_supported", None, raising=False)
    monkeypatch.setattr(pty_terminal.shutil, "which", lambda *a, **k: None)

    assert pty_terminal._systemd_scope_supported() is False


def test_scope_support_is_false_without_a_user_bus(monkeypatch):
    """systemd-run exists but the user bus is unreachable -> plain fork."""

    _requires_posix()
    monkeypatch.setattr(pty_terminal, "_scope_supported", None, raising=False)
    monkeypatch.setattr(pty_terminal.shutil, "which", lambda *a, **k: "/usr/bin/systemd-run")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)

    assert pty_terminal._systemd_scope_supported() is False


def test_a_spawned_runtime_lands_in_a_cgroup_this_user_owns():
    """The whole point: the pane's own memory.reclaim must belong to us.

    Ownership is the thing this change controls, so that is what is asserted.
    Whether the write then succeeds also depends on cgroupfs being mounted
    read-write, which it is not inside a sandbox and which no amount of scope
    placement can fix; reclaim_memory() latches off on that EROFS separately.

    Skips rather than fails where transient scopes are unavailable, because
    the fallback is a deliberate, supported degradation.
    """

    _requires_posix()
    if not pty_terminal._systemd_scope_supported():
        pytest.skip("no reachable user systemd manager on this machine")

    tid = pty_terminal.spawn(["cat"], str(Path.home()), cols=80, rows=24)
    try:
        pid = pty_terminal.get(tid).proc.pid
        assert _settled_comm(pid) == "cat"
        cgroup = pty_terminal._cgroup_dir(pid)
        assert cgroup is not None
        # user@<uid>.service is the user's own manager subtree; the login
        # session scope that a plain fork inherits is not under it.
        assert "user@" in cgroup
        knob = os.path.join(cgroup, "memory.reclaim")
        assert os.stat(knob).st_uid == os.getuid()
    finally:
        pty_terminal.kill(tid)


def test_the_scope_wrapper_preserves_pid_and_process_group():
    """--scope execs in place, so killpg on getpgid must still reach the child.

    If systemd-run forked instead, proc.pid would be the wrapper and the
    SIGSTOP/SIGCONT path plus every RSS reading would target the wrong process.
    """

    _requires_posix()
    if not pty_terminal._systemd_scope_supported():
        pytest.skip("no reachable user systemd manager on this machine")

    tid = pty_terminal.spawn(["cat"], str(Path.home()), cols=80, rows=24)
    try:
        pid = pty_terminal.get(tid).proc.pid
        # The pid must end up being the agent itself, not a surviving
        # systemd-run, and must not have moved to get there.
        assert _settled_comm(pid) == "cat"
        assert pty_terminal.get(tid).proc.pid == pid
        assert os.getpgid(pid) == pid

        _make_pausable(tid)
        assert pty_terminal.pause(tid) is True
        assert pty_terminal.resume(tid) is True
    finally:
        pty_terminal.kill(tid)


def test_a_missing_agent_binary_still_raises_file_not_found():
    """The HTTP layer turns this into '<agent> CLI not found on PATH'.

    Wrapping the command would otherwise hide it behind systemd-run, which
    does exist, and downgrade a clear error into a pane that dies by itself.
    """

    _requires_posix()
    with pytest.raises(FileNotFoundError):
        pty_terminal.spawn(
            ["serena-definitely-not-a-real-binary"], str(Path.home()), cols=80, rows=24
        )


def test_spawning_falls_back_to_a_plain_fork_when_the_scope_fails(monkeypatch):
    """A pane without reclaim beats no pane at all."""

    _requires_posix()
    monkeypatch.setattr(pty_terminal, "_scope_supported", True, raising=False)
    monkeypatch.setattr(
        pty_terminal,
        "_scope_argv",
        lambda argv: ["serena-definitely-not-a-real-binary", *argv],
    )

    tid = pty_terminal.spawn(["cat"], str(Path.home()), cols=80, rows=24)
    try:
        assert pty_terminal.is_alive(tid)
        _make_pausable(tid)
        assert pty_terminal.pause(tid) is True
        assert pty_terminal.resume(tid) is True
    finally:
        pty_terminal.kill(tid)


def test_a_partial_reclaim_is_success_not_a_reason_to_give_up(terminal, monkeypatch):
    """The kernel returns EAGAIN when it reclaims less than asked for.

    Measured on this machine: a 4G request against a 311MB process returns
    EAGAIN having released 304MB. Latching off there would disable reclaim
    immediately after the first pass that actually worked.
    """

    monkeypatch.setattr(pty_terminal, "_reclaim_supported", None, raising=False)
    monkeypatch.setattr(pty_terminal, "_reported_reclaim_failures", set(), raising=False)
    monkeypatch.setattr(pty_terminal, "_cgroup_dir", lambda pid: "/tmp")
    _refuse_reclaim_with(monkeypatch, errno.EAGAIN)
    _make_pausable(terminal)
    assert pty_terminal.pause(terminal) is True

    pty_terminal.reclaim_memory(terminal)

    # EAGAIN is the pass that did the work, so neither latch may trip.
    assert pty_terminal._reclaim_supported is not False
    assert pty_terminal.get(terminal).reclaim_supported is not False
    assert pty_terminal.get(terminal).reclaimed is True


_REAL_OPEN = open


class _AcceptedWrite:
    """Stand-in for a memory.reclaim knob that accepts the write."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def write(self, payload):
        return len(payload)


def _fake_reclaim_write(path, *args, **kwargs):
    """Accept the reclaim write, but leave every other open() real.

    reclaim_memory() reads /proc/<pid>/statm through the same builtin, so a
    blanket fake would break the RSS accounting this is meant to exercise.
    """

    if str(path).endswith("memory.reclaim"):
        return _AcceptedWrite()
    return _REAL_OPEN(path, *args, **kwargs)


def _refuse_reclaim_with(monkeypatch, code: int) -> None:
    """Make the next memory.reclaim write fail with *code*."""

    def _refuse(path, *args, **kwargs):
        if str(path).endswith("memory.reclaim"):
            raise OSError(code, os.strerror(code))
        return _REAL_OPEN(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _refuse)


@pytest.mark.parametrize("code", [errno.EROFS, errno.ENOENT, errno.ENODEV])
def test_a_machine_that_can_never_reclaim_latches_off_globally(
    terminal, monkeypatch, code
):
    """Read-only cgroupfs or a kernel with no knob applies to every runtime.

    Nothing about a different cgroup changes the answer, so learning it once
    is right and saves a doomed write on every future sweep.
    """

    monkeypatch.setattr(pty_terminal, "_reclaim_supported", None, raising=False)
    monkeypatch.setattr(pty_terminal, "_reported_reclaim_failures", set(), raising=False)
    monkeypatch.setattr(pty_terminal, "_cgroup_dir", lambda pid: "/tmp")
    _refuse_reclaim_with(monkeypatch, code)
    _make_pausable(terminal)
    assert pty_terminal.pause(terminal) is True

    assert pty_terminal.reclaim_memory(terminal) == 0.0
    assert pty_terminal._reclaim_supported is False


@pytest.mark.parametrize("code", [errno.EPERM, errno.EACCES, errno.EINVAL])
def test_an_unownable_cgroup_latches_off_only_that_runtime(
    terminal, monkeypatch, code
):
    """EPERM/EACCES mean this child missed its scope, not that the box can't.

    EINVAL belongs here too: measured on this machine a malformed write
    returns EINVAL having reclaimed nothing, so a hardcoded payload that the
    kernel rejects will keep being rejected -- but only for this write, which
    says nothing about a pane that did land in a scope of its own.
    """

    monkeypatch.setattr(pty_terminal, "_reclaim_supported", None, raising=False)
    monkeypatch.setattr(pty_terminal, "_reported_reclaim_failures", set(), raising=False)
    monkeypatch.setattr(pty_terminal, "_cgroup_dir", lambda pid: "/tmp")
    _refuse_reclaim_with(monkeypatch, code)
    _make_pausable(terminal)
    assert pty_terminal.pause(terminal) is True

    assert pty_terminal.reclaim_memory(terminal) == 0.0
    assert pty_terminal.get(terminal).reclaim_supported is False
    # The machine itself is still considered capable.
    assert pty_terminal._reclaim_supported is not False


def test_one_unscoped_pane_does_not_disable_reclaim_for_the_others(monkeypatch):
    """The regression this whole split exists for.

    spawn() silently falls back to a plain fork when a scope will not start,
    so one pane really can sit in a cgroup we do not own while its neighbours
    sit in proper scopes. Letting that pane's EPERM set the process-wide latch
    would switch reclaim off for every pane, including ones already proven to
    work, for the rest of the run.
    """

    if pty_terminal._IS_WINDOWS:
        pytest.skip("SIGSTOP-based sleep is POSIX only")

    monkeypatch.setattr(pty_terminal, "_reclaim_supported", None, raising=False)
    monkeypatch.setattr(pty_terminal, "_reported_reclaim_failures", set(), raising=False)
    monkeypatch.setattr(pty_terminal, "_cgroup_dir", lambda pid: "/tmp")

    scoped = pty_terminal.spawn(["cat"], str(Path.home()), cols=80, rows=24)
    unscoped = pty_terminal.spawn(["cat"], str(Path.home()), cols=80, rows=24)
    try:
        # The pane that got a real scope reclaims fine.
        monkeypatch.setattr("builtins.open", _fake_reclaim_write)
        _make_pausable(scoped)
        assert pty_terminal.pause(scoped) is True
        pty_terminal.reclaim_memory(scoped)
        assert pty_terminal.get(scoped).reclaim_supported is True

        # The pane that fell back to a plain fork cannot.
        _refuse_reclaim_with(monkeypatch, errno.EPERM)
        _make_pausable(unscoped)
        assert pty_terminal.pause(unscoped) is True
        assert pty_terminal.reclaim_memory(unscoped) == 0.0
        assert pty_terminal.get(unscoped).reclaim_supported is False

        # It must not have poisoned the machine-wide latch, so a pane that
        # wakes and sleeps again is still a reclaim candidate.
        assert pty_terminal._reclaim_supported is not False
        pty_terminal.resume(scoped)
        _make_pausable(scoped)
        assert pty_terminal.pause(scoped) is True
        monkeypatch.setattr("builtins.open", _fake_reclaim_write)
        pty_terminal.reclaim_memory(scoped)
        assert pty_terminal.get(scoped).reclaimed is True
    finally:
        pty_terminal.kill(scoped)
        pty_terminal.kill(unscoped)
