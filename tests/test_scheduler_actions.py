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


@pytest.fixture
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
    """A voice failure may fall back to Telegram, but not past the policy."""

    from core.notification_senders import notify

    attempts: list = []
    authority = NotificationAuthority(
        tmp_path / "n.sqlite3",
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0),
        senders={
            "voice": lambda r: attempts.append("voice") or False,
            "telegram": lambda r: attempts.append("telegram") or True,
        },
    )

    result = notify(
        "fleet.run.completed", "the run finished", authority=authority, dedupe_key="run-1"
    )

    assert attempts == ["voice", "telegram"]
    assert result.sent is True


def test_the_fallback_does_not_fire_when_the_notice_was_merely_suppressed(tmp_path):
    from core.notification_senders import notify

    attempts: list = []
    authority = NotificationAuthority(
        tmp_path / "n.sqlite3",
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0),
        senders={
            "voice": lambda r: attempts.append("voice") or True,
            "telegram": lambda r: attempts.append("telegram") or True,
        },
    )
    notify("fleet.run.completed", "the run finished", authority=authority, dedupe_key="run-1")
    attempts.clear()

    # Same dedupe key: suppressed is a real answer, not a transport failure.
    notify("fleet.run.completed", "the run finished", authority=authority, dedupe_key="run-1")

    assert attempts == [], "a suppressed notice must not be retried on another channel"
