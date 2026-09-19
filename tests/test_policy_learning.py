"""Digest batching, dismissal learning with decay, and breakpoint scans."""

from __future__ import annotations

from datetime import datetime

from core.ambient_store import AmbientEvent, AmbientStore
from core.control_plane import ControlPlaneStore
from core.interrupt_policy import (
    PolicyStore,
    presence_now,
    record_feedback,
    scan_and_release,
)
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


def make_authority(tmp_path, **kwargs):
    kwargs.setdefault("policy", awake_policy())
    kwargs.setdefault("senders", {"telegram": lambda request: True,
                                  "desktop": lambda request: True})
    return NotificationAuthority(
        tmp_path / "notices.sqlite3",
        control_store=ControlPlaneStore(tmp_path / "control.sqlite3"),
        **kwargs,
    )


def policy_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENA_POLICY_DB_PATH", str(tmp_path / "policy.sqlite3"))
    return PolicyStore()


def event(kind, at, app="", title=""):
    return AmbientEvent(
        event_id=f"{at}-{kind}-{app}-{title}",
        kind=kind, app=app, title=title,
        started_at=at, ended_at=at,
    )


def test_three_dismissals_suppress_a_kind(tmp_path, monkeypatch):
    store = policy_store(tmp_path, monkeypatch)

    for index in range(3):
        record_feedback("noisy.kind", "dismissed", now=1_000.0 + index, store=store)

    assert store.is_suppressed("noisy.kind", now=2_000.0) is True
    assert store.is_suppressed("other.kind", now=2_000.0) is False


def test_strikes_outside_the_window_do_not_count(tmp_path, monkeypatch):
    store = policy_store(tmp_path, monkeypatch)
    now = 5_000_000.0

    record_feedback("noisy.kind", "dismissed", now=now - 40 * 86_400, store=store)
    record_feedback("noisy.kind", "dismissed", now=now - 35 * 86_400, store=store)
    record_feedback("noisy.kind", "dismissed", now=now - 31 * 86_400, store=store)

    assert store.is_suppressed("noisy.kind", now=now) is False


def test_two_strikes_are_not_enough_but_three_are(tmp_path, monkeypatch):
    store = policy_store(tmp_path, monkeypatch)
    now = 5_000_000.0

    record_feedback("noisy.kind", "dismissed", now=now - 20 * 86_400, store=store)
    record_feedback("noisy.kind", "ignored", now=now - 60.0, store=store)
    assert store.is_suppressed("noisy.kind", now=now) is False

    record_feedback("noisy.kind", "ignored", now=now - 30.0, store=store)
    assert store.is_suppressed("noisy.kind", now=now) is True


def test_silence_lasts_a_week_from_the_third_strike(tmp_path, monkeypatch):
    store = policy_store(tmp_path, monkeypatch)
    now = 5_000_000.0

    for index in range(3):
        record_feedback("noisy.kind", "dismissed", now=now + index, store=store)

    assert store.is_suppressed("noisy.kind", now=now + 3 * 86_400) is True
    assert store.is_suppressed("noisy.kind", now=now + 8 * 86_400) is False


def test_acted_outcomes_do_not_suppress(tmp_path, monkeypatch):
    store = policy_store(tmp_path, monkeypatch)

    for index in range(5):
        record_feedback("good.kind", "acted", now=1_000.0 + index, store=store)

    assert store.is_suppressed("good.kind", now=2_000.0) is False


def test_over_cap_items_arrive_once_as_a_digest(tmp_path, monkeypatch):
    sent = []
    authority = make_authority(
        tmp_path, senders={"telegram": lambda request: sent.append(request) or True})
    policy_store(tmp_path, monkeypatch)
    moment = at_hour(10)
    for index in range(5):
        authority.request(
            NotificationRequest(kind="stuck.nudge", summary=f"nudge {index}",
                                channel="telegram", proactive=True,
                                dedupe_key=f"n{index}"),
            now=moment + index,
        )
    events = [event("window", moment, app="code"), event("unlock", moment + 30.0)]

    released = scan_and_release(authority, events, now=moment + 40.0)

    assert sent and len(sent) == 4  # 3 direct + 1 digest
    assert sent[-1].kind == "proactive.digest"
    assert "nudge 3" in sent[-1].summary and "nudge 4" in sent[-1].summary
    assert released == ["digest:telegram"]


def test_single_held_item_delivers_individually(tmp_path, monkeypatch):
    states = {"typing": True}
    sent = []
    authority = make_authority(
        tmp_path,
        presence=lambda: PresenceState(typing=states["typing"]),
        senders={"telegram": lambda request: sent.append(request) or True})
    policy_store(tmp_path, monkeypatch)
    moment = at_hour(10)
    authority.request(
        NotificationRequest(kind="stuck.nudge", summary="one nudge",
                            channel="telegram", proactive=True, dedupe_key="one"),
        now=moment,
    )
    states["typing"] = False
    events = [event("window", moment, app="code"), event("unlock", moment + 30.0)]

    released = scan_and_release(authority, events, now=moment + 40.0)

    assert [request.summary for request in sent] == ["one nudge"]
    assert sent[0].kind == "stuck.nudge"
    assert len(released) == 1


def test_scan_without_a_breakpoint_releases_nothing(tmp_path, monkeypatch):
    states = {"typing": True}
    sent = []
    authority = make_authority(
        tmp_path,
        presence=lambda: PresenceState(typing=states["typing"]),
        senders={"telegram": lambda request: sent.append(request) or True})
    policy_store(tmp_path, monkeypatch)
    moment = at_hour(10)
    authority.request(
        NotificationRequest(kind="stuck.nudge", summary="held",
                            channel="telegram", proactive=True, dedupe_key="held"),
        now=moment,
    )
    states["typing"] = False
    steady = [event("window", moment + index * 60.0, app="code") for index in range(5)]

    assert scan_and_release(authority, steady, now=moment + 400.0) == []
    assert sent == []


def test_scan_during_quiet_hours_releases_nothing(tmp_path, monkeypatch):
    states = {"typing": True}
    sent = []
    authority = make_authority(
        tmp_path,
        policy=NotificationPolicy(quiet_start_hour=23, quiet_end_hour=8),
        presence=lambda: PresenceState(typing=states["typing"]),
        senders={"telegram": lambda request: sent.append(request) or True})
    policy_store(tmp_path, monkeypatch)
    evening = at_hour(22)
    authority.request(
        NotificationRequest(kind="stuck.nudge", summary="held",
                            channel="telegram", proactive=True, dedupe_key="night"),
        now=evening,
    )
    states["typing"] = False
    night = at_hour(2, day=19)
    events = [event("window", evening, app="code"), event("unlock", night - 10.0)]

    assert scan_and_release(authority, events, now=night) == []
    assert sent == []


def test_suppression_and_holds_are_visible_in_the_log(tmp_path, monkeypatch):
    store = policy_store(tmp_path, monkeypatch)

    store.log_decision("noisy.kind", "drop", "dismissed repeatedly", now=1_000.0)
    store.log_decision("stuck.nudge", "hold", "typing", now=1_001.0)

    log = store.recent_log(limit=10)
    assert [(entry["kind"], entry["action"]) for entry in log] == [
        ("stuck.nudge", "hold"), ("noisy.kind", "drop")]
    assert all(entry["reason"] for entry in log)


def test_dismiss_latest_strikes_the_newest_proactive_kind(tmp_path, monkeypatch):
    from core.interrupt_policy import dismiss_latest_proactive

    sent = []
    authority = make_authority(
        tmp_path, senders={"telegram": lambda request: sent.append(request) or True})
    store = policy_store(tmp_path, monkeypatch)
    moment = at_hour(10)
    authority.request(
        NotificationRequest(kind="stuck.nudge", summary="old nudge",
                            channel="telegram", proactive=True, dedupe_key="d1"),
        now=moment,
    )
    authority.request(
        NotificationRequest(kind="support.checkin", summary="a check-in",
                            channel="telegram", proactive=True, dedupe_key="d2"),
        now=moment + 60.0,
    )

    assert dismiss_latest_proactive(authority, now=moment + 120.0) == "support.checkin"
    assert store.is_suppressed("support.checkin", now=moment + 120.0) is False
    assert dismiss_latest_proactive(authority, now=moment + 180.0) == "support.checkin"
    assert dismiss_latest_proactive(authority, now=moment + 240.0) == "support.checkin"
    assert store.is_suppressed("support.checkin", now=moment + 300.0) is True
    assert store.is_suppressed("stuck.nudge", now=moment + 300.0) is False


def test_dismiss_latest_ignores_digests_and_stale_history(tmp_path, monkeypatch):
    from core.interrupt_policy import dismiss_latest_proactive

    authority = make_authority(tmp_path)
    policy_store(tmp_path, monkeypatch)
    moment = at_hour(10)
    authority.request(
        NotificationRequest(kind="proactive.digest", summary="held",
                            channel="telegram", proactive=True, dedupe_key="g1"),
        now=moment,
    )
    authority.request(
        NotificationRequest(kind="stuck.nudge", summary="old",
                            channel="telegram", proactive=True, dedupe_key="g2"),
        now=moment - 2 * 86_400,
    )

    assert dismiss_latest_proactive(authority, now=moment) == ""


def test_dismiss_latest_skips_rows_that_never_reached_him(tmp_path, monkeypatch):
    from core.interrupt_policy import dismiss_latest_proactive

    states = {"typing": False}
    authority = make_authority(
        tmp_path,
        presence=lambda: PresenceState(typing=states["typing"]),
        senders={"telegram": lambda request: True})
    policy_store(tmp_path, monkeypatch)
    moment = at_hour(10)
    authority.request(
        NotificationRequest(kind="stuck.nudge", summary="seen",
                            channel="telegram", proactive=True, dedupe_key="s1"),
        now=moment,
    )
    states["typing"] = True
    held = authority.request(
        NotificationRequest(kind="support.checkin", summary="held",
                            channel="telegram", proactive=True, dedupe_key="s2"),
        now=moment + 60.0,
    )
    assert held.decision == "deferred"

    assert dismiss_latest_proactive(authority, now=moment + 120.0) == "stuck.nudge"


def test_presence_now_reads_typing_and_class_from_the_buffer(tmp_path):
    db = tmp_path / "ambient.sqlite3"
    store = AmbientStore(db)
    now = 7_000_000.0
    store.record("window", app="code", title="a.py", now=now - 2.0)

    presence = presence_now(store=store, now=now)

    assert presence.typing is True
    assert presence.activity_class == "focused"

    quiet = presence_now(store=store, now=now + 3_600.0)
    assert quiet.typing is False


def test_presence_now_ignores_agent_driven_activity(tmp_path):
    db = tmp_path / "ambient.sqlite3"
    store = AmbientStore(db)
    now = 7_000_000.0
    store.record("window", app="code", title="a.py", agent_driven=True, now=now - 2.0)

    presence = presence_now(store=store, now=now)

    assert presence.typing is False
