"""Adversarial tests for Serena's bounded extensibility and automation.

Covers ws-8: the typed plugin manifest and lifecycle, the bounded scheduler,
the signed webhook foundation, and the single notification authority. Each
test is a way the boundary could be crossed if it were decorative.
"""

from __future__ import annotations

import threading
import time

import pytest

from core.control_plane import ControlPlaneStore
from core.notification_authority import (
    NotificationAuthority,
    NotificationError,
    NotificationPolicy,
    NotificationRequest,
)
from core.serena_plugins import (
    PluginLifecycleError,
    PluginManifestError,
    PluginRegistry,
    diff_manifests,
    validate_manifest,
)
from core.serena_scheduler import (
    MAX_CONSECUTIVE_FAILURES,
    ActionOutcome,
    SchedulerError,
    SerenaScheduler,
)
from core.webhook_signing import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WebhookSignatureError,
    sign,
    verify,
    verify_headers,
)

SECRET = "a-sufficiently-long-shared-secret"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Never let a test touch Raghav's real durable stores.

    The notification authority mirrors outcomes into the shared control plane,
    which defaults to his live database. Every store used here is redirected
    into the per-test directory.
    """

    monkeypatch.setenv("SERENA_CONTROL_PLANE_DB_PATH", str(tmp_path / "control.sqlite3"))
    monkeypatch.setenv("SERENA_NOTIFICATION_DB_PATH", str(tmp_path / "notify.sqlite3"))
    monkeypatch.setenv("SERENA_PLUGIN_DB_PATH", str(tmp_path / "plugins.sqlite3"))
    monkeypatch.setenv("SERENA_SCHEDULER_DB_PATH", str(tmp_path / "scheduler.sqlite3"))
    monkeypatch.setenv(
        "SERENA_WEBHOOK_REPLAY_DB_PATH", str(tmp_path / "webhook-replays.sqlite3")
    )
    monkeypatch.delenv("SERENA_FLEET_DB_PATH", raising=False)


def _manifest(**overrides):
    base = {
        "schema_version": 1,
        "id": "serena.notes",
        "name": "Notes",
        "version": "1.0.0",
        "description": "A small notes surface",
        "entrypoint": "plugins/notes/main.py",
        "tools": [{"name": "add_note", "description": "add one note", "scopes": ["memory.read"]}],
        "ui": [{"surface": "chat_sidebar", "title": "Notes"}],
        "hooks": [{"event": "chat.turn.completed", "handler": "on_turn"}],
        "permissions": {"scopes": ["memory.read"], "filesystem": ["docs/notes"], "network": []},
        "secrets": [{"name": "NOTES_TOKEN", "ref": "env:NOTES_TOKEN"}],
        "health": {"check": "status", "interval_seconds": 300},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- manifests


def test_a_well_formed_manifest_validates():
    manifest = validate_manifest(_manifest())
    assert manifest.id == "serena.notes"
    assert manifest.scopes == ("memory.read",)
    assert manifest.sensitive_scopes == ()


def test_an_unknown_manifest_field_is_refused():
    with pytest.raises(PluginManifestError, match="unknown field"):
        validate_manifest(_manifest(autorun=True))


def test_an_unknown_permission_scope_is_refused():
    payload = _manifest()
    payload["permissions"]["scopes"] = ["memory.read", "root.everything"]
    with pytest.raises(PluginManifestError, match="unknown permission scope"):
        validate_manifest(payload)


def test_an_inline_secret_is_refused():
    """A manifest must never become a place credentials live."""

    payload = _manifest(secrets=[{"name": "NOTES_TOKEN", "value": "sk-live-abcdef"}])
    with pytest.raises(PluginManifestError, match="by reference, not by value"):
        validate_manifest(payload)


@pytest.mark.parametrize("ref", ["sk-live-1234", "NOTES_TOKEN", "http://x/y", ""])
def test_a_secret_without_a_proper_reference_is_refused(ref):
    with pytest.raises(PluginManifestError, match="secret ref"):
        validate_manifest(_manifest(secrets=[{"name": "NOTES_TOKEN", "ref": ref}]))


@pytest.mark.parametrize(
    "ref", ["file:/etc/passwd", "file:../../etc/passwd", "file:~/.ssh/id_rsa"]
)
def test_a_file_secret_reference_cannot_escape_the_repository(ref):
    with pytest.raises(PluginManifestError, match="secret ref|relative path"):
        validate_manifest(_manifest(secrets=[{"name": "NOTES_TOKEN", "ref": ref}]))


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../../etc/passwd", "~/.ssh/id_rsa", ".git/config", "systemd/x.service"],
)
def test_an_escaping_or_protected_entrypoint_is_refused(path):
    with pytest.raises(PluginManifestError):
        validate_manifest(_manifest(entrypoint=path))


def test_a_protected_filesystem_permission_is_refused():
    payload = _manifest()
    payload["permissions"]["filesystem"] = [".ssh/id_rsa"]
    with pytest.raises(PluginManifestError, match="protected path"):
        validate_manifest(payload)


@pytest.mark.parametrize("host", ["*", "*.evil.com", "any", "0.0.0.0"])
def test_a_wildcard_network_permission_is_refused(host):
    payload = _manifest()
    payload["permissions"]["network"] = [host]
    with pytest.raises(PluginManifestError, match="wildcard|hostname"):
        validate_manifest(payload)


def test_an_unknown_hook_event_is_refused():
    with pytest.raises(PluginManifestError, match="unknown hook event"):
        validate_manifest(_manifest(hooks=[{"event": "system.root", "handler": "go"}]))


def test_an_unknown_ui_surface_is_refused():
    with pytest.raises(PluginManifestError, match="unknown UI surface"):
        validate_manifest(_manifest(ui=[{"surface": "kernel", "title": "x"}]))


def test_a_bad_schema_version_is_refused():
    with pytest.raises(PluginManifestError, match="schema version"):
        validate_manifest(_manifest(schema_version=99))


def test_the_diff_flags_privilege_escalation():
    current = validate_manifest(_manifest())
    escalated = _manifest()
    escalated["permissions"]["scopes"] = ["memory.read", "notify.send"]
    change = diff_manifests(current, validate_manifest(escalated))
    assert change["added_scopes"] == ["notify.send"]
    assert change["escalates_privilege"] is True


# ---------------------------------------------------------------- lifecycle


@pytest.fixture
def registry(tmp_path):
    return PluginRegistry(tmp_path / "plugins.sqlite3")


def test_a_plugin_cannot_be_installed_without_an_approved_stage(registry):
    staged = registry.stage(_manifest(), actor="raghav")
    assert staged["state"] == "pending"
    assert registry.get("serena.notes") is None, "staging must not install"

    registry.approve_stage(staged["stage_id"], actor="raghav")
    assert registry.require("serena.notes")["state"] == "installed"


def test_staging_requires_a_named_actor(registry):
    with pytest.raises(PluginLifecycleError, match="named actor"):
        registry.stage(_manifest(), actor="  ")


def test_lifecycle_transitions_are_ordered(registry):
    staged = registry.stage(_manifest(), actor="raghav")
    registry.approve_stage(staged["stage_id"], actor="raghav")

    with pytest.raises(PluginLifecycleError, match="cannot move"):
        registry.transition("serena.notes", "installed", actor="raghav")

    assert registry.transition("serena.notes", "enabled", actor="raghav")["state"] == "enabled"
    assert registry.transition("serena.notes", "disabled", actor="raghav")["state"] == "disabled"
    assert registry.transition("serena.notes", "removed", actor="raghav")["state"] == "removed"
    with pytest.raises(PluginLifecycleError, match="cannot move"):
        registry.transition("serena.notes", "enabled", actor="raghav")


def test_a_disabled_plugin_holds_no_scopes(registry):
    staged = registry.stage(_manifest(), actor="raghav")
    registry.approve_stage(staged["stage_id"], actor="raghav")
    registry.transition("serena.notes", "enabled", actor="raghav")
    assert registry.enabled_scopes("serena.notes") == ("memory.read",)

    registry.transition("serena.notes", "disabled", actor="raghav")
    assert registry.enabled_scopes("serena.notes") == ()


def test_installed_but_not_enabled_holds_no_scopes(registry):
    staged = registry.stage(_manifest(), actor="raghav")
    registry.approve_stage(staged["stage_id"], actor="raghav")
    assert registry.enabled_scopes("serena.notes") == ()


def test_a_sensitive_scope_needs_its_own_approved_manifest(registry):
    """Editing the manifest on disk after approval must not grant new reach."""

    staged = registry.stage(_manifest(), actor="raghav")
    registry.approve_stage(staged["stage_id"], actor="raghav")

    escalated = _manifest()
    escalated["permissions"]["scopes"] = ["memory.read", "notify.send"]
    # Simulate the record being updated to a more powerful manifest without a
    # matching approved stage, then try to enable it.
    import json
    import sqlite3

    with sqlite3.connect(registry.path) as connection:
        connection.execute(
            "UPDATE plugins SET manifest_json = ? WHERE plugin_id = ?",
            (
                json.dumps(
                    validate_manifest(escalated).to_dict(), sort_keys=True, separators=(",", ":")
                ),
                "serena.notes",
            ),
        )

    with pytest.raises(PluginLifecycleError, match="sensitive scopes"):
        registry.transition("serena.notes", "enabled", actor="raghav")


def test_health_and_audit_are_recorded(registry):
    staged = registry.stage(_manifest(), actor="raghav")
    registry.approve_stage(staged["stage_id"], actor="raghav")
    registry.record_health("serena.notes", healthy=False, detail="entrypoint missing")
    record = registry.require("serena.notes")
    assert record["health_state"] == "unhealthy"
    assert "entrypoint missing" in record["health_detail"]
    assert any(entry["action"] == "approved" for entry in registry.audit("serena.notes"))


# ---------------------------------------------------------------- scheduler


@pytest.fixture
def scheduler(tmp_path):
    return SerenaScheduler(tmp_path / "scheduler.sqlite3", notifier=None)


def test_only_reviewed_actions_can_be_scheduled(scheduler):
    with pytest.raises(SchedulerError, match="unknown scheduler action"):
        scheduler.add_schedule(action="rm -rf /", interval_seconds=3600, actor="raghav")


def test_an_interval_below_the_floor_is_refused(scheduler):
    scheduler.register_action("ping", lambda payload: ActionOutcome(True))
    with pytest.raises(SchedulerError, match="interval_seconds must be between"):
        scheduler.add_schedule(action="ping", interval_seconds=5, actor="raghav")


def test_a_schedule_needs_approval_before_it_runs(scheduler):
    calls: list = []
    scheduler.register_action("ping", lambda payload: calls.append(payload) or ActionOutcome(True))
    record = scheduler.add_schedule(
        action="ping", interval_seconds=60, actor="raghav", first_run_at=0
    )
    assert record["state"] == "pending_approval"

    assert scheduler.tick(now=1_000) == []
    assert calls == []

    scheduler.approve(record["schedule_id"], actor="raghav")
    runs = scheduler.tick(now=1_000)
    assert len(runs) == 1 and runs[0].ok is True
    assert calls == [{}]


def test_set_state_cannot_bypass_the_approval_transition(scheduler):
    scheduler.register_action("ping", lambda _payload: ActionOutcome(True))
    record = scheduler.add_schedule(
        action="ping", interval_seconds=60, actor="raghav", first_run_at=0
    )
    with pytest.raises(SchedulerError, match="approve/resume"):
        scheduler.set_state(record["schedule_id"], "active")
    assert scheduler.require(record["schedule_id"])["state"] == "pending_approval"


def test_run_now_refuses_pending_and_paused_schedules(scheduler):
    scheduler.register_action("ping", lambda _payload: ActionOutcome(True))
    record = scheduler.add_schedule(
        action="ping", interval_seconds=60, actor="raghav", first_run_at=0
    )
    with pytest.raises(SchedulerError, match="only for an active"):
        scheduler.run_now(record["schedule_id"], now=1_000)
    scheduler.approve(record["schedule_id"], actor="raghav")
    scheduler.set_state(record["schedule_id"], "paused")
    with pytest.raises(SchedulerError, match="only for an active"):
        scheduler.run_now(record["schedule_id"], now=1_000)


def test_concurrent_ticks_cannot_run_one_schedule_twice(scheduler):
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def handler(_payload):
        calls.append(1)
        entered.set()
        assert release.wait(timeout=3)
        return ActionOutcome(True)

    scheduler.register_action("ping", handler)
    scheduler.add_schedule(
        action="ping",
        interval_seconds=60,
        actor="raghav",
        requires_approval=False,
        first_run_at=0,
    )
    first_results: list = []
    worker = threading.Thread(
        target=lambda: first_results.extend(scheduler.tick(now=1_000)), daemon=True
    )
    worker.start()
    assert entered.wait(timeout=3)
    assert scheduler.tick(now=1_000) == []
    release.set()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert len(first_results) == 1
    assert calls == [1]


def test_pause_during_a_running_handler_is_not_overwritten(scheduler):
    entered = threading.Event()
    release = threading.Event()

    def handler(_payload):
        entered.set()
        assert release.wait(timeout=3)
        return ActionOutcome(True)

    scheduler.register_action("ping", handler)
    record = scheduler.add_schedule(
        action="ping",
        interval_seconds=60,
        actor="raghav",
        requires_approval=False,
        first_run_at=0,
    )
    worker = threading.Thread(target=lambda: scheduler.tick(now=1_000), daemon=True)
    worker.start()
    assert entered.wait(timeout=3)
    scheduler.set_state(record["schedule_id"], "paused")
    release.set()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert scheduler.require(record["schedule_id"])["state"] == "paused"


def test_a_repeatedly_failing_schedule_is_disabled(scheduler):
    scheduler.register_action("boom", lambda payload: ActionOutcome(False, "nope"))
    record = scheduler.add_schedule(
        action="boom",
        interval_seconds=60,
        actor="raghav",
        requires_approval=False,
        first_run_at=0,
    )
    schedule_id = record["schedule_id"]
    for index in range(MAX_CONSECUTIVE_FAILURES):
        scheduler.tick(now=1_000 + index * 60)
    assert scheduler.require(schedule_id)["state"] == "disabled"
    assert scheduler.tick(now=10_000) == []


def test_a_raising_handler_is_recorded_not_propagated(scheduler):
    def explode(payload):
        raise RuntimeError("handler blew up")

    scheduler.register_action("explode", explode)
    record = scheduler.add_schedule(
        action="explode",
        interval_seconds=60,
        actor="raghav",
        requires_approval=False,
        first_run_at=0,
    )
    runs = scheduler.tick(now=1_000)
    assert runs[0].ok is False
    assert "handler blew up" in runs[0].detail
    assert scheduler.history(record["schedule_id"])[0]["ok"] == 0


def test_a_successful_run_reschedules_forward(scheduler):
    scheduler.register_action("ping", lambda payload: ActionOutcome(True))
    record = scheduler.add_schedule(
        action="ping",
        interval_seconds=600,
        actor="raghav",
        requires_approval=False,
        first_run_at=0,
    )
    scheduler.tick(now=1_000)
    assert scheduler.require(record["schedule_id"])["next_run_at"] == 1_600
    assert scheduler.tick(now=1_100) == []


def test_scheduled_notices_go_through_the_notification_authority(tmp_path):
    sent: list = []
    authority = NotificationAuthority(
        tmp_path / "notify.sqlite3",
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0),
        senders={"voice": lambda request: sent.append(request.summary) or True},
    )
    scheduler = SerenaScheduler(tmp_path / "sched.sqlite3", notifier=authority)
    scheduler.register_action(
        "remind",
        lambda payload: ActionOutcome(True, notify={"summary": "the backup finished"}),
    )
    record = scheduler.add_schedule(
        action="remind",
        interval_seconds=60,
        actor="raghav",
        requires_approval=False,
        first_run_at=0,
    )
    assert record["state"] == "active"
    scheduler.tick(now=time.time())
    assert sent == ["the backup finished"]


def test_scheduler_tick_emits_one_bounded_plugin_event(tmp_path, monkeypatch):
    hooks: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "core.plugin_loader.emit_plugin_hook",
        lambda event, payload, **_kwargs: hooks.append((event, payload)) or [],
    )
    scheduler = SerenaScheduler(tmp_path / "sched.sqlite3")
    scheduler.register_action("ping", lambda _payload: ActionOutcome(True, "done"))
    scheduler.add_schedule(
        action="ping",
        interval_seconds=60,
        actor="raghav",
        requires_approval=False,
        first_run_at=0,
    )

    scheduler.tick(now=1_000)

    assert hooks == [
        ("schedule.tick", {"run_count": 1, "succeeded": 1, "failed": 0})
    ]


# ------------------------------------------------------- notification authority


@pytest.fixture
def authority(tmp_path):
    sent: list = []
    instance = NotificationAuthority(
        tmp_path / "notify.sqlite3",
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0),
        senders={"voice": lambda request: sent.append(request.summary) or True},
    )
    instance.sent = sent  # type: ignore[attr-defined]
    return instance


def _request(**overrides):
    base = {"kind": "fleet.run.completed", "summary": "the run finished", "channel": "voice"}
    base.update(overrides)
    return NotificationRequest(**base)


def test_a_notice_is_delivered_and_recorded(authority):
    result = authority.request(_request())
    assert result.sent is True
    assert authority.sent == ["the run finished"]
    assert authority.history()[0]["decision"] == "sent"


def test_a_sent_notice_emits_only_bounded_plugin_metadata(authority, monkeypatch):
    hooks: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "core.plugin_loader.emit_plugin_hook",
        lambda event, payload, **_kwargs: hooks.append((event, payload)) or [],
    )

    result = authority.request(
        _request(source_surface="fleet", job_id="run-1", metadata={"private": "not emitted"})
    )

    assert hooks == [
        (
            "notification.sent",
            {
                "notification_id": result.notification_id,
                "kind": "fleet.run.completed",
                "channel": "voice",
                "source_surface": "fleet",
                "job_id": "run-1",
            },
        )
    ]


def test_notification_outbox_drives_one_fulfilled_obligation_without_a_source_job(tmp_path):
    control = ControlPlaneStore(tmp_path / "control.sqlite3")
    instance = NotificationAuthority(
        tmp_path / "notify.sqlite3",
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0),
        senders={"voice": lambda _request: True},
        control_store=control,
    )

    result = instance.request(_request())

    events = control.events(surface="notification", job_id=result.notification_id)
    assert {event.event_type for event in events} == {"notice.queued", "notice.delivered"}
    obligations = control.obligations(state="fulfilled", surface="notification")
    assert len(obligations) == 1
    assert obligations[0].job_id == result.notification_id


def test_failed_notification_attempt_stays_open_with_durable_evidence(tmp_path):
    control = ControlPlaneStore(tmp_path / "control.sqlite3")
    instance = NotificationAuthority(
        tmp_path / "notify.sqlite3",
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0),
        senders={"voice": lambda _request: False},
        control_store=control,
    )

    result = instance.request(_request())

    assert result.decision == "failed"
    obligations = control.obligations(state="open", surface="notification")
    assert len(obligations) == 1
    assert obligations[0].attempts == 1
    assert "not delivered" in str(obligations[0].last_error)


def test_notification_outbox_survives_control_plane_failure_and_restart(tmp_path):
    class BrokenControl:
        def append_envelope(self, _envelope):
            raise RuntimeError("control plane unavailable")

    path = tmp_path / "notify.sqlite3"
    first = NotificationAuthority(
        path,
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0),
        senders={"voice": lambda _request: True},
        control_store=BrokenControl(),
    )
    result = first.request(_request())
    assert first.pending_control_events() == 2

    control = ControlPlaneStore(tmp_path / "control.sqlite3")
    reopened = NotificationAuthority(path, control_store=control)
    assert reopened.flush_control_outbox() == 2
    assert reopened.pending_control_events() == 0
    obligations = control.obligations(state="fulfilled", surface="notification")
    assert [item.job_id for item in obligations] == [result.notification_id]


def test_a_duplicate_notice_within_the_window_is_suppressed(authority):
    first = authority.request(_request(dedupe_key="run-123"))
    second = authority.request(_request(dedupe_key="run-123"))
    assert first.sent is True
    assert second.decision == "suppressed"
    assert authority.sent == ["the run finished"], "he must not be told twice"


def test_quiet_hours_defer_a_normal_notice(tmp_path):
    sent: list = []
    instance = NotificationAuthority(
        tmp_path / "notify.sqlite3",
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=23),
        senders={"voice": lambda request: sent.append(request.summary) or True},
    )
    result = instance.request(_request())
    assert result.decision == "deferred"
    assert result.deliver_after is not None
    assert sent == []


def test_a_critical_notice_ignores_quiet_hours(tmp_path):
    sent: list = []
    instance = NotificationAuthority(
        tmp_path / "notify.sqlite3",
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=23),
        senders={"voice": lambda request: sent.append(request.summary) or True},
    )
    assert instance.request(_request(urgency="critical")).sent is True
    assert sent == ["the run finished"]


def test_the_hourly_limit_suppresses_a_flood(tmp_path):
    sent: list = []
    instance = NotificationAuthority(
        tmp_path / "notify.sqlite3",
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0, hourly_limit=3),
        senders={"voice": lambda request: sent.append(request.summary) or True},
    )
    decisions = [
        instance.request(_request(dedupe_key=f"event-{index}")).decision for index in range(6)
    ]
    assert decisions.count("sent") == 3
    assert decisions.count("suppressed") == 3
    assert len(sent) == 3


def test_an_approval_required_kind_is_held_then_released(tmp_path):
    sent: list = []
    instance = NotificationAuthority(
        tmp_path / "notify.sqlite3",
        policy=NotificationPolicy(
            quiet_start_hour=0,
            quiet_end_hour=0,
            approval_required_kinds=("memory.rewrite",),
        ),
        senders={"voice": lambda request: sent.append(request.summary) or True},
    )
    held = instance.request(_request(kind="memory.rewrite"))
    assert held.decision == "pending_approval"
    assert sent == []
    assert len(instance.pending_approvals()) == 1

    released = instance.approve(held.notification_id)
    assert released.sent is True
    assert sent == ["the run finished"]


def test_a_failing_channel_is_retried_within_a_bounded_budget(tmp_path):
    attempts = {"count": 0}

    def flaky(request):
        attempts["count"] += 1
        return attempts["count"] > 2

    instance = NotificationAuthority(
        tmp_path / "notify.sqlite3",
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0, max_attempts=4),
        senders={"voice": flaky},
    )
    first = instance.request(_request())
    assert first.decision == "failed"
    assert first.deliver_after is not None

    later = instance.deliver_due(now=first.deliver_after + 1)
    assert later and later[0].decision == "failed"
    final = instance.deliver_due(now=time.time() + 100_000)
    assert final and final[0].sent is True


def test_retries_stop_at_the_attempt_ceiling(tmp_path):
    instance = NotificationAuthority(
        tmp_path / "notify.sqlite3",
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0, max_attempts=2),
        senders={"voice": lambda request: False},
    )
    instance.request(_request())
    instance.deliver_due(now=time.time() + 100_000)
    assert instance.deliver_due(now=time.time() + 200_000) == []


def test_an_unknown_channel_is_refused(authority):
    with pytest.raises(NotificationError, match="unknown notification channel"):
        authority.request(_request(channel="sms"))


def test_a_channel_with_no_sender_fails_closed(tmp_path):
    instance = NotificationAuthority(
        tmp_path / "notify.sqlite3",
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0),
        senders={},
    )
    result = instance.request(_request())
    assert result.decision == "failed"
    assert "no sender" in result.reason


# ------------------------------------------------------------ webhook signing


def test_a_signed_payload_verifies():
    body = b'{"event":"ping"}'
    signed = sign(body, SECRET)
    ok, reason = verify(
        body, SECRET, signature=signed.signature, timestamp=signed.timestamp
    )
    assert ok is True and reason == "verified"


def test_a_tampered_body_fails_verification():
    signed = sign(b'{"amount":1}', SECRET)
    ok, reason = verify(
        b'{"amount":1000}', SECRET, signature=signed.signature, timestamp=signed.timestamp
    )
    assert ok is False and "did not match" in reason


def test_a_wrong_secret_fails_verification():
    body = b"payload"
    signed = sign(body, SECRET)
    ok, _reason = verify(
        body, "another-sufficiently-long-secret", signature=signed.signature,
        timestamp=signed.timestamp,
    )
    assert ok is False


def test_a_replayed_signature_outside_the_window_fails():
    body = b"payload"
    signed = sign(body, SECRET, timestamp=1_000)
    ok, reason = verify(
        body, SECRET, signature=signed.signature, timestamp=signed.timestamp, now=1_000 + 4_000
    )
    assert ok is False and "replay window" in reason


def test_a_future_dated_signature_fails():
    body = b"payload"
    signed = sign(body, SECRET, timestamp=9_000)
    ok, reason = verify(
        body, SECRET, signature=signed.signature, timestamp=signed.timestamp, now=1_000
    )
    assert ok is False and "future" in reason


def test_moving_a_signature_to_a_new_timestamp_fails():
    """The timestamp is inside the signed string, so it cannot be swapped."""

    body = b"payload"
    signed = sign(body, SECRET, timestamp=1_000)
    ok, _reason = verify(body, SECRET, signature=signed.signature, timestamp=1_100, now=1_100)
    assert ok is False


@pytest.mark.parametrize(
    "signature",
    ["", "deadbeef", "v1=nothex", "v2=" + "a" * 64, "v1=" + "a" * 63],
)
def test_a_malformed_signature_header_fails(signature):
    ok, _reason = verify(b"payload", SECRET, signature=signature, timestamp=int(time.time()))
    assert ok is False


def test_a_short_secret_is_refused():
    with pytest.raises(WebhookSignatureError, match="too short"):
        sign(b"payload", "tiny")


def test_a_missing_timestamp_fails():
    signed = sign(b"payload", SECRET)
    ok, reason = verify(b"payload", SECRET, signature=signed.signature, timestamp=None)
    assert ok is False and "timestamp" in reason


def test_header_helper_is_case_insensitive():
    body = b"payload"
    signed = sign(body, SECRET)
    ok, _reason = verify_headers(
        body,
        SECRET,
        {
            SIGNATURE_HEADER.lower(): signed.signature,
            TIMESTAMP_HEADER.upper(): str(signed.timestamp),
        },
    )
    assert ok is True


def test_header_helper_rejects_a_second_delivery_of_the_same_valid_request(tmp_path):
    body = b"payload"
    signed = sign(body, SECRET, timestamp=1_000)
    headers = signed.headers()
    first = verify_headers(body, SECRET, headers, now=1_000)
    second = verify_headers(body, SECRET, headers, now=1_001)
    assert first == (True, "verified")
    assert second[0] is False and "already consumed" in second[1]
