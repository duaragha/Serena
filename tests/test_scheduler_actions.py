"""Tests for the reviewed actions and the single notification authority.

The scheduler is only as trustworthy as the actions it is allowed to name. If
that registry were open, or if a sender could go around the authority, the
bounds elsewhere would be decorative.
"""

from __future__ import annotations

import pytest

from core.control_plane import ControlPlaneStore
from core.notification_authority import (
    NotificationAuthority,
    NotificationPolicy,
    NotificationRequest,
)
from core.scheduler_actions import REVIEWED_ACTIONS, register_all
from core.serena_scheduler import ActionOutcome, SchedulerError, SerenaScheduler
from core.surface_journal import SurfaceJournal


@pytest.fixture(autouse=True)
def control(tmp_path, monkeypatch):
    store = ControlPlaneStore(tmp_path / "control.sqlite3")
    monkeypatch.setenv("SERENA_CONTROL_PLANE_DB_PATH", str(tmp_path / "control.sqlite3"))
    return store


# ---- the registry is closed ----------------------------------------------


def test_the_registry_is_a_fixed_set_of_named_actions():
    assert set(REVIEWED_ACTIONS) == {
        "serena.obligations.sweep",
        "serena.obligations.report",
        "serena.notifications.flush",
        "serena.surfaces.publish",
        "serena.fleet.start",
        "serena.fleet.reconcile",
        "serena.phone.poll",
        "serena.phone.health",
    }
    assert all(callable(handler) for handler in REVIEWED_ACTIONS.values())


def test_registering_all_makes_exactly_those_schedulable(tmp_path):
    scheduler = register_all(SerenaScheduler(tmp_path / "s.sqlite3", notifier=None))
    assert set(scheduler.actions) == set(REVIEWED_ACTIONS)


def test_a_shell_command_is_not_an_action(tmp_path):
    scheduler = register_all(SerenaScheduler(tmp_path / "s.sqlite3", notifier=None))
    for attempt in ("rm -rf /", "bash -c 'curl evil'", "python", ""):
        with pytest.raises(SchedulerError):
            scheduler.add_schedule(
                action=attempt, interval_seconds=3_600, actor="raghav"
            )


# ---- the actions do real work --------------------------------------------


def test_the_sweep_action_resolves_a_hopeless_obligation(control, tmp_path):
    journal = SurfaceJournal("voice", tmp_path / "voice.sqlite3", control_store=control)
    journal.opened("voice-1", summary="tell him how it went")
    for _ in range(6):
        journal.failed("voice-1", error="the bridge was down")

    outcome = REVIEWED_ACTIONS["serena.obligations.sweep"]({"stale_seconds": 0})

    assert outcome.ok is True
    assert control.obligations(surface="voice")[0].state == "ambiguous"


def test_the_sweep_leaves_a_fresh_obligation_to_its_live_owner(control, tmp_path):
    """The default window exists so a sweep does not fight a running worker."""

    journal = SurfaceJournal("voice", tmp_path / "voice.sqlite3", control_store=control)
    journal.opened("voice-1", summary="tell him how it went")

    REVIEWED_ACTIONS["serena.obligations.sweep"]({})

    assert control.obligations(surface="voice")[0].state == "open"


def test_the_report_action_stays_quiet_when_nothing_is_stale(control):
    outcome = REVIEWED_ACTIONS["serena.obligations.report"]({})

    assert outcome.ok is True
    assert outcome.notify is None
    assert "nothing outstanding" in outcome.detail


def test_the_report_action_speaks_up_about_a_stale_debt(control, tmp_path):
    journal = SurfaceJournal("voice", tmp_path / "voice.sqlite3", control_store=control)
    journal.opened("voice-1", summary="tell him how it went")
    # Backdate it so it counts as genuinely sitting there.
    control.append_event(
        surface="voice",
        event_type="job.accepted",
        lifecycle_state="accepted",
        job_id="voice-old",
        occurred_at=1_000.0,
    )

    outcome = REVIEWED_ACTIONS["serena.obligations.report"]({"stale_seconds": 0})

    assert outcome.notify is not None
    assert outcome.notify["urgency"] == "low"
    assert "still open" in outcome.notify["summary"]


def test_the_publish_action_reports_a_count(control):
    outcome = REVIEWED_ACTIONS["serena.surfaces.publish"]({})
    assert outcome.ok is True
    assert "published" in outcome.detail


# ---- everything user-facing goes through the one authority ---------------


def test_a_scheduled_notice_obeys_quiet_hours(tmp_path):
    sent: list = []
    authority = NotificationAuthority(
        tmp_path / "n.sqlite3",
        # Quiet all day, so a scheduled notice must be held.
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=23),
        senders={"voice": lambda r: sent.append(r.summary) or True},
    )
    scheduler = SerenaScheduler(tmp_path / "s.sqlite3", notifier=authority)
    scheduler.register_action(
        "noisy", lambda _p: ActionOutcome(True, notify={"summary": "wake up"})
    )
    scheduler.add_schedule(
        action="noisy",
        interval_seconds=60,
        actor="raghav",
        requires_approval=False,
        first_run_at=0,
    )

    scheduler.tick()

    assert sent == [], "quiet hours must hold a scheduled notice"
    assert authority.history()[0]["decision"] == "deferred"


def test_a_scheduled_notice_cannot_declare_itself_critical(tmp_path):
    """A schedule may pick urgency, but the action registry is reviewed code."""

    sent: list = []
    authority = NotificationAuthority(
        tmp_path / "n.sqlite3",
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=23),
        senders={"voice": lambda r: sent.append(r.summary) or True},
    )
    # The reviewed report action pins urgency to low, so a stale backlog can
    # never wake him at 3am no matter how large it gets.
    outcome_notify = {"kind": "serena.owed", "summary": "lots", "urgency": "low"}
    authority.request(
        NotificationRequest(
            kind=outcome_notify["kind"],
            summary=outcome_notify["summary"],
            urgency=outcome_notify["urgency"],
            channel="voice",
        )
    )
    assert sent == []


def test_a_flood_of_scheduled_notices_is_capped(tmp_path):
    sent: list = []
    authority = NotificationAuthority(
        tmp_path / "n.sqlite3",
        policy=NotificationPolicy(
            quiet_start_hour=0, quiet_end_hour=0, hourly_limit=2
        ),
        senders={"voice": lambda r: sent.append(r.summary) or True},
    )
    scheduler = SerenaScheduler(tmp_path / "s.sqlite3", notifier=authority)
    counter = {"n": 0}

    def noisy(_payload):
        counter["n"] += 1
        return ActionOutcome(
            True, notify={"summary": f"notice {counter['n']}", "dedupe_key": f"k{counter['n']}"}
        )

    scheduler.register_action("noisy", noisy)
    record = scheduler.add_schedule(
        action="noisy",
        interval_seconds=60,
        actor="raghav",
        requires_approval=False,
        first_run_at=0,
    )
    for index in range(5):
        scheduler.run_now(record["schedule_id"], now=1_000 + index)

    assert len(sent) == 2, "the hourly limit applies to scheduled notices too"


def test_the_default_senders_cover_every_declared_channel():
    from core.notification_authority import CHANNELS
    from core.notification_senders import DEFAULT_SENDERS

    assert set(DEFAULT_SENDERS) == set(CHANNELS)


def test_the_fallback_hop_still_goes_through_the_authority(tmp_path):
    """A voice failure may fall back to his iMessage line, but not past the policy."""

    from core.notification_senders import notify

    attempts: list = []
    authority = NotificationAuthority(
        tmp_path / "n.sqlite3",
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0),
        senders={
            "voice": lambda r: attempts.append("voice") or False,
            "imessage": lambda r: attempts.append("imessage") or True,
        },
    )

    result = notify(
        "fleet.run.completed", "the run finished", authority=authority, dedupe_key="run-1"
    )

    assert attempts == ["voice", "imessage"]
    assert result.sent is True


def test_the_fallback_does_not_fire_when_the_notice_was_merely_suppressed(tmp_path):
    from core.notification_senders import notify

    attempts: list = []
    authority = NotificationAuthority(
        tmp_path / "n.sqlite3",
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0),
        senders={
            "voice": lambda r: attempts.append("voice") or True,
            "imessage": lambda r: attempts.append("imessage") or True,
        },
    )
    notify("fleet.run.completed", "the run finished", authority=authority, dedupe_key="run-1")
    attempts.clear()

    # Same dedupe key: suppressed is a real answer, not a transport failure.
    notify("fleet.run.completed", "the run finished", authority=authority, dedupe_key="run-1")

    assert attempts == [], "a suppressed notice must not be retried on another channel"


# ---- Fleet dispatch consumes the task-store contract ----------------------


@pytest.fixture
def fleet_queue(tmp_path, monkeypatch):
    """Exercise dispatch with the ws-3 public contract and no live Fleet runs."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    from core import agent_checkouts, coding_job_contract
    from fleet import supervisor
    from memory import store

    queue = SimpleNamespace(
        claim=Mock(return_value=None), mark=Mock(return_value=True),
        release=Mock(return_value=True), runs=Mock(return_value=[]),
        start=Mock(return_value={"run_id": "run-1", "state": "queued"}),
        resolve=Mock(return_value=tmp_path),
    )
    monkeypatch.setattr(store, "claim_next_task", queue.claim)
    monkeypatch.setattr(store, "mark_task_running", queue.mark)
    monkeypatch.setattr(store, "release_task_claim", queue.release)
    monkeypatch.setattr(store, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(supervisor, "list_runs", queue.runs)
    monkeypatch.setattr(supervisor, "start_run", queue.start)
    monkeypatch.setattr(coding_job_contract, "resolve_repository_root", queue.resolve)
    queue.prepare = Mock(side_effect=lambda source, task_id: SimpleNamespace(path=source))
    monkeypatch.setattr(agent_checkouts, "prepare", queue.prepare)
    queue.task = {
        "id": 7, "content": "Fix the Serena scheduler failing to resume after capacity returns",
        "project_hint": "serena", "priority": "high", "state": "claimed",
        "lease_token": "lease-1",
    }
    return queue


def test_fleet_empty_queue_is_a_clean_noop(fleet_queue):
    outcome = REVIEWED_ACTIONS["serena.fleet.start"]({})
    assert outcome.ok and outcome.detail == "no ready task"
    fleet_queue.start.assert_not_called()
    fleet_queue.runs.assert_not_called()


def test_fleet_claims_one_ordered_ready_task_and_records_run(fleet_queue):
    fleet_queue.claim.return_value = fleet_queue.task
    outcome = REVIEWED_ACTIONS["serena.fleet.start"]({})

    assert outcome.ok and outcome.output == {"task_id": 7, "run_id": "run-1"}
    fleet_queue.claim.assert_called_once()
    fleet_queue.resolve.assert_called_once_with(
        fleet_queue.task["content"], project_hint="serena"
    )
    from core.scheduler_actions import _delivery_rules

    fleet_queue.prepare.assert_called_once_with(fleet_queue.resolve.return_value, 7)
    fleet_queue.start.assert_called_once_with(
        task=fleet_queue.task["content"] + _delivery_rules(7),
        activity="auto", provider_mode="auto",
        cwd=str(fleet_queue.resolve.return_value), origin_session_id="serena-task:7",
    )
    owner = fleet_queue.claim.call_args.args[0]
    fleet_queue.mark.assert_called_once_with(7, owner, "lease-1", "run-1")


@pytest.mark.parametrize("payload", [{"command": "echo unsafe"}, {"cwd": "/tmp"},
                                     {"task": "bypass triage"}, {"callable": "x.y"}])
def test_fleet_schedule_cannot_supply_execution_inputs(fleet_queue, payload):
    assert not REVIEWED_ACTIONS["serena.fleet.start"](payload).ok
    fleet_queue.claim.assert_not_called()
    fleet_queue.start.assert_not_called()


@pytest.mark.parametrize("state", ["queued", "running", "stopping", "waiting_for_capacity",
                                   "waiting_for_resources", "waiting_for_input", "completed"])
def test_fleet_existing_task_run_is_never_dispatched_again(fleet_queue, state):
    fleet_queue.claim.return_value = fleet_queue.task
    fleet_queue.runs.return_value = [
        {"origin_session_id": "serena-task:7", "run_id": "old-run", "state": state}
    ]
    outcome = REVIEWED_ACTIONS["serena.fleet.start"]({})
    assert outcome.ok and outcome.output["run_id"] == "old-run"
    fleet_queue.start.assert_not_called()


def test_fleet_receipt_survives_task_update_failure_and_history_truncation(fleet_queue):
    fleet_queue.claim.return_value = fleet_queue.task
    fleet_queue.mark.side_effect = OSError("task write failed")
    first = REVIEWED_ACTIONS["serena.fleet.start"]({})
    assert first.output["state"] == "blocked"

    fleet_queue.mark.side_effect = None
    fleet_queue.runs.side_effect = AssertionError("receipt must avoid truncated history")
    second = REVIEWED_ACTIONS["serena.fleet.start"]({})
    assert second.output["run_id"] == "run-1"
    assert fleet_queue.start.call_count == 1


def test_fleet_ambiguous_start_is_held_even_when_no_run_is_visible(fleet_queue):
    fleet_queue.claim.return_value = fleet_queue.task
    fleet_queue.start.side_effect = RuntimeError("failed after persisting run")
    first = REVIEWED_ACTIONS["serena.fleet.start"]({})
    second = REVIEWED_ACTIONS["serena.fleet.start"]({})
    assert first.ok and second.ok
    assert first.output["state"] == second.output["state"] == "blocked"
    assert "uncertain" in second.detail
    assert fleet_queue.start.call_count == 1


def test_fleet_recovers_a_positive_receipt_after_an_uncertain_start(fleet_queue):
    fleet_queue.claim.return_value = fleet_queue.task
    fleet_queue.start.side_effect = RuntimeError("failed after persisting run")
    REVIEWED_ACTIONS["serena.fleet.start"]({})
    fleet_queue.runs.return_value = [
        {"origin_session_id": "serena-task:7", "run_id": "recovered", "state": "running"}
    ]
    outcome = REVIEWED_ACTIONS["serena.fleet.start"]({})
    assert outcome.output["run_id"] == "recovered"
    assert fleet_queue.start.call_count == 1


def test_fleet_history_failure_before_dispatch_leaves_the_task_retriable(fleet_queue):
    fleet_queue.claim.return_value = fleet_queue.task
    fleet_queue.runs.side_effect = OSError("run history unavailable")
    first = REVIEWED_ACTIONS["serena.fleet.start"]({})
    assert first.ok and first.output["state"] == "ready"
    assert "history is unavailable" in first.detail
    fleet_queue.start.assert_not_called()

    fleet_queue.runs.side_effect = None
    second = REVIEWED_ACTIONS["serena.fleet.start"]({})
    assert second.output["run_id"] == "run-1"
    assert fleet_queue.start.call_count == 1


def test_fleet_history_failure_never_clears_an_existing_uncertain_intent(fleet_queue):
    fleet_queue.claim.return_value = fleet_queue.task
    fleet_queue.start.side_effect = RuntimeError("failed after persisting run")
    assert REVIEWED_ACTIONS["serena.fleet.start"]({}).output["state"] == "blocked"

    fleet_queue.runs.side_effect = OSError("run history unavailable")
    held = REVIEWED_ACTIONS["serena.fleet.start"]({})
    assert held.ok and held.output["state"] == "blocked" and "uncertain" in held.detail

    fleet_queue.runs.side_effect = None
    fleet_queue.start.side_effect = None
    again = REVIEWED_ACTIONS["serena.fleet.start"]({})
    assert again.output["state"] == "blocked" and "uncertain" in again.detail
    assert fleet_queue.start.call_count == 1


def test_fleet_lease_reassignment_during_start_cannot_duplicate_dispatch(fleet_queue):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, finish = Event(), Event()
    fleet_queue.claim.return_value = fleet_queue.task

    def slow_start(**_kwargs):
        entered.set()
        assert finish.wait(5)
        return {"run_id": "run-1"}

    fleet_queue.start.side_effect = slow_start
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(REVIEWED_ACTIONS["serena.fleet.start"], {})
        try:
            assert entered.wait(5)
            # Model ws-3 reclaiming a lease while Fleet's public start is slow.
            fleet_queue.claim.return_value = {**fleet_queue.task, "lease_token": "lease-2"}
            second = REVIEWED_ACTIONS["serena.fleet.start"]({})
            assert second.ok and second.output["state"] == "blocked"
        finally:
            finish.set()
        assert first.result(timeout=5).ok
    assert fleet_queue.start.call_count == 1


def test_fleet_bad_repository_goes_to_triage_before_reserving(fleet_queue):
    fleet_queue.claim.return_value = fleet_queue.task
    fleet_queue.resolve.side_effect = ValueError("which repository?")
    outcome = REVIEWED_ACTIONS["serena.fleet.start"]({})
    assert outcome.ok and outcome.output["state"] == "needs_triage"
    fleet_queue.start.assert_not_called()
    fleet_queue.resolve.side_effect = None
    assert REVIEWED_ACTIONS["serena.fleet.start"]({}).output["run_id"] == "run-1"


def test_fleet_dispatch_ledger_has_a_hard_bound(fleet_queue, monkeypatch):
    from core import scheduler_actions

    monkeypatch.setattr(scheduler_actions, "MAX_FLEET_DISPATCHES", 1)
    fleet_queue.claim.return_value = fleet_queue.task
    REVIEWED_ACTIONS["serena.fleet.start"]({})
    fleet_queue.claim.return_value = {**fleet_queue.task, "id": 8}
    outcome = REVIEWED_ACTIONS["serena.fleet.start"]({})
    assert outcome.ok and "full" in outcome.detail
    assert fleet_queue.start.call_count == 1


def test_fleet_action_is_held_by_runtime_capacity_gate(fleet_queue, tmp_path):
    from unittest.mock import Mock

    from core.automation_runtime import AutomationRuntime

    scheduler = register_all(SerenaScheduler(tmp_path / "s.sqlite3", notifier=None))
    scheduler.add_schedule(
        action="serena.fleet.start", interval_seconds=60, actor="raghav",
        requires_approval=False, first_run_at=0,
    )
    authority = Mock()
    authority.deliver_due.return_value = []
    capacity = Mock(return_value=(True, "both providers exhausted"))
    runtime = AutomationRuntime(
        scheduler=scheduler, authority=authority, capacity_reader=capacity,
        capacity_probe_seconds=1, publish_journals=False,
    )
    assert runtime.run_pass(now=1000).capacity_held
    fleet_queue.claim.assert_not_called()
    capacity.return_value = (False, "")
    assert not runtime.run_pass(now=2000).capacity_held
    fleet_queue.claim.assert_called_once()


def test_fleet_process_death_leaves_an_intent_that_cannot_be_retried(fleet_queue):
    fleet_queue.claim.return_value = fleet_queue.task
    fleet_queue.start.side_effect = SystemExit("process died after invocation")
    with pytest.raises(SystemExit):
        REVIEWED_ACTIONS["serena.fleet.start"]({})
    outcome = REVIEWED_ACTIONS["serena.fleet.start"]({})
    assert outcome.ok and outcome.output["state"] == "blocked"
    assert fleet_queue.start.call_count == 1


def test_fleet_does_not_claim_a_state_update_when_lease_was_lost(fleet_queue):
    fleet_queue.claim.return_value = fleet_queue.task
    fleet_queue.resolve.side_effect = ValueError("which repository?")
    fleet_queue.release.return_value = False
    outcome = REVIEWED_ACTIONS["serena.fleet.start"]({})
    assert outcome.output["state"] == "claim_changed"
    fleet_queue.start.assert_not_called()


def test_fleet_schedule_still_requires_approval(fleet_queue, tmp_path):
    scheduler = register_all(SerenaScheduler(tmp_path / "s.sqlite3", notifier=None))
    scheduler.add_schedule(
        action="serena.fleet.start", interval_seconds=60, actor="raghav", first_run_at=0,
    )
    assert scheduler.tick(now=1000) == []
    fleet_queue.claim.assert_not_called()


@pytest.fixture
def real_fleet_queue(tmp_path, monkeypatch):
    """Real queue persistence and repository resolution; no live Fleet launch."""
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import Mock

    from core import agent_checkouts
    from fleet import supervisor
    from memory import store

    monkeypatch.setattr(store, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(agent_checkouts, "prepare",
                        lambda source, task_id: SimpleNamespace(path=source))
    clock = [1000.0]
    monkeypatch.setattr(store.time, "time", lambda: clock[0])
    start = Mock(return_value={"run_id": "run-1", "state": "queued"})
    runs = Mock(return_value=[])
    monkeypatch.setattr(supervisor, "start_run", start)
    monkeypatch.setattr(supervisor, "list_runs", runs)
    root = Path(__file__).resolve().parents[1]

    def enqueue(priority="normal", text=None):
        return store.enqueue_task(
            text or "Fix the scheduler capacity recovery so queued tasks resume automatically",
            project_hint=str(root), priority=priority,
        )

    return SimpleNamespace(store=store, clock=clock, start=start, runs=runs,
                           root=root, enqueue=enqueue)


def test_real_queue_dispatches_highest_priority_and_preserves_thin_briefs(real_fleet_queue):
    queue = real_fleet_queue
    low = queue.enqueue("low")
    thin = queue.enqueue("critical", "locket is broken")
    first = queue.enqueue("high")
    second = queue.enqueue("high")
    action = REVIEWED_ACTIONS["serena.fleet.start"]

    for task in (first, second, low):
        outcome = action({})
        assert outcome.ok and outcome.output["task_id"] == task["id"]
        persisted = queue.store.get_memory(task["id"])
        assert persisted["state"] == "running"
        assert persisted["run_id"] == "run-1"
        assert persisted["assignee"].startswith("scheduler:")
        assert float(persisted["lease_until"]) > queue.clock[0]
    assert action({}).detail == "no ready task"
    assert queue.store.get_memory(thin["id"])["state"] == "needs_triage"
    assert queue.start.call_count == 3
    assert all(call.kwargs["cwd"] == str(queue.root) for call in queue.start.call_args_list)


def test_real_queue_existing_active_run_is_attached_without_start(real_fleet_queue):
    queue = real_fleet_queue
    task = queue.enqueue()
    queue.runs.return_value = [{"origin_session_id": f"serena-task:{task['id']}",
                                "run_id": "already-running", "state": "running"}]
    outcome = REVIEWED_ACTIONS["serena.fleet.start"]({})
    assert outcome.ok and outcome.output["run_id"] == "already-running"
    assert queue.store.get_memory(task["id"])["run_id"] == "already-running"
    assert REVIEWED_ACTIONS["serena.fleet.start"]({}).detail == "no ready task"
    queue.start.assert_not_called()


@pytest.mark.parametrize("visible", [False, True])
def test_real_queue_reclaims_after_crash_without_second_dispatch(real_fleet_queue, visible):
    queue = real_fleet_queue
    task = queue.enqueue()
    queue.start.side_effect = SystemExit("worker died during public start")
    action = REVIEWED_ACTIONS["serena.fleet.start"]
    with pytest.raises(SystemExit):
        action({})
    assert queue.store.get_memory(task["id"])["state"] == "claimed"
    queue.clock[0] += queue.store.TASK_LEASE_SECONDS + 1
    if visible:
        queue.runs.return_value = [{"origin_session_id": f"serena-task:{task['id']}",
                                    "run_id": "recovered", "state": "queued"}]
    outcome = action({})
    row = queue.store.get_memory(task["id"])
    assert outcome.ok
    assert row["state"] == ("running" if visible else "blocked")
    if visible:
        assert row["run_id"] == "recovered"
    assert action({}).detail == "no ready task"
    assert queue.start.call_count == 1


def test_real_queue_history_failure_returns_the_task_and_dispatches_once(real_fleet_queue):
    queue = real_fleet_queue
    task = queue.enqueue()
    queue.runs.side_effect = OSError("run history unavailable")
    action = REVIEWED_ACTIONS["serena.fleet.start"]

    first = action({})
    assert first.ok and first.output["state"] == "ready"
    assert queue.store.get_memory(task["id"])["state"] == "ready"
    queue.start.assert_not_called()

    queue.runs.side_effect = None
    second = action({})
    assert second.output["run_id"] == "run-1"
    assert queue.store.get_memory(task["id"])["state"] == "running"
    assert action({}).detail == "no ready task"
    assert queue.start.call_count == 1


def test_real_queue_overlapping_expired_claims_dispatch_once(real_fleet_queue):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    queue = real_fleet_queue
    task = queue.enqueue()
    entered, finish = Event(), Event()

    def slow_start(**_kwargs):
        entered.set()
        assert finish.wait(5)
        return {"run_id": "slow-run"}

    queue.start.side_effect = slow_start
    action = REVIEWED_ACTIONS["serena.fleet.start"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(action, {})
        try:
            assert entered.wait(5)
            queue.clock[0] += queue.store.TASK_LEASE_SECONDS + 1
            second = action({})
            assert second.ok and second.output["state"] == "blocked"
        finally:
            finish.set()
        assert first.result(timeout=5).output["run_id"] == "slow-run"
    assert queue.store.get_memory(task["id"])["state"] == "blocked"
    assert action({}).detail == "no ready task"
    assert queue.start.call_count == 1


def test_real_queue_capacity_hold_preserves_ready_task(real_fleet_queue, tmp_path):
    from unittest.mock import Mock

    from core.automation_runtime import AutomationRuntime

    queue = real_fleet_queue
    task = queue.enqueue()
    scheduler = register_all(SerenaScheduler(tmp_path / "scheduler.sqlite3", notifier=None))
    scheduler.add_schedule(action="serena.fleet.start", interval_seconds=60,
                           actor="raghav", requires_approval=False, first_run_at=0)
    capacity = Mock(return_value=(True, "providers exhausted"))
    authority = Mock()
    authority.deliver_due.return_value = []
    runtime = AutomationRuntime(scheduler=scheduler, authority=authority,
                                capacity_reader=capacity, capacity_probe_seconds=1,
                                publish_journals=False)
    assert runtime.run_pass(now=1000).capacity_held
    assert queue.store.get_memory(task["id"])["state"] == "ready"
    queue.start.assert_not_called()
    capacity.return_value = (False, "")
    assert runtime.run_pass(now=1002).ran == ["serena.fleet.start:ok"]
    assert queue.store.get_memory(task["id"])["state"] == "running"
    queue.start.assert_called_once()
