"""Verify the split-kill cleanup holds when one half of a linked claude/codex
split *crashes* (abnormal exit / signal kill), not just on a clean teardown.

A crash does NOT go through ``_kill_session`` (that's the explicit user-initiated
path). Instead VTE fires its ``child-exited`` signal, which is handled by the
closure built in ``ChatsApp._make_child_exit_handler``. This test drives the
*real* production handler (and the real ``_exit_split`` / ``_tear_down_paned`` /
``_detach_vte`` it calls) against real Gtk widgets and a real crashing
subprocess, then asserts the split is dismantled and the surviving pane is
preserved.

We deliberately avoid constructing the full ``ChatsApp`` (its ``__init__``
builds a WebKit2 WebView and loads a URL). Instead we bind the unbound methods
under test onto a lightweight harness that supplies exactly the attributes those
methods touch — so the assertions exercise the shipping code, not a reimpl.

Run:  uv run --with pytest pytest desktop/tests/test_split_crash_kill.py -v
"""

import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import GLib, Gtk, Vte  # noqa: E402

import pytest  # noqa: E402

# Make the `desktop` package importable and import the real app module.
DESKTOP_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = DESKTOP_DIR.parent
for p in (str(REPO_ROOT), str(DESKTOP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Importing app_gtk requires WebKit2 4.1 at module import time. If that typelib
# isn't installed in this environment, the whole desktop UI can't run anyway, so
# skip rather than error.
app_gtk = pytest.importorskip(
    "app_gtk",
    reason="app_gtk import failed (missing WebKit2/Vte typelibs?)",
)
ChatsApp = app_gtk.ChatsApp


def _run_main_loop_until(predicate, timeout_ms=10000):
    """Spin the Gtk main loop until predicate() is true or we time out.

    Returns True if predicate became true, False on timeout. The crash handler
    runs as a Gtk signal callback, so we must service the loop for it to fire.
    """
    loop = GLib.MainLoop()
    state = {"done": False, "timed_out": False}

    def _poll():
        if predicate():
            state["done"] = True
            loop.quit()
            return False  # stop polling
        return True  # keep polling

    def _timeout():
        state["timed_out"] = True
        loop.quit()
        return False

    GLib.timeout_add(20, _poll)
    GLib.timeout_add(timeout_ms, _timeout)
    loop.run()
    return state["done"] and not state["timed_out"]


class _SplitHarness:
    """Minimal stand-in for ChatsApp that owns ONLY the state the crash-cleanup
    code path reads/writes. The methods under test are the real bound methods
    from ChatsApp (attached below), so this is a faithful exercise of shipping
    logic — not a parallel reimplementation."""

    # --- the real production methods we are validating ---
    _make_child_exit_handler = ChatsApp._make_child_exit_handler
    _exit_split = ChatsApp._exit_split
    _tear_down_paned = ChatsApp._tear_down_paned
    _detach_vte = ChatsApp._detach_vte

    def __init__(self):
        self._vtes: dict[str, Vte.Terminal] = {}
        self._vte_pids: dict[str, int] = {}
        self._input_drafts: dict[int, str] = {}
        self._sid_agent: dict[str, str] = {}

        self._stack = Gtk.Stack()
        self._placeholder = Gtk.Box()
        self._stack.add_named(self._placeholder, "__blank__")
        self._stack.set_visible_child_name("__blank__")
        self._stack_hidden = True

        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)

        # A real container so queue_resize() and child mgmt behave like the app.
        self.overlay = Gtk.Overlay()
        self.overlay.add(self._stack)
        self.overlay.add_overlay(self._paned)

        self._split_active = False
        self._split_sids = (None, None)

        # Capture JS exit notifications so we can assert the sidebar marker
        # would be cleared for the crashed sid.
        self.js_exit_calls: list[str] = []

    def _eval_js(self, script: str):
        # The handler emits: window.onGtkCodeExit && window.onGtkCodeExit("<sid>");
        self.js_exit_calls.append(script)

    # --- test helpers (NOT under test) ---
    def _build_vte(self) -> Vte.Terminal:
        return Vte.Terminal()

    def _spawn(self, vte: Vte.Terminal, sid: str, argv: list[str]) -> None:
        """Spawn a real child into the VTE. Mirrors the app's spawn_sync
        fallback path so the PID bookkeeping matches production."""
        ok, pid = vte.spawn_sync(
            Vte.PtyFlags.DEFAULT,
            "/tmp",
            argv,
            [],
            GLib.SpawnFlags.DEFAULT,
            None,
            None,
            None,
        )
        assert ok, f"spawn_sync failed for {sid}"
        self._vte_pids[sid] = pid

    def enter_split(self, left_sid: str, right_sid: str,
                    left_argv: list[str], right_argv: list[str],
                    left_agent="claude", right_agent="codex"):
        """Mount two real VTEs side-by-side in the paned, each running a real
        child process — the same shape as a live linked claude/codex split."""
        left = self._build_vte()
        right = self._build_vte()
        self._vtes[left_sid] = left
        self._vtes[right_sid] = right
        self._sid_agent[left_sid] = left_agent
        self._sid_agent[right_sid] = right_agent
        # Wire the REAL child-exited handler — this is the crash entry point.
        left.connect("child-exited", self._make_child_exit_handler(left_sid))
        right.connect("child-exited", self._make_child_exit_handler(right_sid))

        self._paned.pack1(left, True, True)
        self._paned.pack2(right, True, True)
        left.show()
        right.show()

        self._split_active = True
        self._split_sids = (left_sid, right_sid)
        self._stack.set_visible_child_name("__blank__")
        self._stack_hidden = True

        self._spawn(left, left_sid, left_argv)
        self._spawn(right, right_sid, right_argv)
        return left, right


@pytest.fixture
def harness():
    return _SplitHarness()


# A long-lived process keeps the survivor pane "alive" during the test, and a
# crashing command exercises the abnormal-exit path. `sleep 30` is plenty for
# the loop iterations; the test tears down well before it would matter.
LONG_LIVED = ["/bin/sh", "-c", "sleep 30"]
CRASH_NONZERO = ["/bin/sh", "-c", "exit 42"]      # nonzero exit (e.g. codex panic)
CRASH_SIGNAL = ["/bin/sh", "-c", "kill -9 $$"]    # SIGKILL — true abnormal crash


@pytest.mark.parametrize("crash_argv", [CRASH_NONZERO, CRASH_SIGNAL],
                         ids=["nonzero-exit", "sigkill-crash"])
def test_codex_crash_dismantles_split_and_preserves_survivor(harness, crash_argv):
    left_sid = "claude-survivor-sid"
    right_sid = "codex-crashing-sid"

    left_vte, right_vte = harness.enter_split(
        left_sid, right_sid,
        left_argv=LONG_LIVED, right_argv=crash_argv,
        left_agent="claude", right_agent="codex",
    )

    # Give the crashing (codex) half an input draft so we can assert it's wiped.
    harness._input_drafts[id(right_vte)] = "half-typed prompt"

    # Sanity: split is genuinely active and both panes are children of the paned.
    assert harness._split_active is True
    assert harness._split_sids == (left_sid, right_sid)
    assert right_vte in harness._paned.get_children()
    assert left_vte in harness._paned.get_children()

    # Wait for the crash to be observed: the handler pops the crashed sid out of
    # _vtes. If the handler never fires (or never cleans up), this times out and
    # the assertion below fails — which is exactly the regression we'd catch.
    crashed = _run_main_loop_until(lambda: right_sid not in harness._vtes)
    assert crashed, "codex crash did not trigger child-exited cleanup within timeout"

    # --- The split must be fully dismantled ---
    assert harness._split_active is False, "split left active after codex crash"
    assert harness._split_sids == (None, None), "split sids not cleared after crash"
    assert right_vte not in harness._paned.get_children(), \
        "crashed codex VTE still parented in paned"

    # --- The crashed pane's bookkeeping must be cleaned up ---
    assert right_sid not in harness._vtes, "crashed sid not removed from _vtes"
    assert right_sid not in harness._vte_pids, "crashed sid pid not cleared"
    assert id(right_vte) not in harness._input_drafts, "crashed pane draft not cleared"

    # --- JS was told to clear the sidebar marker for the crashed sid ---
    assert any(right_sid in call for call in harness.js_exit_calls), \
        "onGtkCodeExit not emitted for crashed sid"

    # --- The SURVIVOR must NOT be killed: it is reparented back to the stack ---
    assert left_sid in harness._vtes, "survivor pane was dropped from _vtes"
    assert harness._vtes[left_sid] is left_vte
    assert harness._stack.get_child_by_name(left_sid) is left_vte, \
        "survivor pane was not returned to the stack (would vanish from the UI)"
    # Survivor's child process is still alive.
    surv_pid = harness._vte_pids.get(left_sid)
    assert surv_pid is not None, "survivor lost its pid"
    try:
        os.kill(surv_pid, 0)
        survivor_alive = True
    except OSError:
        survivor_alive = False
    assert survivor_alive, "survivor process was killed by the crash cleanup"

    # Cleanup: terminate the long-lived survivor so we don't leak it.
    try:
        os.kill(surv_pid, 9)
    except OSError:
        pass


def test_crash_outside_split_does_not_blow_up(harness):
    """A codex crash while NOT in a split should still clean up its own VTE and
    notify JS, without touching split state (which is already inactive)."""
    sid = "lonely-codex-sid"
    vte = harness._build_vte()
    harness._vtes[sid] = vte
    harness._sid_agent[sid] = "codex"
    harness._stack.add_named(vte, sid)
    vte.show()
    vte.connect("child-exited", harness._make_child_exit_handler(sid))
    harness._spawn(vte, sid, CRASH_NONZERO)

    crashed = _run_main_loop_until(lambda: sid not in harness._vtes)
    assert crashed, "standalone codex crash did not clean up within timeout"

    assert harness._split_active is False
    assert harness._split_sids == (None, None)
    assert sid not in harness._vte_pids
    assert any(sid in call for call in harness.js_exit_calls)
