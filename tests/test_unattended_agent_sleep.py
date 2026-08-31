"""An agent working in a pane nobody is watching must not be frozen.

Idle sleep measures from ``last_activity``, which was only ever touched by our
own reads and writes. Reads happen while a websocket is attached, so the moment
the user looks at another pane the clock stops moving even though the child is
still churning. After the idle window the pane was SIGSTOPped mid-turn.

That is not theoretical: three Codex processes were found stopped, one of them
for an hour and thirty-nine minutes, on a chat the user believed was running.

The transcript is the fix. When the child writes to its session file, that file
grows, and the sweep already reads its size and mtime for other reasons. A
version that changed is the child saying it is alive, and it is the only such
signal available when nobody is attached.
"""

from __future__ import annotations

import time

import pytest

from ui import pty_terminal


@pytest.fixture()
def pane():
    """A real PTY child that sits there without producing output."""
    tid = pty_terminal.spawn(["cat"], cwd="/tmp", cols=80, rows=24)
    yield tid
    try:
        pty_terminal.kill(tid)
    except Exception:
        pass


def _age(tid: str) -> float:
    return time.monotonic() - pty_terminal.get(tid).last_activity


def test_a_growing_transcript_counts_as_activity(pane) -> None:
    term = pty_terminal.get(pane)
    term.last_activity = time.monotonic() - 3600      # look long idle
    assert _age(pane) > 3000

    pty_terminal.refresh_turn_state(pane, None, (100, 1))   # first sighting
    pty_terminal.refresh_turn_state(pane, None, (200, 2))   # the child wrote

    assert _age(pane) < 5, "a transcript that grew must count as the child working"


def test_an_unchanged_transcript_does_not_reset_the_clock(pane) -> None:
    """Otherwise nothing would ever sleep: the sweep polls constantly."""
    pty_terminal.refresh_turn_state(pane, None, (100, 1))
    term = pty_terminal.get(pane)
    term.last_activity = time.monotonic() - 3600

    for _ in range(3):
        pty_terminal.refresh_turn_state(pane, None, (100, 1))

    assert _age(pane) > 3000, "an idle pane must still be allowed to sleep"


def test_the_first_sighting_is_not_mistaken_for_work(pane) -> None:
    """A pane adopted mid-life has no previous version to compare against."""
    term = pty_terminal.get(pane)
    term.last_activity = time.monotonic() - 3600

    pty_terminal.refresh_turn_state(pane, None, (100, 1))

    assert _age(pane) > 3000, "the first version seen says nothing about activity"


def test_a_pane_whose_agent_is_writing_is_not_frozen(pane) -> None:
    """The whole point: the sweep must refuse to pause it."""
    term = pty_terminal.get(pane)
    term.started_at = time.monotonic() - 3600         # past the prewarm window
    term.last_activity = time.monotonic() - 3600

    assert pty_terminal.pause(pane, min_idle_seconds=600) is True, "sanity: idle sleeps"
    pty_terminal.resume(pane)

    term.last_activity = time.monotonic() - 3600
    pty_terminal.refresh_turn_state(pane, None, (100, 1))
    pty_terminal.refresh_turn_state(pane, None, (900, 2))

    assert pty_terminal.pause(pane, min_idle_seconds=600) is False, (
        "a pane whose transcript is growing was frozen mid-turn"
    )


def test_an_idle_pane_still_sleeps(pane) -> None:
    """The feature has to keep working; freezing idle panes is the point of it."""
    term = pty_terminal.get(pane)
    term.started_at = time.monotonic() - 3600
    pty_terminal.refresh_turn_state(pane, None, (100, 1))
    term.last_activity = time.monotonic() - 3600

    assert pty_terminal.pause(pane, min_idle_seconds=600) is True
    pty_terminal.resume(pane)
