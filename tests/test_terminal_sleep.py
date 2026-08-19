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
