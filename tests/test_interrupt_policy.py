from __future__ import annotations

from datetime import datetime

from core.ambient_store import AmbientEvent
from core.interrupt_policy import (
    PolicyDecision,
    ProactiveItem,
    decide,
    is_breakpoint,
)
from core.notification_authority import NotificationPolicy, PresenceState


def event(kind, at, app="", title="", url=""):
    return AmbientEvent(
        event_id=f"{at}-{kind}-{app}-{title}",
        kind=kind, app=app, title=title, url=url,
        started_at=at, ended_at=at,
    )


def item(**kwargs):
    base = {"kind": "stuck.nudge", "summary": "looks stuck on BadWindow", "channel": "desktop"}
    base.update(kwargs)
    return ProactiveItem(**base)


def test_typing_holds_non_critical_delivery():
    decision = decide(
        item(), presence=PresenceState(typing=True),
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0), now=1_000.0,
    )

    assert decision.action == "hold"
    assert "typing" in decision.reason


def test_focused_holds_non_critical_delivery():
    decision = decide(
        item(), presence=PresenceState(activity_class="focused"),
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0), now=1_000.0,
    )

    assert decision.action == "hold"
    assert "focus" in decision.reason


def test_idle_at_a_breakpoint_delivers():
    decision = decide(
        item(), presence=PresenceState(),
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0), now=1_000.0,
    )

    assert decision.action == "deliver"


def test_quiet_hours_hold_everything_non_critical():
    policy = NotificationPolicy(quiet_start_hour=23, quiet_end_hour=8)
    moment = datetime(2026, 9, 18, 23, 30).timestamp()

    decision = decide(item(), presence=PresenceState(), policy=policy, now=moment)

    assert decision.action == "hold"
    assert "quiet" in decision.reason


def test_critical_passes_quiet_hours_and_typing():
    policy = NotificationPolicy(quiet_start_hour=23, quiet_end_hour=8)
    moment = datetime(2026, 9, 18, 23, 30).timestamp()

    decision = decide(
        item(urgency="critical"), presence=PresenceState(typing=True),
        policy=policy, now=moment,
    )

    assert decision.action == "deliver"


def test_suppressed_kind_is_dropped_with_a_reason():
    decision = decide(
        item(kind="noisy.kind"), presence=PresenceState(),
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0), now=1_000.0,
        is_suppressed=lambda kind: True,
    )

    assert decision.action == "drop"
    assert "dismiss" in decision.reason


def test_unlock_is_a_breakpoint():
    events = [event("window", 1_000.0, app="code"), event("unlock", 1_100.0)]

    assert is_breakpoint(events, now=1_110.0) is True


def test_return_from_idle_is_a_breakpoint():
    events = [
        event("window", 1_000.0, app="code"),
        event("idle", 1_100.0),
        event("active", 1_600.0),
    ]

    assert is_breakpoint(events, now=1_610.0) is True


def test_short_idle_gap_is_not_a_breakpoint():
    events = [
        event("window", 1_000.0, app="code"),
        event("idle", 1_100.0),
        event("active", 1_150.0),
    ]

    assert is_breakpoint(events, now=1_160.0) is False


def test_app_switch_after_a_long_focus_block_is_a_breakpoint():
    events = [
        event("window", 1_000.0, app="code", title="a.py"),
        event("window", 2_700.0, app="microsoft-edge", title="docs"),
    ]

    assert is_breakpoint(events, now=2_710.0) is True


def test_ordinary_app_switch_is_not_a_breakpoint():
    events = [
        event("window", 1_000.0, app="code", title="a.py"),
        event("window", 1_200.0, app="microsoft-edge", title="docs"),
    ]

    assert is_breakpoint(events, now=1_210.0) is False


def test_commit_or_push_is_a_breakpoint():
    events = [
        event("window", 1_000.0, app="code"),
        event("vcs", 1_100.0, app="serena", title="push"),
    ]

    assert is_breakpoint(events, now=1_110.0) is True


def test_steady_work_has_no_breakpoint():
    events = [event("window", 1_000.0 + index * 60.0, app="code") for index in range(5)]

    assert is_breakpoint(events, now=1_400.0) is False


def test_decision_is_a_small_auditable_record():
    decision = PolicyDecision(action="hold", reason="typing")
    assert decision.action == "hold"
    assert decision.reason == "typing"
