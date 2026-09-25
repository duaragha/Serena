"""Panes in a merged view sleep the way Serena Dev's native panes do.

Only the focused pane of a merged (linked) view is being used. The others are
on screen, so they used to be kept running forever -- a rule written after a
visible pane was frozen part-way through its own startup and painted nothing.
The rule he asked for is the Dev one instead, with that failure designed out:

- a peer on screen sleeps once it has loaded and gone quiet: 5 s if he has not
  touched it since the chat opened, 20 s once he has worked in it;
- it keeps its last frame, and a click, a keystroke, a new layout or a renderer
  attaching wakes it at once, with a full redraw;
- a turn still running, an unsent draft or Serena's reserved work keeps it
  awake, so it goes to sleep only after its code run has finished;
- it never sleeps inside the start-up window, which is where the old bug lived;
- quiet for a minute, it also gives its memory back;
- the focused pane never sleeps, and "pin both" keeps every visible pane up.

Every assertion reads the kernel's view of the process, not our own flag.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ui import pty_terminal

pytestmark = pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc to read process state")


def _state(tid: str) -> str:
    """'T' is a process the kernel has stopped."""
    pid = pty_terminal.get(tid).proc.pid
    return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()[0]


def _stopped(tid: str) -> bool:
    return _state(tid) in {"T", "t"}


@pytest.fixture()
def panes():
    tids = [pty_terminal.spawn(["bash", "-c", "sleep 300"], cwd="/tmp", cols=80, rows=24) for _ in range(3)]
    yield tids
    for tid in tids:
        try:
            pty_terminal.kill(tid)
        except Exception:
            pass


def _loaded(tid: str, *, quiet: float) -> None:
    """Past the start-up guard, and silent for `quiet` seconds."""
    term = pty_terminal.get(tid)
    term.started_at = time.monotonic() - 3600
    term.last_activity = time.monotonic() - quiet


@pytest.fixture()
def sync(monkeypatch):
    from ui import web

    monkeypatch.setattr(web, "_terminal_file_snapshot", lambda tid: (None, None))
    client = web.app.test_client()

    def call(focus, visible, *, engaged=(), protected=(), pin_both=False):
        response = client.post(
            "/api/terminal-runtime/sync",
            json={"focus_tid": focus, "visible_tids": list(visible), "engaged_tids": list(engaged),
                  "protected_tids": list(protected), "pin_both": pin_both},
            base_url="http://127.0.0.1:46747",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert response.status_code == 200, response.data
        return response.get_json()["states"]

    return call


def test_an_untouched_peer_sleeps_once_loaded_and_the_focused_pane_never_does(panes, sync) -> None:
    focus, peer, _ = panes
    _loaded(focus, quiet=3600)
    _loaded(peer, quiet=6)

    states = sync(focus, [focus, peer])

    assert states[peer]["state"] == "paused" and _stopped(peer)
    assert states[focus]["state"] == "live" and not _stopped(focus)


def test_a_peer_he_has_worked_in_gets_twenty_quiet_seconds(panes, sync) -> None:
    focus, peer, _ = panes
    _loaded(peer, quiet=10)
    sync(focus, [focus, peer], engaged=[focus, peer])
    assert not _stopped(peer), "an engaged peer slept after only ten quiet seconds"

    _loaded(peer, quiet=25)
    sync(focus, [focus, peer], engaged=[focus, peer])
    assert _stopped(peer)


def test_a_peer_still_starting_up_is_never_frozen(panes, sync) -> None:
    """The failure the old rule existed for: stopped mid-start, painting nothing."""
    focus, peer, _ = panes
    pty_terminal.get(peer).last_activity = time.monotonic() - 30  # quiet, but just spawned

    sync(focus, [focus, peer])

    assert not _stopped(peer)


@pytest.mark.parametrize("guard", ["turn", "draft", "client-protected", "reserved"])
def test_a_running_turn_a_draft_or_serenas_work_keeps_a_peer_awake(panes, sync, guard) -> None:
    focus, peer, _ = panes
    _loaded(peer, quiet=600)
    term = pty_terminal.get(peer)
    if guard == "turn":
        pty_terminal.mark_turn_started(peer)
    elif guard == "draft":
        term.input_draft = "half a thought"
    elif guard == "reserved":
        term.work_item_id = "job-1"

    sync(focus, [focus, peer], protected=[peer] if guard == "client-protected" else [])

    assert not _stopped(peer)


def test_a_peer_sleeps_once_its_code_run_finishes(panes, sync) -> None:
    focus, peer, _ = panes
    _loaded(peer, quiet=600)
    pty_terminal.mark_turn_started(peer)
    sync(focus, [focus, peer], engaged=[peer])
    assert not _stopped(peer)

    pty_terminal.get(peer).runtime_busy = False  # the transcript says the turn ended
    sync(focus, [focus, peer], engaged=[peer])
    assert _stopped(peer)


def test_pin_both_keeps_every_visible_pane_running(panes, sync) -> None:
    focus, peer, _ = panes
    _loaded(peer, quiet=600)
    sync(focus, [focus, peer])
    assert _stopped(peer)

    sync(focus, [focus, peer], pin_both=True)

    assert not _stopped(peer)


def test_a_quiet_peer_gives_its_memory_back_after_a_minute(panes, sync, monkeypatch) -> None:
    focus, peer, _ = panes
    reclaimed = []
    monkeypatch.setattr(pty_terminal, "reclaim_memory", lambda tid: reclaimed.append(tid) or 0.0)
    _loaded(peer, quiet=10)
    sync(focus, [focus, peer])
    assert _stopped(peer) and reclaimed == []

    pty_terminal.get(peer).last_activity = time.monotonic() - 61
    sync(focus, [focus, peer])
    assert reclaimed == [peer]


def test_an_off_screen_pane_keeps_the_long_idle_rule(panes, sync) -> None:
    focus, _, hidden = panes
    _loaded(hidden, quiet=30)

    sync(focus, [focus])

    assert not _stopped(hidden), "an off-screen pane slept on the merged-view clock"


def test_clicking_a_sleeping_pane_wakes_it_now(panes, sync) -> None:
    from ui import web

    focus, peer, _ = panes
    _loaded(peer, quiet=600)
    sync(focus, [focus, peer])
    assert _stopped(peer)

    response = web.app.test_client().post(
        f"/api/terminal-runtime/wake/{peer}",
        base_url="http://127.0.0.1:46747", environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.get_json() == {"ok": True, "state": "live"}
    assert not _stopped(peer)


def test_typing_into_a_sleeping_pane_wakes_it(panes, sync) -> None:
    focus, peer, _ = panes
    _loaded(peer, quiet=600)
    sync(focus, [focus, peer])
    assert _stopped(peer)

    assert pty_terminal.write(peer, b"x")

    assert not _stopped(peer) and pty_terminal.get_runtime_state(peer) == "live"


def test_a_new_layout_wakes_a_sleeping_pane_to_redraw_but_the_same_one_does_not(panes, sync) -> None:
    focus, peer, _ = panes
    _loaded(peer, quiet=600)
    sync(focus, [focus, peer])
    pty_terminal.resize(peer, 24, 80)
    assert _stopped(peer), "an unchanged size woke the pane for nothing"

    pty_terminal.resize(peer, 24, 100)

    assert not _stopped(peer), "a sleeping pane was left garbled at its old size"


def test_attaching_a_renderer_wakes_a_stopped_pane(panes, sync) -> None:
    """A stopped process accepts the socket, replays, and then draws nothing."""
    focus, peer, _ = panes
    _loaded(peer, quiet=600)
    sync(focus, [focus, peer])
    assert _stopped(peer)

    assert pty_terminal.attach(peer) is not None

    assert pty_terminal.get_runtime_state(peer) == "live"
    time.sleep(0.2)
    assert not _stopped(peer)
