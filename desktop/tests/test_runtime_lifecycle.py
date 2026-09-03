"""Focused coverage for linked-pane standby safety and intentional exits."""

import signal
import threading

import pytest

app_gtk = pytest.importorskip("desktop.app_gtk")
ChatsApp = app_gtk.ChatsApp


class _Vte:
    def __init__(self):
        self.input_enabled = True
        self.fed = []

    def set_input_enabled(self, enabled):
        self.input_enabled = bool(enabled)

    def feed_child(self, data):
        self.fed.append(data)


class _Harness:
    _runtime_transcript_path = ChatsApp._runtime_transcript_path
    _runtime_is_working = ChatsApp._runtime_is_working
    _runtime_is_protected = ChatsApp._runtime_is_protected
    _runtime_is_background_split_sid = ChatsApp._runtime_is_background_split_sid
    _pause_runtime = ChatsApp._pause_runtime
    _sleep_runtime = ChatsApp._sleep_runtime
    _wake_runtime = ChatsApp._wake_runtime
    _evict_runtime_sids = ChatsApp._evict_runtime_sids
    _sweep_runtime_idle = ChatsApp._sweep_runtime_idle
    notify_runtime_turn_finished = ChatsApp.notify_runtime_turn_finished
    _runtime_turn_finished = ChatsApp._runtime_turn_finished
    runtime_work_reservation = ChatsApp.runtime_work_reservation
    runtime_reserve_work = ChatsApp.runtime_reserve_work
    runtime_release_work = ChatsApp.runtime_release_work
    runtime_interrupt_work = ChatsApp.runtime_interrupt_work
    runtime_context_snapshot = ChatsApp.runtime_context_snapshot
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
        self._runtime_turn_offsets = {}
        self._runtime_leases = {}
        self._runtime_work_reservations = {}
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
        self._runtime_reclaimed = set()
        self._runtime_closed_vtes = set()
        self._runtime_focus_sid = None
        self._runtime_exclusive_generation = 0
        self._split_active = False
        self._split_pinned = False
        self._split_sids = (None, None)
        self._runtime_prewarm_seconds = 3.0
        self._runtime_idle_seconds = 600
        self._sid_agent = {self.sid: "codex"}
        self.signals = []
        self.states = []
        self.painted = []
        self.js = []
        self.working = set()
        self.reclaimed = []

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

    def _reclaim_runtime_memory(self, sid, pid):
        self._runtime_reclaimed.add(sid)
        self.reclaimed.append((sid, pid))

    def _sid_for_vte(self, vte):
        return next((sid for sid, candidate in self._vtes.items() if candidate is vte), None)

    def _eval_js(self, script):
        self.js.append(script)


def test_native_show_refuses_external_workflow_owner(monkeypatch):
    class GuardHarness:
        _show_session = ChatsApp._show_session
        _use_native_vte = True

        def __init__(self):
            self.js = []

        def _eval_js(self, script):
            self.js.append(script)

    monkeypatch.setattr(
        app_gtk.meta_sync,
        "external_runtime_active",
        lambda sid: sid == "workflow-session",
    )
    harness = GuardHarness()

    harness._show_session({"sid": "workflow-session", "agent": "codex"})

    assert len(harness.js) == 1
    assert "onGtkExternalRuntime" in harness.js[0]


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


def test_completed_transcript_clears_stale_manual_submit_guard():
    harness = _Harness()
    harness._runtime_busy.add(harness.sid)
    harness._runtime_turn_offsets[harness.sid] = 100
    harness._runtime_file_paths[harness.sid] = "/tmp/transcript.jsonl"
    harness._runtime_activity_reader = type(
        "_Reader",
        (),
        {"completed_since": lambda *_args: True},
    )()

    assert harness._runtime_is_working(harness.sid) is False
    assert harness.sid not in harness._runtime_busy
    assert harness.sid not in harness._runtime_turn_offsets


def test_unfinished_transcript_keeps_manual_submit_guard():
    harness = _Harness()
    harness._runtime_busy.add(harness.sid)
    harness._runtime_turn_offsets[harness.sid] = 100
    harness._runtime_file_paths[harness.sid] = "/tmp/transcript.jsonl"
    harness._runtime_activity_reader = type(
        "_Reader",
        (),
        {"completed_since": lambda *_args: False},
    )()

    assert harness._runtime_is_working(harness.sid) is True
    assert harness.sid in harness._runtime_busy


def test_usage_reset_wait_bypasses_terminal_output_idle_timer(monkeypatch):
    harness = _Harness()
    harness._sid_agent[harness.sid] = "claude"
    harness._runtime_file_paths[harness.sid] = "/tmp/transcript.jsonl"
    harness._runtime_busy.add(harness.sid)
    harness._runtime_turn_offsets[harness.sid] = 100
    harness._runtime_last_activity[harness.sid] = 99.0
    harness._runtime_activity_reader = type(
        "_Reader",
        (),
        {
            "completed_since": lambda *_args: True,
            "waiting_for_usage_reset": lambda *_args: True,
        },
    )()
    monkeypatch.setattr(app_gtk.time, "monotonic", lambda: 100.0)

    assert harness._sweep_runtime_idle() is True
    assert harness.signals == [(1234, signal.SIGSTOP)]
    assert harness._runtime_state[harness.sid] == "paused"
    assert harness.states[-1] == (
        harness.sid,
        "paused",
        "usage-reset-wait",
    )


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
    harness._runtime_turn_offsets[harness.sid] = 123
    harness._runtime_leases[harness.sid] = 1
    harness._runtime_focus_sid = harness.sid
    harness._runtime_last_output[harness.sid] = 2.0
    harness._runtime_file_paths[harness.sid] = "/tmp/transcript.jsonl"

    harness._migrate_runtime_sid(harness.sid, "session-2")

    assert harness._input_drafts["session-2"] == "unfinished"
    assert "session-2" in harness._runtime_busy
    assert harness._runtime_turn_offsets["session-2"] == 123
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


def test_work_reservation_blocks_manual_input_and_runtime_standby(monkeypatch):
    """A voice job must not share input or lose its owner to standby."""
    harness = _Harness()
    monkeypatch.setattr(app_gtk.meta_sync, "external_runtime_active", lambda _sid: False)
    monkeypatch.setattr(app_gtk.meta_sync, "get_meta", lambda _sid: {})

    assert harness.runtime_reserve_work(harness.sid, "job-1") == (True, "reserved")
    assert harness.vte.input_enabled is False
    assert harness.runtime_work_reservation(harness.sid) == "job-1"
    assert harness._pause_runtime(harness.sid, "idle") is False
    assert harness.signals == []

    assert harness.runtime_release_work(harness.sid, "wrong-job") is False
    assert harness.runtime_release_work(harness.sid, "job-1") is True
    assert harness.vte.input_enabled is True


@pytest.mark.parametrize(
    ("condition", "message"),
    [
        ("busy", "runtime has an active turn"),
        ("draft", "runtime has an unsent draft"),
        ("external", "runtime has an external owner"),
        ("fleet", "fleet worker runtimes are read-only"),
        ("done", "runtime is marked done"),
    ],
)
def test_work_reservation_rejects_unsafe_native_runtime(
    monkeypatch, condition, message
):
    """A stale focus must never override busy, draft, or ownership evidence."""
    harness = _Harness()
    if condition == "busy":
        harness._runtime_busy.add(harness.sid)
    if condition == "draft":
        harness._input_drafts[harness.sid] = "unfinished"
    monkeypatch.setattr(
        app_gtk.meta_sync,
        "external_runtime_active",
        lambda _sid: condition == "external",
    )
    monkeypatch.setattr(
        app_gtk.meta_sync,
        "get_meta",
        lambda _sid: {"fleet_worker": {"run_id": "fleet-1"}}
        if condition == "fleet"
        else ({"done": True} if condition == "done" else {}),
    )

    assert harness.runtime_reserve_work(harness.sid, "job-1") == (False, message)
    assert harness.runtime_work_reservation(harness.sid) is None


def test_work_interrupt_requires_the_exact_native_item(monkeypatch):
    """A cancellation for another job must not touch the user's Codex pane."""
    harness = _Harness()
    monkeypatch.setattr(app_gtk.meta_sync, "external_runtime_active", lambda _sid: False)
    monkeypatch.setattr(app_gtk.meta_sync, "get_meta", lambda _sid: {})
    assert harness.runtime_reserve_work(harness.sid, "job-1")[0] is True

    assert harness.runtime_interrupt_work(harness.sid, "job-2") is False
    assert harness.vte.fed == []
    assert harness.runtime_interrupt_work(harness.sid, "job-1") is True
    assert harness.vte.fed == [b"\x03"]


def test_native_runtime_context_exposes_flags_without_draft_text(monkeypatch):
    """Runtime discovery must not leak the user's unsent prompt contents."""
    harness = _Harness()
    harness._runtime_focus_sid = harness.sid
    harness._split_active = True
    harness._split_sids = (harness.sid, "session-2")
    harness._input_drafts[harness.sid] = "private unfinished prompt"
    harness._runtime_work_reservations[harness.sid] = "job-1"

    context = harness.runtime_context_snapshot()

    assert context["focused_sid"] == harness.sid
    assert context["split_pair"] == [harness.sid, "session-2"]
    entry = next(item for item in context["runtimes"] if item["sid"] == harness.sid)
    assert entry["draft"] is True
    assert entry["reserved"] is True
    assert entry["reservation_item_id"] == "job-1"
    assert "private unfinished prompt" not in repr(context)
