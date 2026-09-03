"""Briefing generation, interruption rules, and the scheduler wiring.

Nothing in this file may send anything. Every notification authority built here
is constructed with explicit fake senders, so there is no path from a test to
the desktop voice bridge or the Telegram bot. `test_no_real_sender_is_reachable`
holds that line deliberately rather than by luck.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from core.briefings import (
    BRIEFING_ACTIONS,
    Briefing,
    InterruptionRules,
    build_evening_briefing,
    build_morning_briefing,
    build_pre_event_briefing,
    deliver,
    pending_pre_event_briefings,
    register_briefing_actions,
)
from core.commitments import CommitmentStore
from core.notification_authority import (
    NotificationAuthority,
    NotificationPolicy,
    NotificationRequest,
)
from core.serena_scheduler import SerenaScheduler

MORNING = datetime(2026, 8, 8, 8, 30, 0).timestamp()
EVENING = datetime(2026, 8, 8, 19, 30, 0).timestamp()
MIDNIGHT = datetime(2026, 8, 8, 2, 0, 0).timestamp()
HOUR = 3_600.0
DAY = 86_400.0


@pytest.fixture
def store(tmp_path):
    return CommitmentStore(tmp_path / "commitments.sqlite3")


class RecordingSender:
    """Stands in for a transport. Records, never sends."""

    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.sent: list[NotificationRequest] = []

    def __call__(self, request: NotificationRequest) -> bool:
        self.sent.append(request)
        return self.accept


@pytest.fixture
def authority(tmp_path):
    sender = RecordingSender()
    target = NotificationAuthority(
        tmp_path / "notifications.sqlite3",
        policy=NotificationPolicy(quiet_start_hour=22, quiet_end_hour=8),
        senders={"voice": sender, "telegram": sender, "desktop": sender},
    )
    target.recorded = sender  # type: ignore[attr-defined]
    return target


def _accepted(store, title, *, due_at, priority="normal", now=MORNING, lead=1_800):
    return store.propose(
        title=title,
        actor="raghav",
        source="voice",
        due_at=due_at,
        priority=priority,
        state="accepted",
        lead_seconds=lead,
        now=now,
    )


# ---- morning --------------------------------------------------------------


def test_an_empty_day_says_so_and_is_not_worth_interrupting(store):
    briefing = build_morning_briefing(store, now=MORNING)
    assert briefing.kind == "morning"
    assert briefing.empty
    assert "clear" in briefing.spoken
    assert not InterruptionRules().evaluate(briefing, now=MORNING).interrupt


def test_morning_leads_with_overdue_and_counts_the_rest(store):
    _accepted(store, "file the taxes", due_at=MORNING - DAY)
    _accepted(store, "dentist", due_at=MORNING + 4 * HOUR)
    briefing = build_morning_briefing(store, now=MORNING)

    assert briefing.lines[0].startswith("file the taxes, overdue since")
    assert "dentist, at 12:30 pm" in briefing.spoken
    assert "1 already overdue" in briefing.spoken
    assert briefing.urgency == "normal"
    assert briefing.dedupe_key == "briefing:morning:2026-08-08"


def test_a_long_day_is_trimmed_rather_than_read_out_in_full(store):
    for index in range(7):
        _accepted(store, f"thing {index}", due_at=MORNING + (index + 1) * HOUR)
    briefing = build_morning_briefing(store, now=MORNING)
    assert len(briefing.lines) == 4
    assert "3 more behind those" in briefing.spoken
    # The trimming is a speaking decision; nothing is lost from the record.
    assert len(briefing.commitment_ids) == 7


def test_only_a_critical_overdue_thing_escalates_urgency(store):
    _accepted(store, "renew the licence", due_at=MORNING - DAY, priority="critical")
    briefing = build_morning_briefing(store, now=MORNING)
    assert briefing.urgency == "critical"


def test_snoozed_and_dismissed_commitments_stay_out_of_the_morning(store):
    snoozed = _accepted(store, "call the bank", due_at=MORNING - HOUR)
    store.snooze(snoozed.commitment_id, actor="raghav", seconds=6 * HOUR, now=MORNING)
    dropped = _accepted(store, "old thing", due_at=MORNING - HOUR)
    store.dismiss(dropped.commitment_id, actor="raghav", now=MORNING)

    briefing = build_morning_briefing(store, now=MORNING)
    assert briefing.empty


# ---- evening --------------------------------------------------------------


def test_evening_reports_what_closed_and_what_is_next(store):
    done = _accepted(store, "ship the release", due_at=MORNING)
    store.complete(done.commitment_id, actor="raghav", now=EVENING - HOUR)
    _accepted(store, "standup", due_at=EVENING + 14 * HOUR)

    briefing = build_evening_briefing(store, now=EVENING)
    assert "you closed 1 today" in briefing.spoken
    assert "next up: standup, tomorrow" in briefing.spoken


def test_evening_on_a_dead_day_says_nothing_happened(store):
    briefing = build_evening_briefing(store, now=EVENING)
    assert briefing.empty
    assert "quiet day" in briefing.spoken


# ---- pre-event ------------------------------------------------------------


def test_pre_event_fires_only_inside_the_lead_window(store):
    soon = _accepted(store, "dentist", due_at=MORNING + 20 * 60, lead=1_800)
    _accepted(store, "later thing", due_at=MORNING + 5 * HOUR, lead=1_800)

    pending = pending_pre_event_briefings(store, now=MORNING)
    assert [item.commitment_ids[0] for item in pending] == [soon.commitment_id]
    assert "in 20 min" in pending[0].spoken


def test_a_pre_event_briefing_is_not_repeated_once_spoken(store):
    item = _accepted(store, "dentist", due_at=MORNING + 20 * 60, lead=1_800)
    assert pending_pre_event_briefings(store, now=MORNING)
    store.mark_briefed(item.commitment_id, now=MORNING)
    assert pending_pre_event_briefings(store, now=MORNING) == []


def test_an_overdue_thing_is_not_a_heads_up(store):
    _accepted(store, "missed it", due_at=MORNING - HOUR)
    assert pending_pre_event_briefings(store, now=MORNING) == []


def test_pre_event_dedupe_key_is_per_commitment(store):
    item = _accepted(store, "dentist", due_at=MORNING + 20 * 60)
    briefing = build_pre_event_briefing(store.require(item.commitment_id), now=MORNING)
    assert briefing.dedupe_key == f"briefing:pre_event:{item.commitment_id}"


# ---- interruption rules ---------------------------------------------------


def test_quiet_hours_hold_an_ordinary_briefing(store):
    _accepted(store, "dentist", due_at=MIDNIGHT + 8 * HOUR, now=MIDNIGHT)
    briefing = build_morning_briefing(store, now=MIDNIGHT)
    decision = InterruptionRules().evaluate(briefing, now=MIDNIGHT)
    assert not decision.interrupt
    assert "quiet hours" in decision.reason


def test_a_critical_overdue_thing_gets_through_quiet_hours(store):
    _accepted(
        store,
        "renew the licence",
        due_at=MIDNIGHT - DAY,
        priority="critical",
        now=MIDNIGHT,
    )
    briefing = build_morning_briefing(store, now=MIDNIGHT)
    assert InterruptionRules().evaluate(briefing, now=MIDNIGHT).interrupt


def test_a_second_briefing_too_soon_is_held_back(store):
    _accepted(store, "dentist", due_at=MORNING + 3 * HOUR)
    briefing = build_morning_briefing(store, now=MORNING)
    decision = InterruptionRules().evaluate(
        briefing, now=MORNING, last_briefing_at=MORNING - 600
    )
    assert not decision.interrupt
    assert "already went out" in decision.reason


# ---- delivery -------------------------------------------------------------


def test_delivery_goes_through_the_authority_and_records_the_decision(store, authority):
    _accepted(store, "dentist", due_at=MORNING + 3 * HOUR)
    briefing = build_morning_briefing(store, now=MORNING)
    decision, result = deliver(briefing, authority=authority, now=MORNING)

    assert decision.interrupt
    assert result is not None and result.sent
    assert len(authority.recorded.sent) == 1
    assert authority.recorded.sent[0].kind == "serena.briefing.morning"
    assert authority.recorded.sent[0].channel == "voice"


def test_a_held_back_briefing_never_reaches_a_sender(store, authority):
    briefing = build_morning_briefing(store, now=MORNING)  # empty day
    decision, result = deliver(briefing, authority=authority, now=MORNING)
    assert not decision.interrupt
    assert result is None
    assert authority.recorded.sent == []


def test_delivery_refuses_without_an_explicit_authority(store):
    briefing = build_morning_briefing(store, now=MORNING)
    with pytest.raises(ValueError):
        deliver(briefing, authority=None, now=MORNING)


def test_the_authority_still_deduplicates_a_repeated_briefing(store, authority):
    _accepted(store, "dentist", due_at=MORNING + 3 * HOUR)
    briefing = build_morning_briefing(store, now=MORNING)
    deliver(briefing, authority=authority, now=MORNING)
    # Same dedupe key inside the window: the authority suppresses it, and the
    # briefing layer does not get to override that.
    _decision, second = deliver(
        briefing, authority=authority, now=MORNING + 60, last_briefing_at=None
    )
    assert second is not None and second.decision == "suppressed"
    assert len(authority.recorded.sent) == 1


# ---- scheduler wiring -----------------------------------------------------


class InertNotifier:
    """The scheduler's notifier seam, held open so nothing is ever delivered."""

    def __init__(self) -> None:
        self.requests: list = []

    def request(self, request):
        self.requests.append(request)
        return None


def test_registering_briefing_actions_leaves_the_shared_registry_alone(tmp_path, store):
    from core.scheduler_actions import REVIEWED_ACTIONS, register_all

    scheduler = SerenaScheduler(tmp_path / "s.sqlite3", notifier=InertNotifier())
    register_all(scheduler)
    before = set(scheduler.actions)
    register_briefing_actions(scheduler, store=store, sources=[])

    # The other unit's closed set is untouched; ours is additive on top.
    assert set(REVIEWED_ACTIONS) <= before
    assert set(scheduler.actions) == before | set(BRIEFING_ACTIONS)


def test_the_morning_action_runs_and_hands_a_notice_to_the_scheduler(tmp_path, store):
    _accepted(store, "dentist", due_at=MORNING + 3 * HOUR, now=MORNING)
    notifier = InertNotifier()
    scheduler = SerenaScheduler(tmp_path / "s.sqlite3", notifier=notifier)
    register_briefing_actions(
        scheduler, store=store, sources=[], clock=lambda: MORNING
    )

    schedule = scheduler.add_schedule(
        action="serena.briefing.morning",
        interval_seconds=86_400,
        actor="raghav",
        requires_approval=False,
        first_run_at=0,
    )
    run = scheduler.run_now(schedule["schedule_id"])
    assert run.ok
    # Delivery went down the scheduler's existing bounded path, not a new one.
    assert len(notifier.requests) == 1
    assert notifier.requests[0].kind == "serena.briefing.morning"


def test_the_scheduler_path_does_not_claim_a_briefing_was_spoken(tmp_path, store):
    """SerenaScheduler._notify swallows failures, so it cannot prove delivery."""

    item = _accepted(store, "dentist", due_at=MORNING + 3 * HOUR, now=MORNING)
    scheduler = SerenaScheduler(tmp_path / "s.sqlite3", notifier=InertNotifier())
    register_briefing_actions(
        scheduler, store=store, sources=[], clock=lambda: MORNING
    )
    schedule = scheduler.add_schedule(
        action="serena.briefing.morning",
        interval_seconds=86_400,
        actor="raghav",
        requires_approval=False,
        first_run_at=0,
    )
    run = scheduler.run_now(schedule["schedule_id"])

    assert run.ok
    assert store.require(item.commitment_id).last_briefed_at is None


def test_with_an_authority_a_sent_briefing_is_recorded_as_spoken(tmp_path, store, authority):
    item = _accepted(store, "dentist", due_at=MORNING + 3 * HOUR, now=MORNING)
    scheduler = SerenaScheduler(tmp_path / "s.sqlite3", notifier=InertNotifier())
    register_briefing_actions(
        scheduler,
        store=store,
        sources=[],
        clock=lambda: MORNING,
        authority=authority,
    )
    schedule = scheduler.add_schedule(
        action="serena.briefing.morning",
        interval_seconds=86_400,
        actor="raghav",
        requires_approval=False,
        first_run_at=0,
    )
    run = scheduler.run_now(schedule["schedule_id"])

    assert run.ok
    assert store.require(item.commitment_id).last_briefed_at == pytest.approx(MORNING)
    assert len(authority.recorded.sent) == 1


def test_a_briefing_the_transport_refused_stays_eligible_to_be_said_again(
    tmp_path, store
):
    """A dead channel must not silently consume the only chance to mention this."""

    item = _accepted(store, "dentist", due_at=MORNING + 3 * HOUR, now=MORNING)
    refusing = RecordingSender(accept=False)
    target = NotificationAuthority(
        tmp_path / "notifications.sqlite3",
        policy=NotificationPolicy(quiet_start_hour=22, quiet_end_hour=8),
        senders={"voice": refusing, "telegram": refusing, "desktop": refusing},
    )
    briefing = build_morning_briefing(store, now=MORNING)

    _decision, result = deliver(
        briefing, authority=target, now=MORNING, store=store
    )

    assert result.decision != "sent"
    assert refusing.sent  # it was genuinely attempted
    assert store.require(item.commitment_id).last_briefed_at is None


def test_an_unsent_pre_event_heads_up_is_still_pending_next_tick(tmp_path, store):
    _accepted(
        store, "flight", due_at=MORNING + 1_200, now=MORNING - HOUR, lead=1_800
    )
    scheduler = SerenaScheduler(tmp_path / "s.sqlite3", notifier=InertNotifier())
    register_briefing_actions(
        scheduler, store=store, sources=[], clock=lambda: MORNING
    )
    schedule = scheduler.add_schedule(
        action="serena.briefing.pre_event",
        interval_seconds=300,
        actor="raghav",
        requires_approval=False,
        first_run_at=0,
    )
    scheduler.run_now(schedule["schedule_id"])

    # Nothing confirmed it was heard, so it has not been struck off.
    assert len(pending_pre_event_briefings(store, now=MORNING)) == 1


def test_the_ingest_action_reports_unavailable_sources_without_failing(tmp_path, store):
    scheduler = SerenaScheduler(tmp_path / "s.sqlite3", notifier=InertNotifier())

    class Missing:
        name = "local_ics"

        def status(self):
            from core.commitment_sources import SourceStatus

            return SourceStatus(False, "no directory set")

        def fetch(self, *, now):  # pragma: no cover - never reached
            raise AssertionError("must not fetch")

    register_briefing_actions(
        scheduler, store=store, sources=[Missing()], clock=lambda: MORNING
    )
    schedule = scheduler.add_schedule(
        action="serena.commitments.ingest",
        interval_seconds=3_600,
        actor="raghav",
        requires_approval=False,
        first_run_at=0,
    )
    run = scheduler.run_now(schedule["schedule_id"])
    assert run.ok
    assert "unavailable: local_ics" in run.detail


def test_the_activate_action_promotes_due_commitments(tmp_path, store):
    _accepted(store, "standup", due_at=MORNING + 60, now=MORNING)
    scheduler = SerenaScheduler(tmp_path / "s.sqlite3", notifier=InertNotifier())
    register_briefing_actions(
        scheduler, store=store, sources=[], clock=lambda: MORNING
    )
    schedule = scheduler.add_schedule(
        action="serena.commitments.activate",
        interval_seconds=300,
        actor="raghav",
        requires_approval=False,
        first_run_at=0,
    )
    run = scheduler.run_now(schedule["schedule_id"])
    assert run.ok
    assert "1 commitment went active" in run.detail


def test_a_quiet_briefing_action_succeeds_without_notifying(tmp_path, store):
    notifier = InertNotifier()
    scheduler = SerenaScheduler(tmp_path / "s.sqlite3", notifier=notifier)
    register_briefing_actions(
        scheduler, store=store, sources=[], clock=lambda: EVENING
    )
    schedule = scheduler.add_schedule(
        action="serena.briefing.evening",
        interval_seconds=86_400,
        actor="raghav",
        requires_approval=False,
        first_run_at=0,
    )
    run = scheduler.run_now(schedule["schedule_id"])
    assert run.ok
    assert run.detail == "nothing worth saying"
    assert notifier.requests == []


# ---- continuity honesty ---------------------------------------------------


def test_a_degraded_briefing_says_it_is_degraded(store):
    _accepted(store, "dentist", due_at=MORNING + 3 * HOUR)
    briefing = build_morning_briefing(store, now=MORNING, mode="degraded")
    assert briefing.mode == "degraded"
    assert "local model" in briefing.spoken


def test_an_offline_briefing_is_still_a_real_briefing(store):
    _accepted(store, "dentist", due_at=MORNING + 3 * HOUR)
    briefing = build_morning_briefing(store, now=MORNING, mode="offline")
    # The whole point of ws-8: this path never needed a model at all.
    assert not briefing.empty
    assert "dentist" in briefing.spoken
    assert "no model available" in briefing.spoken


# ---- the safety line ------------------------------------------------------


def test_no_real_sender_is_reachable_from_a_briefing_test(authority):
    """The authority under test only knows fakes, and delivery demands one."""

    from core.notification_senders import DEFAULT_SENDERS

    for name, sender in authority._senders.items():
        assert isinstance(sender, RecordingSender), name
        assert sender is not DEFAULT_SENDERS.get(name)

    briefing = Briefing(kind="morning", generated_at=MORNING, spoken="x", lines=("x",))
    with pytest.raises(ValueError):
        deliver(briefing, authority=None)
