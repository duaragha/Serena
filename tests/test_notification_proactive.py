"""Proactive caps, typing/focus holds, and the 23-08 quiet alignment.

Fake senders and private DBs only; nothing here may send a real notice.
"""

from __future__ import annotations

from datetime import datetime

from core.control_plane import ControlPlaneStore
from core.notification_authority import (
    NotificationAuthority,
    NotificationPolicy,
    NotificationRequest,
    PresenceState,
)


def at_hour(hour, day=18):
    return datetime(2026, 9, day, hour, 30).timestamp()


def awake_policy(**kwargs):
    base = {"quiet_start_hour": 0, "quiet_end_hour": 0}
    base.update(kwargs)
    return NotificationPolicy(**base)


def make_authority(tmp_path, *, policy=None, presence=None, senders=None):
    return NotificationAuthority(
        tmp_path / "notices.sqlite3",
        policy=policy or awake_policy(),
        senders=senders or {"voice": lambda request: True, "telegram": lambda request: True,
                            "desktop": lambda request: True, "imessage": lambda request: True},
        control_store=ControlPlaneStore(tmp_path / "control.sqlite3"),
        presence=presence,
    )


def proactive(channel="telegram", **kwargs):
    base = {"kind": "stuck.nudge", "summary": "you look stuck", "channel": channel,
            "proactive": True}
    base.update(kwargs)
    return NotificationRequest(**base)


def test_typing_holds_proactive_delivery_for_a_breakpoint(tmp_path):
    authority = make_authority(
        tmp_path, presence=lambda: PresenceState(typing=True))
    moment = at_hour(10)

    result = authority.request(proactive(dedupe_key="t1"), now=moment)

    assert result.decision == "deferred"
    assert "typing" in result.reason
    assert result.deliver_after is None


def test_focused_holds_proactive_delivery_for_a_breakpoint(tmp_path):
    authority = make_authority(
        tmp_path, presence=lambda: PresenceState(activity_class="focused"))
    moment = at_hour(10)

    result = authority.request(proactive(dedupe_key="f1"), now=moment)

    assert result.decision == "deferred"
    assert "focus" in result.reason


def test_breakpoint_release_delivers_held_oldest_first(tmp_path):
    states = {"typing": True}
    sent = []
    authority = make_authority(
        tmp_path,
        presence=lambda: PresenceState(typing=states["typing"]),
        senders={"telegram": lambda request: sent.append(request.summary) or True})
    moment = at_hour(10)
    authority.request(proactive(summary="first", dedupe_key="r1"), now=moment)
    authority.request(proactive(summary="second", dedupe_key="r2"), now=moment + 1)

    states["typing"] = False
    released = authority.release_at_breakpoint(now=moment + 60)

    assert sent == ["first", "second"]
    assert all(item.decision == "sent" for item in released)


def test_release_stays_held_while_still_typing(tmp_path):
    authority = make_authority(
        tmp_path, presence=lambda: PresenceState(typing=True))
    moment = at_hour(10)
    authority.request(proactive(dedupe_key="h1"), now=moment)

    assert authority.release_at_breakpoint(now=moment + 60) == []


def test_daily_cap_holds_the_fourth_proactive_send_per_channel(tmp_path):
    authority = make_authority(tmp_path)
    moment = at_hour(10)

    results = [
        authority.request(proactive(dedupe_key=f"d{i}"), now=moment + i)
        for i in range(4)
    ]

    assert [item.decision for item in results] == ["sent", "sent", "sent", "deferred"]
    assert "digest" in results[3].reason


def test_daily_cap_is_shared_across_phone_but_not_desktop(tmp_path):
    authority = make_authority(tmp_path)
    moment = at_hour(10)
    for index, channel in enumerate(["telegram", "imessage", "voice"]):
        assert authority.request(
            proactive(channel=channel, dedupe_key=f"t{index}"),
            now=moment + index,
        ).decision == "sent"

    fourth = authority.request(
        proactive(channel="telegram", dedupe_key="t3"), now=moment + 10)

    assert fourth.decision == "deferred"
    assert "phone" in fourth.reason

    desktop = authority.request(
        proactive(channel="desktop", dedupe_key="desk"), now=moment + 11)

    assert desktop.decision == "sent"


def test_desktop_hourly_cap_holds_the_second_send(tmp_path):
    authority = make_authority(tmp_path)
    moment = at_hour(10)

    first = authority.request(proactive(channel="desktop", dedupe_key="h1"), now=moment)
    second = authority.request(
        proactive(channel="desktop", dedupe_key="h2"), now=moment + 60)

    assert first.decision == "sent"
    assert second.decision == "deferred"
    assert "digest" in second.reason


def test_awaited_work_is_not_starved_by_proactive_caps(tmp_path):
    authority = make_authority(tmp_path)
    moment = at_hour(10)
    for index in range(3):
        authority.request(proactive(dedupe_key=f"p{index}"), now=moment + index)

    result = authority.request(
        NotificationRequest(kind="fleet.done", summary="run finished",
                            channel="telegram", dedupe_key="fleet-1"),
        now=moment + 10,
    )

    assert result.decision == "sent"


def test_critical_proactive_bypasses_caps_and_typing(tmp_path):
    authority = make_authority(
        tmp_path, presence=lambda: PresenceState(typing=True))
    moment = at_hour(10)
    for index in range(3):
        authority.request(
            proactive(summary=f"n{index}", dedupe_key=f"c{index}", urgency="normal"),
            now=moment,
        )

    result = authority.request(
        proactive(summary="deadline", dedupe_key="crit", urgency="critical"),
        now=moment + 1,
    )

    assert result.decision == "sent"


def test_quiet_hours_are_23_to_08_per_the_documented_policy(tmp_path):
    authority = make_authority(
        tmp_path, policy=NotificationPolicy(quiet_start_hour=23, quiet_end_hour=8))

    held = authority.request(proactive(dedupe_key="q1"), now=at_hour(23))
    assert held.decision == "deferred"
    assert held.deliver_after is not None

    awake = authority.request(proactive(dedupe_key="q2"), now=at_hour(22))
    assert awake.decision == "sent"


def test_quiet_default_matches_the_documented_policy():
    assert NotificationPolicy().quiet_start_hour == 23
    assert NotificationPolicy().quiet_end_hour == 8


def test_release_is_gated_by_quiet_hours_too(tmp_path):
    states = {"typing": True}
    authority = make_authority(
        tmp_path, policy=NotificationPolicy(quiet_start_hour=23, quiet_end_hour=8),
        presence=lambda: PresenceState(typing=states["typing"]))
    authority.request(proactive(dedupe_key="n1"), now=at_hour(22))

    states["typing"] = False
    assert authority.release_at_breakpoint(now=at_hour(2, day=19)) == []


def test_cross_channel_dedupe_sends_once(tmp_path):
    authority = make_authority(tmp_path)
    moment = at_hour(10)

    first = authority.request(proactive(channel="telegram", dedupe_key="same"), now=moment)
    second = authority.request(
        proactive(channel="desktop", dedupe_key="same"), now=moment + 1)

    assert first.decision == "sent"
    assert second.decision == "suppressed"


def test_digest_requests_skip_caps_but_keep_quiet_hours(tmp_path):
    authority = make_authority(tmp_path)
    moment = at_hour(10)
    for index in range(3):
        authority.request(proactive(dedupe_key=f"g{index}"), now=moment + index)

    digest = authority.request(
        NotificationRequest(kind="proactive.digest", summary="3 held items",
                            channel="telegram", dedupe_key="digest-1", proactive=True),
        now=moment + 10,
    )
    assert digest.decision == "sent"

    quiet_authority = make_authority(
        tmp_path, policy=NotificationPolicy(quiet_start_hour=23, quiet_end_hour=8))
    held_digest = quiet_authority.request(
        NotificationRequest(kind="proactive.digest", summary="held", channel="telegram",
                            dedupe_key="digest-2", proactive=True),
        now=at_hour(2, day=19),
    )
    assert held_digest.decision == "deferred"


def test_outcomes_stay_within_the_existing_five(tmp_path):
    authority = make_authority(
        tmp_path, presence=lambda: PresenceState(typing=True))

    result = authority.request(proactive(dedupe_key="o1"), now=at_hour(10))

    assert result.decision in ("sent", "suppressed", "deferred", "pending_approval", "failed")


def test_quiet_expired_items_rehold_while_typing_instead_of_sending(tmp_path):
    states = {"typing": False}
    sent = []
    authority = make_authority(
        tmp_path, policy=NotificationPolicy(quiet_start_hour=23, quiet_end_hour=8),
        presence=lambda: PresenceState(typing=states["typing"]),
        senders={"telegram": lambda request: sent.append(request.summary) or True})
    held = authority.request(proactive(dedupe_key="q-exp"), now=at_hour(2, day=19))
    assert held.decision == "deferred"
    assert held.deliver_after is not None

    states["typing"] = True
    resumed = authority.deliver_due(now=at_hour(9, day=19))

    assert sent == []
    assert [item.decision for item in resumed] == ["deferred"]
    assert "typing" in resumed[0].reason
    assert authority.held_items() != []

    states["typing"] = False
    released = authority.release_at_breakpoint(now=at_hour(9, day=19) + 60)

    assert sent == ["you look stuck"]
    assert all(item.decision == "sent" for item in released)


def test_quiet_expired_items_send_at_resume_when_idle(tmp_path):
    sent = []
    authority = make_authority(
        tmp_path, policy=NotificationPolicy(quiet_start_hour=23, quiet_end_hour=8),
        senders={"telegram": lambda request: sent.append(request.summary) or True})
    authority.request(proactive(dedupe_key="q-idle"), now=at_hour(2, day=19))

    resumed = authority.deliver_due(now=at_hour(9, day=19))

    assert [item.decision for item in resumed] == ["sent"]
    assert sent == ["you look stuck"]
