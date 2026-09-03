"""Adversarial tests for Serena's bounded automation.

Covers ws-6: the scheduler's one-shot/edit/remove/chain/workdir behaviour, the
resident loop that finally makes any of it run, the single notification
authority, and the signed webhook ingress. Each test is a way the boundary
could be crossed if it were decorative.
"""

from __future__ import annotations

import threading
import time

import pytest

from core.automation_runtime import AutomationRuntime, PassReport
from core.notification_authority import (
    NotificationAuthority,
    NotificationPolicy,
    NotificationRequest,
)
from core.serena_scheduler import (
    MAX_CHAIN_FANOUT,
    ActionOutcome,
    SchedulerError,
    SerenaScheduler,
)
from core.webhook_ingress import (
    RouteOutcome,
    WebhookIngress,
    WebhookIngressError,
    default_ingress,
)
from core.webhook_signing import WebhookReplayStore, sign

SECRET = "a-sufficiently-long-shared-secret"


@pytest.fixture
def scheduler(tmp_path):
    return SerenaScheduler(tmp_path / "scheduler.sqlite3", notifier=None)


def _add(scheduler, action="ping", **kwargs):
    kwargs.setdefault("interval_seconds", 60)
    kwargs.setdefault("actor", "raghav")
    kwargs.setdefault("requires_approval", False)
    kwargs.setdefault("first_run_at", 0)
    return scheduler.add_schedule(action=action, **kwargs)


# ---------------------------------------------------------- one-shot jobs


def test_a_one_shot_job_runs_once_and_retires(scheduler):
    calls: list = []
    scheduler.register_action("ping", lambda p: calls.append(p) or ActionOutcome(True))
    record = _add(scheduler, one_shot=True)

    assert len(scheduler.tick(now=1_000)) == 1
    assert scheduler.require(record["schedule_id"])["state"] == "completed"
    assert scheduler.tick(now=1_000_000) == []
    assert len(calls) == 1


def test_a_failed_one_shot_job_stays_alive_to_retry(scheduler):
    scheduler.register_action("ping", lambda _p: ActionOutcome(False, "not yet"))
    record = _add(scheduler, one_shot=True)

    scheduler.tick(now=1_000)

    # It never succeeded, so retiring it would silently drop what he asked for.
    assert scheduler.require(record["schedule_id"])["state"] == "active"


def test_a_recurring_job_keeps_its_place_in_the_rotation(scheduler):
    scheduler.register_action("ping", lambda _p: ActionOutcome(True))
    record = _add(scheduler, interval_seconds=600)

    scheduler.tick(now=1_000)

    schedule = scheduler.require(record["schedule_id"])
    assert schedule["state"] == "active"
    assert schedule["next_run_at"] == 1_600


# ------------------------------------------------------------ edit/remove


def test_edit_changes_the_interval_and_payload(scheduler):
    seen: list = []
    scheduler.register_action("ping", lambda p: seen.append(p) or ActionOutcome(True))
    record = _add(scheduler, payload={"who": "old"})

    scheduler.edit(
        record["schedule_id"], actor="raghav", interval_seconds=900, payload={"who": "new"}
    )
    scheduler.tick(now=1_000)

    assert seen == [{"who": "new"}]
    assert scheduler.require(record["schedule_id"])["interval_seconds"] == 900


def test_edit_cannot_swap_the_action_out_from_under_an_approval(scheduler):
    scheduler.register_action("ping", lambda _p: ActionOutcome(True))
    scheduler.register_action("dangerous", lambda _p: ActionOutcome(True))
    record = _add(scheduler)

    # There is deliberately no `action=` parameter to pass.
    with pytest.raises(TypeError):
        scheduler.edit(record["schedule_id"], actor="raghav", action="dangerous")
    assert scheduler.require(record["schedule_id"])["action"] == "ping"


def test_edit_requires_a_named_actor(scheduler):
    scheduler.register_action("ping", lambda _p: ActionOutcome(True))
    record = _add(scheduler)
    with pytest.raises(SchedulerError, match="named actor"):
        scheduler.edit(record["schedule_id"], actor="   ", interval_seconds=900)


def test_remove_retires_a_schedule_but_keeps_its_history(scheduler):
    scheduler.register_action("ping", lambda _p: ActionOutcome(True))
    record = _add(scheduler)
    scheduler.tick(now=1_000)

    scheduler.remove(record["schedule_id"], actor="raghav")

    assert scheduler.require(record["schedule_id"])["state"] == "removed"
    assert scheduler.tick(now=1_000_000) == []
    assert len(scheduler.history(record["schedule_id"])) >= 2


def test_a_removed_schedule_cannot_be_edited(scheduler):
    scheduler.register_action("ping", lambda _p: ActionOutcome(True))
    record = _add(scheduler)
    scheduler.remove(record["schedule_id"], actor="raghav")

    with pytest.raises(SchedulerError, match="cannot be edited"):
        scheduler.edit(record["schedule_id"], actor="raghav", interval_seconds=900)


def test_a_removed_schedule_cannot_be_resumed(scheduler):
    scheduler.register_action("ping", lambda _p: ActionOutcome(True))
    record = _add(scheduler)
    scheduler.remove(record["schedule_id"], actor="raghav")

    with pytest.raises(SchedulerError, match="no paused or disabled"):
        scheduler.resume(record["schedule_id"], actor="raghav")


# ------------------------------------------------------------- workdirs


def test_a_project_workdir_reaches_the_handler(scheduler, tmp_path):
    seen: list = []
    scheduler.register_action("ping", lambda p: seen.append(p) or ActionOutcome(True))
    project = tmp_path / "project"
    project.mkdir()
    _add(scheduler, workdir=project)

    scheduler.tick(now=1_000)

    assert seen[0]["workdir"] == str(project.resolve())


def test_a_workdir_that_does_not_exist_is_refused_at_write_time(scheduler, tmp_path):
    scheduler.register_action("ping", lambda _p: ActionOutcome(True))
    with pytest.raises(SchedulerError, match="does not exist"):
        _add(scheduler, workdir=tmp_path / "nope")


def test_a_relative_workdir_is_refused(scheduler):
    scheduler.register_action("ping", lambda _p: ActionOutcome(True))
    with pytest.raises(SchedulerError, match="absolute"):
        _add(scheduler, workdir="../somewhere")


def test_a_plain_schedule_still_sees_exactly_its_own_payload(scheduler):
    seen: list = []
    scheduler.register_action("ping", lambda p: seen.append(p) or ActionOutcome(True))
    _add(scheduler)

    scheduler.tick(now=1_000)

    assert seen == [{}]


# ------------------------------------------------------ chaining / fan-out


def test_a_successful_run_hands_its_output_to_the_next_job(scheduler):
    seen: list = []
    scheduler.register_action(
        "first", lambda _p: ActionOutcome(True, output={"rows": 3})
    )
    scheduler.register_action("second", lambda p: seen.append(p) or ActionOutcome(True))
    second = _add(scheduler, "second", first_run_at=1e12)
    _add(scheduler, "first", chain_to=[second["schedule_id"]])

    scheduler.drain(now=1_000)

    assert seen and seen[0]["chain_input"]["output"] == {"rows": 3}


def test_a_failed_run_does_not_wake_its_successor(scheduler):
    seen: list = []
    scheduler.register_action("first", lambda _p: ActionOutcome(False, "nope"))
    scheduler.register_action("second", lambda p: seen.append(p) or ActionOutcome(True))
    second = _add(scheduler, "second", first_run_at=1e12)
    _add(scheduler, "first", chain_to=[second["schedule_id"]])

    scheduler.drain(now=1_000)

    assert seen == []


def test_fan_out_wakes_every_declared_successor(scheduler):
    seen: list = []
    scheduler.register_action("first", lambda _p: ActionOutcome(True))
    scheduler.register_action("leaf", lambda p: seen.append(1) or ActionOutcome(True))
    leaves = [_add(scheduler, "leaf", first_run_at=1e12)["schedule_id"] for _ in range(3)]
    _add(scheduler, "first", chain_to=leaves)

    scheduler.drain(now=1_000)

    assert len(seen) == 3


def test_fan_out_is_bounded(scheduler):
    scheduler.register_action("first", lambda _p: ActionOutcome(True))
    with pytest.raises(SchedulerError, match="at most"):
        _add(scheduler, "first", chain_to=[f"id-{i}" for i in range(MAX_CHAIN_FANOUT + 1)])


def test_a_chain_cycle_costs_a_bounded_number_of_runs_not_the_machine(scheduler):
    calls: list = []
    scheduler.register_action("loop", lambda _p: calls.append(1) or ActionOutcome(True))
    first = _add(scheduler, "loop")
    second = _add(scheduler, "loop", first_run_at=1e12)
    scheduler.edit(first["schedule_id"], actor="raghav", chain_to=[second["schedule_id"]])
    scheduler.edit(second["schedule_id"], actor="raghav", chain_to=[first["schedule_id"]])

    scheduler.drain(now=1_000)

    # Bounded by chain depth, not spinning until something breaks.
    assert 0 < len(calls) <= 8


def test_a_schedule_cannot_chain_to_itself(scheduler):
    scheduler.register_action("loop", lambda _p: ActionOutcome(True))
    record = _add(scheduler, "loop")
    with pytest.raises(SchedulerError, match="cannot chain to itself"):
        scheduler.edit(
            record["schedule_id"], actor="raghav", chain_to=[record["schedule_id"]]
        )


def test_chaining_cannot_wake_a_schedule_that_was_never_approved(scheduler):
    seen: list = []
    scheduler.register_action("first", lambda _p: ActionOutcome(True))
    scheduler.register_action("second", lambda p: seen.append(p) or ActionOutcome(True))
    second = scheduler.add_schedule(
        action="second",
        interval_seconds=60,
        actor="raghav",
        requires_approval=True,
        first_run_at=0,
    )
    _add(scheduler, "first", chain_to=[second["schedule_id"]])

    scheduler.drain(now=1_000)

    assert seen == []
    assert scheduler.require(second["schedule_id"])["state"] == "pending_approval"


# --------------------------------------------------------------- fan-in


def test_a_join_waits_for_every_parent(scheduler):
    seen: list = []
    scheduler.register_action("parent", lambda _p: ActionOutcome(True))
    scheduler.register_action("join", lambda p: seen.append(1) or ActionOutcome(True))
    left = _add(scheduler, "parent", first_run_at=1e12)
    right = _add(scheduler, "parent", first_run_at=1e12)
    _add(scheduler, "join", join_of=[left["schedule_id"], right["schedule_id"]])

    # Neither parent has succeeded, so the join is not due.
    assert scheduler.tick(now=1_000) == []
    assert seen == []

    scheduler.run_now(left["schedule_id"], now=1_100)
    assert scheduler.tick(now=1_200) == []
    assert seen == []

    scheduler.run_now(right["schedule_id"], now=1_300)
    scheduler.tick(now=1_400)
    assert len(seen) == 1


def test_a_join_does_not_re_fire_on_stale_parent_success(scheduler):
    seen: list = []
    scheduler.register_action("parent", lambda _p: ActionOutcome(True))
    scheduler.register_action("join", lambda p: seen.append(1) or ActionOutcome(True))
    parent = _add(scheduler, "parent", first_run_at=1e12)
    _add(scheduler, "join", join_of=[parent["schedule_id"]], interval_seconds=60)

    scheduler.run_now(parent["schedule_id"], now=1_100)
    scheduler.tick(now=1_200)
    assert len(seen) == 1

    # The parent has not succeeded again, so the join must stay quiet.
    scheduler.tick(now=5_000)
    assert len(seen) == 1


def test_a_schedule_cannot_wait_on_itself(scheduler):
    scheduler.register_action("join", lambda _p: ActionOutcome(True))
    record = _add(scheduler, "join")
    with pytest.raises(SchedulerError, match="cannot wait on itself"):
        scheduler.edit(
            record["schedule_id"], actor="raghav", join_of=[record["schedule_id"]]
        )


# ------------------------------------------------------------------ dedup


def test_an_equivalent_run_inside_the_window_is_skipped(scheduler):
    calls: list = []
    scheduler.register_action("ping", lambda _p: calls.append(1) or ActionOutcome(True))
    _add(scheduler, dedupe_key="nightly-backup", dedupe_window_seconds=3_600)
    other = _add(scheduler, dedupe_key="nightly-backup", dedupe_window_seconds=3_600)

    scheduler.tick(now=1_000)
    scheduler.run_now(other["schedule_id"], now=1_010)

    assert len(calls) == 1


def test_dedup_lets_the_run_through_once_the_window_passes(scheduler):
    calls: list = []
    scheduler.register_action("ping", lambda _p: calls.append(1) or ActionOutcome(True))
    record = _add(scheduler, dedupe_key="hourly", dedupe_window_seconds=60)

    scheduler.tick(now=1_000)
    scheduler.run_now(record["schedule_id"], now=1_100)

    assert len(calls) == 2


# ------------------------------------------------- the resident loop runs


def test_the_loop_actually_ticks_the_scheduler(tmp_path):
    calls: list = []
    scheduler = SerenaScheduler(tmp_path / "s.sqlite3", notifier=None)
    scheduler.register_action("ping", lambda _p: calls.append(1) or ActionOutcome(True))
    scheduler.add_schedule(
        action="ping",
        interval_seconds=60,
        actor="raghav",
        requires_approval=False,
        first_run_at=0,
    )
    runtime = AutomationRuntime(
        scheduler=scheduler,
        authority=_authority(tmp_path),
        capacity_probe_seconds=0,
        publish_journals=False,
    )

    report = runtime.run_pass(now=1_000)

    assert calls == [1]
    assert report.ran == ["ping:ok"]


def test_the_loop_delivers_notices_that_came_due(tmp_path):
    sent: list = []
    authority = NotificationAuthority(
        tmp_path / "n.sqlite3",
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=23),
        senders={"voice": lambda r: sent.append(r.summary) or True},
    )
    held = authority.request(
        NotificationRequest(kind="test", summary="the deferred one", channel="voice")
    )
    assert held.decision == "deferred" and sent == []

    runtime = AutomationRuntime(
        scheduler=_idle_scheduler(tmp_path),
        authority=authority,
        capacity_probe_seconds=0,
        publish_journals=False,
    )
    report = runtime.run_pass(now=held.deliver_after + 1)

    assert sent == ["the deferred one"]
    assert report.notifications == 1


def test_the_loop_holds_scheduled_work_when_both_providers_are_out(tmp_path):
    calls: list = []
    scheduler = SerenaScheduler(tmp_path / "s.sqlite3", notifier=None)
    scheduler.register_action("ping", lambda _p: calls.append(1) or ActionOutcome(True))
    scheduler.add_schedule(
        action="ping",
        interval_seconds=60,
        actor="raghav",
        requires_approval=False,
        first_run_at=0,
    )
    exhausted = {"value": True}
    runtime = AutomationRuntime(
        scheduler=scheduler,
        authority=_authority(tmp_path),
        capacity_reader=lambda: (exhausted["value"], "claude: usage limit"),
        capacity_probe_seconds=1,
        publish_journals=False,
    )

    held = runtime.run_pass(now=1_000)
    assert held.capacity_held is True
    assert calls == []

    # Capacity comes back and the hold lifts by itself.
    exhausted["value"] = False
    resumed = runtime.run_pass(now=2_000)
    assert resumed.capacity_held is False
    assert calls == [1]


def test_a_broken_scheduler_cannot_take_the_loop_down(tmp_path):
    class Exploding:
        def drain(self, **_kwargs):
            raise RuntimeError("the scheduler is broken")

    runtime = AutomationRuntime(
        scheduler=Exploding(),
        authority=_authority(tmp_path),
        capacity_probe_seconds=0,
        publish_journals=False,
    )
    report = runtime.run_pass(now=1_000)

    assert isinstance(report, PassReport)
    assert any("scheduler" in error for error in report.errors)


def test_serve_forever_stops_when_asked(tmp_path):
    runtime = AutomationRuntime(
        scheduler=_idle_scheduler(tmp_path),
        authority=_authority(tmp_path),
        poll_seconds=1,
        capacity_probe_seconds=0,
        publish_journals=False,
    )
    stop = threading.Event()
    passes = runtime.serve_forever(stop_event=stop, max_passes=3)
    assert passes == 3


def _authority(tmp_path):
    return NotificationAuthority(
        tmp_path / f"auth-{time.time_ns()}.sqlite3",
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0),
        senders={"voice": lambda _r: True},
    )


def _idle_scheduler(tmp_path):
    return SerenaScheduler(tmp_path / f"idle-{time.time_ns()}.sqlite3", notifier=None)


# ---------------------------------------------------------- webhook ingress


@pytest.fixture
def ingress(tmp_path):
    instance = WebhookIngress(
        tmp_path / "ingress.sqlite3",
        secret=SECRET,
        replay_store=WebhookReplayStore(tmp_path / "replays.sqlite3"),
    )
    instance.register("ping", lambda _p, _r: RouteOutcome(True, "pong"))
    return instance


def _post(ingress, route, body=b'{"hello":"there"}', *, secret=SECRET, now=1_000, **over):
    signed = sign(body, secret, timestamp=int(now))
    headers = dict(signed.headers())
    headers.update(over)
    return ingress.handle(route, body, headers, now=now)


def test_a_correctly_signed_request_reaches_its_route(ingress):
    result = _post(ingress, "ping")
    assert result.accepted is True
    assert result.reason == "pong"
    assert ingress.history()[0]["decision"] == "accepted"


def test_an_unsigned_request_is_rejected(ingress):
    result = ingress.handle("ping", b"{}", {}, now=1_000)
    assert result.decision == "rejected"
    assert result.status == 401


def test_a_tampered_body_is_rejected(ingress):
    signed = sign(b'{"amount":1}', SECRET, timestamp=1_000)
    result = ingress.handle("ping", b'{"amount":1000}', signed.headers(), now=1_000)
    assert result.decision == "rejected"
    assert "did not match" in result.reason


def test_a_replayed_request_is_rejected_even_inside_the_window(ingress):
    body = b'{"hello":"there"}'
    signed = sign(body, SECRET, timestamp=1_000)
    first = ingress.handle("ping", body, signed.headers(), now=1_000)
    second = ingress.handle("ping", body, signed.headers(), now=1_001)

    assert first.accepted is True
    assert second.decision == "rejected"
    assert "already consumed" in second.reason


def test_a_stale_signature_is_rejected(ingress):
    result = _post(ingress, "ping", now=1_000)
    assert result.accepted
    signed = sign(b"{}", SECRET, timestamp=1_000)
    late = ingress.handle("ping", b"{}", signed.headers(), now=1_000 + 100_000)
    assert late.decision == "rejected"
    assert "replay window" in late.reason


def test_an_unknown_route_is_refused_before_anything_runs(ingress):
    result = _post(ingress, "definitely-not-a-route")
    assert result.decision == "rejected"
    assert result.status == 404
    assert "unknown webhook route" in result.reason


def test_a_valid_signature_for_one_route_does_not_open_another(ingress):
    ingress.register("admin", lambda _p, _r: RouteOutcome(True, "ran"))
    body = b'{"x":1}'
    signed = sign(body, SECRET, timestamp=1_000)
    # Same valid signature, different route: the route is still checked.
    assert ingress.handle("admin", body, signed.headers(), now=1_000).accepted is True
    replay = ingress.handle("ping", body, signed.headers(), now=1_000)
    assert replay.decision == "rejected"


def test_an_ingress_with_no_secret_is_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("SERENA_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("SERENA_WEBHOOK_SECRET_FILE", raising=False)
    instance = WebhookIngress(tmp_path / "i.sqlite3", secret="")
    instance.register("ping", lambda _p, _r: RouteOutcome(True))

    result = instance.handle("ping", b"{}", {}, now=1_000)

    assert result.decision == "rejected"
    assert result.status == 503
    assert "no webhook secret" in result.reason


def test_a_non_object_body_is_rejected(ingress):
    result = _post(ingress, "ping", body=b'"just a string"')
    assert result.decision == "rejected"
    assert "JSON object" in result.reason


def test_an_oversized_body_is_rejected(ingress):
    huge = b'{"x":"' + b"a" * (300 * 1024) + b'"}'
    result = _post(ingress, "ping", body=huge)
    assert result.decision == "rejected"
    assert result.status == 413


def test_a_route_needing_approval_holds_and_runs_nothing(tmp_path):
    ran: list = []
    instance = WebhookIngress(
        tmp_path / "i.sqlite3",
        secret=SECRET,
        replay_store=WebhookReplayStore(tmp_path / "r.sqlite3"),
    )
    instance.register(
        "deploy",
        lambda _p, _r: ran.append(1) or RouteOutcome(True, "deployed"),
        requires_approval=True,
    )

    held = _post(instance, "deploy")

    assert held.decision == "held"
    assert held.status == 202
    assert ran == []
    assert len(instance.pending()) == 1

    released = instance.approve(held.delivery_id, actor="raghav")
    assert released.accepted is True
    assert ran == [1]


def test_approving_a_delivery_requires_an_actor(tmp_path):
    instance = WebhookIngress(
        tmp_path / "i.sqlite3",
        secret=SECRET,
        replay_store=WebhookReplayStore(tmp_path / "r.sqlite3"),
    )
    instance.register("deploy", lambda _p, _r: RouteOutcome(True), requires_approval=True)
    held = _post(instance, "deploy")

    with pytest.raises(WebhookIngressError, match="requires an actor"):
        instance.approve(held.delivery_id, actor="")


def test_a_raising_handler_is_recorded_not_propagated(ingress):
    def explode(_payload, _request):
        raise RuntimeError("the handler blew up")

    ingress.register("boom", explode)
    result = _post(ingress, "boom")

    assert result.decision == "rejected"
    assert "the handler blew up" in result.reason


def test_every_decision_lands_in_the_audit_trail(ingress):
    _post(ingress, "ping", now=1_000)
    ingress.handle("ping", b"{}", {}, now=1_001)
    _post(ingress, "nope", now=1_002)

    decisions = [entry["decision"] for entry in ingress.history()]
    assert decisions.count("accepted") == 1
    assert decisions.count("rejected") == 2


def test_a_rejected_body_is_not_stored_verbatim(ingress):
    _post(ingress, "ping", body=b'{"secret":"hunter2"}')
    row = ingress.history()[0]
    assert "hunter2" not in row["payload_json"]
    assert row["body_sha256"]


def test_an_outside_caller_cannot_declare_its_own_message_critical(tmp_path):
    requested: list = []

    class Recording:
        def request(self, request):
            requested.append(request)
            return type("R", (), {"sent": True, "decision": "sent"})()

    instance = default_ingress(
        path=tmp_path / "i.sqlite3",
        secret=SECRET,
        replay_store=WebhookReplayStore(tmp_path / "r.sqlite3"),
        authority=Recording(),
    )
    body = b'{"summary":"wake up","urgency":"critical"}'
    held = _post(instance, "notify", body=body)
    instance.approve(held.delivery_id, actor="raghav")

    assert requested and requested[0].urgency == "normal"


def test_the_default_notify_route_waits_for_approval(tmp_path):
    instance = default_ingress(
        path=tmp_path / "i.sqlite3",
        secret=SECRET,
        replay_store=WebhookReplayStore(tmp_path / "r.sqlite3"),
    )
    result = _post(instance, "notify", body=b'{"summary":"hello"}')
    assert result.decision == "held"


def test_a_route_must_be_registered_with_a_callable(tmp_path):
    instance = WebhookIngress(tmp_path / "i.sqlite3", secret=SECRET)
    with pytest.raises(WebhookIngressError, match="callable"):
        instance.register("bad", "not a function")  # type: ignore[arg-type]


# ------------------------------------ regressions from the Review phase


def test_run_now_retires_a_one_shot_job(scheduler):
    """Running it early by hand is still the one time he asked for it."""

    calls: list = []
    scheduler.register_action("ping", lambda p: calls.append(p) or ActionOutcome(True))
    record = _add(scheduler, one_shot=True, first_run_at=10_000)

    scheduler.run_now(record["schedule_id"], now=1_000)

    assert scheduler.require(record["schedule_id"])["state"] == "completed"
    # It must not also fire when its original time arrives.
    assert scheduler.tick(now=10_001) == []
    assert len(calls) == 1


def test_run_now_leaves_a_failed_one_shot_job_alive(scheduler):
    scheduler.register_action("ping", lambda _p: ActionOutcome(False, "not yet"))
    record = _add(scheduler, one_shot=True)

    scheduler.run_now(record["schedule_id"], now=1_000)

    assert scheduler.require(record["schedule_id"])["state"] == "active"


def test_run_now_does_not_retire_a_recurring_job(scheduler):
    scheduler.register_action("ping", lambda _p: ActionOutcome(True))
    record = _add(scheduler, interval_seconds=600)

    scheduler.run_now(record["schedule_id"], now=1_000)

    assert scheduler.require(record["schedule_id"])["state"] == "active"


def test_an_approved_delivery_replays_the_body_that_actually_arrived(tmp_path):
    """The handler must see the request Raghav approved, not an empty one."""

    seen: list = []
    instance = WebhookIngress(
        tmp_path / "i.sqlite3",
        secret=SECRET,
        replay_store=WebhookReplayStore(tmp_path / "r.sqlite3"),
    )
    instance.register(
        "deploy",
        lambda _p, request: seen.append(request) or RouteOutcome(True, "ran"),
        requires_approval=True,
    )
    body = b'{"ref":"refs/heads/main"}'
    held = _post(instance, "deploy", body=body, x_event_id="evt-42")

    instance.approve(held.delivery_id, actor="raghav")

    assert seen[0].body == body, "the original bytes must survive the hold"
    assert seen[0].headers.get("x_event_id") == "evt-42"


def test_an_approved_delivery_does_not_replay_its_signature(tmp_path):
    """The signature was consumed once; keeping it at rest buys nothing."""

    seen: list = []
    instance = WebhookIngress(
        tmp_path / "i.sqlite3",
        secret=SECRET,
        replay_store=WebhookReplayStore(tmp_path / "r.sqlite3"),
    )
    instance.register(
        "deploy",
        lambda _p, request: seen.append(request) or RouteOutcome(True, "ran"),
        requires_approval=True,
    )
    held = _post(instance, "deploy")
    instance.approve(held.delivery_id, actor="raghav")

    joined = " ".join(seen[0].headers).lower()
    assert "signature" not in joined


def test_a_resolved_delivery_stops_retaining_the_body(tmp_path):
    instance = WebhookIngress(
        tmp_path / "i.sqlite3",
        secret=SECRET,
        replay_store=WebhookReplayStore(tmp_path / "r.sqlite3"),
    )
    instance.register("deploy", lambda _p, _r: RouteOutcome(True), requires_approval=True)
    held = _post(instance, "deploy", body=b'{"secret":"hunter2"}')
    instance.approve(held.delivery_id, actor="raghav")

    row = instance.history()[0]
    assert row["decision"] == "accepted"
    assert not row["body_raw"], "an approved delivery no longer needs to be replayable"
    assert "hunter2" not in str(row["payload_json"])


# ---------------------------------------------------- the HTTP mount


@pytest.fixture
def webhook_client(tmp_path, monkeypatch):
    """A Flask test client wired to a temporary ingress."""

    from flask import Flask

    from ui import webhook_web

    instance = default_ingress(
        path=tmp_path / "i.sqlite3",
        secret=SECRET,
        replay_store=WebhookReplayStore(tmp_path / "r.sqlite3"),
    )
    instance.register("ping", lambda _p, _r: RouteOutcome(True, "pong"))
    webhook_web._reset_ingress_for_tests(instance)

    app = Flask(__name__)
    app.register_blueprint(webhook_web.webhook_bp)
    with app.test_client() as client:
        yield client, instance
    webhook_web._reset_ingress_for_tests(None)


def _signed_headers(body, *, now=None):
    """Sign against the real clock, since the HTTP path cannot inject a time."""

    moment = int(time.time() if now is None else now)
    return {key: value for key, value in sign(body, SECRET, timestamp=moment).headers().items()}


def test_the_ingress_is_actually_reachable_over_http(webhook_client):
    client, instance = webhook_client
    body = b'{"hello":"there"}'

    response = client.post("/webhooks/ping", data=body, headers=_signed_headers(body))

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert instance.history()[0]["decision"] == "accepted"


def test_an_unsigned_http_request_is_refused(webhook_client):
    client, _instance = webhook_client
    response = client.post("/webhooks/ping", data=b"{}")
    assert response.status_code == 401
    assert response.get_json()["ok"] is False


def test_an_http_refusal_does_not_hand_a_stranger_the_reason(webhook_client):
    client, instance = webhook_client
    response = client.post("/webhooks/definitely-not-a-route", data=b"{}")

    assert response.status_code == 404
    assert "unknown webhook route" not in str(response.get_json())
    # The reason is still recorded where Raghav can read it.
    assert "unknown webhook route" in instance.history()[0]["reason"]


def test_a_held_route_answers_202_over_http(webhook_client):
    client, _instance = webhook_client
    body = b'{"summary":"hello"}'
    response = client.post("/webhooks/notify", data=body, headers=_signed_headers(body))

    assert response.status_code == 202
    assert response.get_json()["status"] == "held for approval"


def test_a_held_delivery_can_be_approved_over_http(webhook_client):
    client, instance = webhook_client
    body = b'{"summary":"hello"}'
    client.post("/webhooks/notify", data=body, headers=_signed_headers(body))
    delivery_id = instance.pending()[0]["delivery_id"]

    response = client.post(
        f"/api/webhooks/{delivery_id}/approve", json={"actor": "raghav"}
    )

    assert response.status_code == 200
    assert instance.history(decision="accepted")


def test_approving_over_http_requires_a_named_actor(webhook_client):
    client, instance = webhook_client
    body = b'{"summary":"hello"}'
    client.post("/webhooks/notify", data=body, headers=_signed_headers(body))
    delivery_id = instance.pending()[0]["delivery_id"]

    response = client.post(f"/api/webhooks/{delivery_id}/approve", json={})

    assert response.status_code == 400
    assert instance.pending(), "it must still be waiting"


def test_approving_an_unknown_delivery_is_a_404(webhook_client):
    client, _instance = webhook_client
    response = client.post("/api/webhooks/nope/approve", json={"actor": "raghav"})
    assert response.status_code == 404


def test_the_management_endpoints_refuse_a_non_local_caller(webhook_client):
    """Posting a signed request is public; releasing a held one is not."""

    client, _instance = webhook_client
    for path in ("/api/webhooks/pending", "/api/webhooks/history"):
        response = client.get(path, environ_overrides={"REMOTE_ADDR": "203.0.113.9"})
        assert response.status_code == 403

    approve = client.post(
        "/api/webhooks/anything/approve",
        json={"actor": "raghav"},
        environ_overrides={"REMOTE_ADDR": "203.0.113.9"},
    )
    assert approve.status_code == 403


def test_a_caller_with_no_address_is_not_treated_as_local(webhook_client):
    client, _instance = webhook_client
    response = client.get("/api/webhooks/pending", environ_overrides={"REMOTE_ADDR": ""})
    assert response.status_code == 403
