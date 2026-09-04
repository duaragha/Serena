"""A pane you can see must be running.

A merged (linked) view puts two agents on screen side by side, and only one of
them can hold keyboard focus. The idle sweep keyed sleep on focus alone, so the
other half -- fully visible, half the window -- was SIGSTOPped once it had been
quiet for a while and stayed that way for as long as the chat stayed open. It
painted nothing, which reads as "codex isn't showing".

One of these was found stopped for ten hours while its pane was on screen.

Sleep is for panes that are off screen. This pins that down from both ends:
the sweep resumes everything visible and refuses to stop it, and attach() wakes
whatever a renderer binds itself to, which covers every path onto the screen
that does not go through the sweep -- a reconnect after a dropped socket, an
older client that sends no visible list, a pane restored on load.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import pytest

from ui import pty_terminal

WEB_SOURCE = Path(__file__).resolve().parents[1] / "ui" / "web.py"


def _state(pid: int) -> str:
    """The kernel's view, not ours: 'T' is a stopped process."""
    return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0]


@pytest.fixture()
def pane():
    tid = pty_terminal.spawn(["bash", "-c", "sleep 300"], cwd="/tmp", cols=80, rows=24)
    yield tid
    try:
        pty_terminal.kill(tid)
    except Exception:
        pass


def _force_sleep(tid: str) -> None:
    """Stop the pane the way the sweep does, past every guard."""
    term = pty_terminal.get(tid)
    term.started_at = time.monotonic() - 3600
    term.last_activity = time.monotonic() - 3600
    assert pty_terminal.pause(tid, min_idle_seconds=0.0) is True
    assert _state(term.proc.pid) == "T", "the process was not actually stopped"


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc to read process state")
def test_attaching_a_renderer_wakes_a_stopped_pane(pane) -> None:
    """A stopped process accepts the socket, replays, and then draws nothing."""
    _force_sleep(pane)

    assert pty_terminal.attach(pane) is not None

    assert pty_terminal.get_runtime_state(pane) == "live"
    assert _state(pty_terminal.get(pane).proc.pid) != "T", (
        "the pane is on screen and still stopped"
    )


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc to read process state")
def test_a_woken_pane_still_produces_output(pane) -> None:
    """Runtime state saying 'live' is not the same as the process running."""
    _force_sleep(pane)
    pty_terminal.attach(pane)

    pid = pty_terminal.get(pane).proc.pid
    # A stopped process cannot reap its own children or advance at all; the
    # cheapest honest probe is that the kernel no longer reports it stopped
    # after a scheduling slice.
    time.sleep(0.2)
    assert _state(pid) not in {"T", "t"}


def test_the_sweep_resumes_every_visible_pane_not_only_the_focused_one() -> None:
    source = WEB_SOURCE.read_text(encoding="utf-8")
    start = source.index("def api_terminal_runtime_sync(")
    body = source[start : source.index("@app.route", start + 10)]

    assert "if tid in visible_tids or pin_both:" in body, (
        "the unfocused half of a merged view is still a sleep candidate"
    )
    assert "if tid == focus_tid or pin_both:" not in body
    assert 'data.get("visible_tids")' in body, "the server cannot tell what is on screen"
    assert "visible_tids.add(focus_tid)" in body, (
        "a client that sends no visible list must still keep its focused pane awake"
    )


def test_the_client_reports_the_whole_split_as_visible() -> None:
    source = WEB_SOURCE.read_text(encoding="utf-8")
    start = source.index("async function _syncWebRuntimePolicy(")
    body = source[start : source.index("\nfunction _scheduleWebRuntimePolicy(", start)]

    assert "visible_tids: visibleTids" in body, "the split is never reported"
    assert "_gtkSplitActive && _gtkSplitSids && _gtkSplitSids.length" in body, (
        "only the focused pane is treated as on screen"
    )
