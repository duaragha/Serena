"""Focused coverage for linked-pane standby safety and intentional exits."""

import signal
import threading

import pytest


app_gtk = pytest.importorskip("desktop.app_gtk")
ChatsApp = app_gtk.ChatsApp


class _Vte:
    pass


class _Harness:
    _runtime_is_working = ChatsApp._runtime_is_working
    _runtime_is_protected = ChatsApp._runtime_is_protected
    _runtime_is_background_split_sid = ChatsApp._runtime_is_background_split_sid
    _pause_runtime = ChatsApp._pause_runtime
    _sleep_runtime = ChatsApp._sleep_runtime
    _wake_runtime = ChatsApp._wake_runtime
    _evict_runtime_sids = ChatsApp._evict_runtime_sids
    notify_runtime_turn_finished = ChatsApp.notify_runtime_turn_finished
    _runtime_turn_finished = ChatsApp._runtime_turn_finished
    _make_child_exit_handler = ChatsApp._make_child_exit_handler
    _migrate_runtime_sid = ChatsApp._migrate_runtime_sid
    _schedule_exclusive_runtime = ChatsApp._schedule_exclusive_runtime

    def __init__(self):
        self.sid = "session-1"
        self.vte = _Vte()
        self._vtes = {self.sid: self.vte}
        self._vte_pids = {self.sid: 1234}
        self._input_drafts = {}
        self._runtime_busy = set()
        self._runtime_leases = {}
        self._runtime_lock = threading.Lock()
        self._runtime_state = {self.sid: "live"}
        self._runtime_cwd = {self.sid: "/tmp"}
        self._runtime_last_activity = {self.sid: 1.0}
        self._runtime_last_output = {}
        self._runtime_file_paths = {}
        self._runtime_started_at = {self.sid: 1.0}
        self._runtime_stop_reasons = {}
        self._runtime_sleep_after_wake = {}
        self._runtime_wake_after_stop = set()
        self._runtime_closed_vtes = set()
        self._runtime_focus_sid = None
        self._runtime_exclusive_generation = 0
        self._split_active = False
        self._split_pinned = False
        self._split_sids = (None, None)
        self._runtime_prewarm_seconds = 3.0
        self._sid_agent = {self.sid: "codex"}
        self.signals = []
        self.states = []
        self.painted = []
        self.js = []
        self.working = set()

    def _runtime_has_active_turn(self, sid):
        return sid in self.working

    def _runtime_pid_alive(self, pid):
        return pid == 1234

    def _terminate_runtime_pid(self, pid, sig):
        self.signals.append((pid, sig))

    def _set_runtime_state(self, sid, state, reason=""):
        self._runtime_state[sid] = state
        self.states.append((sid, state, reason))

    def _paint_runtime_asleep(self, sid):
        self.painted.append(sid)

    def _sid_for_vte(self, vte):
        return next((sid for sid, candidate in self._vtes.items() if candidate is vte), None)

    def _eval_js(self, script):
        self.js.append(script)


@pytest.mark.parametrize("protection", ["busy", "lease", "draft"])
def test_sleep_refuses_to_interrupt_protected_sessions(monkeypatch, protection):
    harness = _Harness()
    monkeypatch.setattr(app_gtk.GLib, "timeout_add", lambda *_args: 1)
    if protection == "busy":
        harness._runtime_busy.add(harness.sid)
    elif protection == "lease":
        harness._runtime_leases[harness.sid] = 1
    else:
        harness._input_drafts[harness.sid] = "unsent"

    assert harness._sleep_runtime(harness.sid, "test") is False
    assert harness.signals == []
    assert harness._runtime_state[harness.sid] == "live"


def test_unprotected_sleep_signals_the_whole_runtime(monkeypatch):
    harness = _Harness()
    monkeypatch.setattr(app_gtk.GLib, "timeout_add", lambda *_args: 1)

    assert harness._sleep_runtime(harness.sid, "sibling-focused") is True
    assert harness.signals == [(1234, signal.SIGTERM)]
    assert harness._runtime_stop_reasons[harness.sid] == "sibling-focused"
    assert harness._runtime_state[harness.sid] == "sleeping"


def test_unprotected_standby_stops_without_destroying_the_runtime():
    harness = _Harness()

    assert harness._pause_runtime(harness.sid, "sibling-focused") is True
    assert harness.signals == [(1234, signal.SIGSTOP)]
    assert harness._vte_pids[harness.sid] == 1234
    assert harness._runtime_state[harness.sid] == "paused"


def test_hot_standby_wakes_with_sigcont_in_place():
    harness = _Harness()
    harness._runtime_state[harness.sid] = "paused"

    assert harness._wake_runtime(harness.sid, "clicked", focus=False) is True
    assert harness.signals == [(1234, signal.SIGCONT)]
    assert harness._vte_pids[harness.sid] == 1234
    assert harness._runtime_state[harness.sid] == "live"


def test_working_runtime_cannot_remain_paused():
    harness = _Harness()
    harness._runtime_state[harness.sid] = "paused"
    harness.working.add(harness.sid)

    assert harness._pause_runtime(harness.sid, "sibling-focused") is False
    assert harness.signals == [(1234, signal.SIGCONT)]
    assert harness._runtime_state[harness.sid] == "live"


def test_split_focus_prefers_the_only_working_runtime():
    assert ChatsApp._select_split_focus(
        "claude-session",
        ("claude-session", "codex-session"),
        {"codex-session"},
    ) == "codex-session"


def test_split_focus_keeps_requested_side_when_both_are_working():
    assert ChatsApp._select_split_focus(
        "claude-session",
        ("claude-session", "codex-session"),
        {"claude-session", "codex-session"},
    ) == "claude-session"


def test_clicking_idle_sibling_keeps_working_runtime_live(monkeypatch):
    harness = _Harness()
    focused_sid = "focused-session"
    harness._vte_pids[focused_sid] = 1234
    harness._runtime_state[focused_sid] = "live"
    harness._runtime_started_at[focused_sid] = 0.0
    harness._split_active = True
    harness._split_sids = (focused_sid, harness.sid)
    harness._runtime_focus_sid = focused_sid
    harness.working.add(harness.sid)
    scheduled = []
    monkeypatch.setattr(
        app_gtk.GLib,
        "timeout_add",
        lambda delay, callback: scheduled.append((delay, callback)) or 1,
    )

    harness._schedule_exclusive_runtime(focused_sid)
    scheduled.pop(0)[1]()

    assert harness.signals == []
    assert harness._runtime_state[harness.sid] == "live"
    assert harness.states == []
    assert scheduled[0][0] == 1000


def test_background_runtime_enters_standby_after_work_finishes(monkeypatch):
    harness = _Harness()
    focused_sid = "focused-session"
    harness._vte_pids[focused_sid] = 1234
    harness._runtime_state[focused_sid] = "live"
    harness._runtime_started_at[focused_sid] = 0.0
    harness._split_active = True
    harness._split_sids = (focused_sid, harness.sid)
    harness._runtime_focus_sid = focused_sid
    harness.working.add(harness.sid)
    scheduled = []
    monkeypatch.setattr(
        app_gtk.GLib,
        "timeout_add",
        lambda delay, callback: scheduled.append((delay, callback)) or 1,
    )

    harness._schedule_exclusive_runtime(focused_sid)
    scheduled.pop(0)[1]()
    harness.working.clear()
    scheduled.pop(0)[1]()

    assert harness.signals == [(1234, signal.SIGSTOP)]
    assert harness._runtime_state[harness.sid] == "paused"


def test_navigation_keeps_previous_runtime_alive():
    harness = _Harness()

    harness._evict_runtime_sids([harness.sid])

    assert harness.signals == []
    assert harness._runtime_state[harness.sid] == "live"
    assert harness._runtime_last_activity[harness.sid] > 1.0


def test_sleep_requested_during_spawn_is_queued_without_losing_the_request(monkeypatch):
    harness = _Harness()
    harness._runtime_state[harness.sid] = "waking"
    harness._vte_pids.clear()
    monkeypatch.setattr(app_gtk.GLib, "timeout_add", lambda *_args: 1)

    assert harness._sleep_runtime(harness.sid, "view-changed") is True
    assert harness.signals == []
    assert harness._runtime_sleep_after_wake[harness.sid] == "view-changed"
    assert harness._runtime_state[harness.sid] == "sleeping"


def test_intentional_child_exit_keeps_the_vte_and_becomes_asleep():
    harness = _Harness()
    harness._runtime_stop_reasons[harness.sid] = "view-changed"

    harness._make_child_exit_handler(harness.sid)(harness.vte, 0)

    assert harness.sid in harness._vtes
    assert harness.sid not in harness._vte_pids
    assert harness._runtime_state[harness.sid] == "asleep"
    assert harness.painted == [harness.sid]
    assert harness.js == []


def test_sid_migration_preserves_draft_state_and_focus():
    harness = _Harness()
    harness._input_drafts[harness.sid] = "unfinished"
    harness._runtime_busy.add(harness.sid)
    harness._runtime_leases[harness.sid] = 1
    harness._runtime_focus_sid = harness.sid
    harness._runtime_last_output[harness.sid] = 2.0
    harness._runtime_file_paths[harness.sid] = "/tmp/transcript.jsonl"

    harness._migrate_runtime_sid(harness.sid, "session-2")

    assert harness._input_drafts["session-2"] == "unfinished"
    assert "session-2" in harness._runtime_busy
    assert harness._runtime_leases["session-2"] == 1
    assert harness._runtime_focus_sid == "session-2"
    assert harness._runtime_last_output["session-2"] == 2.0
    assert harness._runtime_file_paths["session-2"] == "/tmp/transcript.jsonl"


def test_completed_visible_sibling_turn_schedules_hot_standby(monkeypatch):
    harness = _Harness()
    harness._runtime_busy.add(harness.sid)
    harness._split_active = True
    harness._split_sids = ("focused-session", harness.sid)
    harness._runtime_focus_sid = "focused-session"
    scheduled = []
    monkeypatch.setattr(
        app_gtk.GLib,
        "idle_add",
        lambda callback, *args: callback(*args),
    )
    monkeypatch.setattr(
        app_gtk.GLib,
        "timeout_add",
        lambda delay, callback: scheduled.append((delay, callback)) or 1,
    )

    harness.notify_runtime_turn_finished(harness.sid)

    assert harness.sid not in harness._runtime_busy
    assert len(scheduled) == 1
    assert scheduled[0][0] == 150

    scheduled[0][1]()
    assert harness.signals == [(1234, signal.SIGSTOP)]
    assert harness._runtime_state[harness.sid] == "paused"


def test_completed_turn_outside_current_chat_stays_live(monkeypatch):
    harness = _Harness()
    harness._runtime_busy.add(harness.sid)
    scheduled = []
    monkeypatch.setattr(
        app_gtk.GLib,
        "idle_add",
        lambda callback, *args: callback(*args),
    )
    monkeypatch.setattr(
        app_gtk.GLib,
        "timeout_add",
        lambda delay, callback: scheduled.append((delay, callback)) or 1,
    )

    harness.notify_runtime_turn_finished(harness.sid)

    assert harness.sid not in harness._runtime_busy
    assert scheduled == []
    assert harness.signals == []
