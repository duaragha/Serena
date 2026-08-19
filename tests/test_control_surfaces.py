"""Tests for the shared control plane beyond Fleet.

Two things must hold. The obligation ledger has to track every surface that
owes Raghav something, not just Fleet. And a crash must never turn an
undelivered promise into a silent success.
"""

from __future__ import annotations

import sqlite3

import pytest

from core.control_plane import (
    OBLIGATION_RULES,
    ControlPlaneStore,
    SurfaceOutbox,
)
from core.control_recovery import (
    outstanding_summary,
    reconcile_obligations,
)


def _store(tmp_path) -> ControlPlaneStore:
    return ControlPlaneStore(tmp_path / "control.sqlite3")


# ---- rule table -----------------------------------------------------------


def test_every_declared_surface_rule_is_well_formed():
    for rule in OBLIGATION_RULES:
        assert rule.open_on
        assert rule.fulfill_on, f"{rule.kind} must be closable"
        # An event may not both open and close the same obligation.
        assert rule.open_on not in rule.fulfill_on
        assert rule.open_on not in rule.fail_on
        assert rule.open_on not in rule.cancel_on
        assert not set(rule.fulfill_on) & set(rule.fail_on)


def test_fleet_final_delivery_semantics_are_unchanged(tmp_path):
    store = _store(tmp_path)
    store.append_event(
        surface="fleet",
        event_type="run.created",
        lifecycle_state="created",
        job_id="run-1",
        payload={"summary": "ship the thing"},
    )

    obligations = store.obligations(surface="fleet")

    assert len(obligations) == 1
    assert obligations[0].obligation_id == "fleet:run-1:final-delivery"
    assert obligations[0].kind == "final_result_delivery"
    assert obligations[0].summary == "ship the thing"
    assert obligations[0].state == "open"


def test_a_fleet_dry_run_owes_nobody_anything(tmp_path):
    store = _store(tmp_path)
    store.append_event(
        surface="fleet",
        event_type="run.created",
        lifecycle_state="created",
        job_id="run-dry",
        payload={"dry_run": True, "summary": "rehearsal"},
    )

    assert store.obligations(surface="fleet") == []


@pytest.mark.parametrize(
    ("surface", "open_event", "close_event", "job"),
    [
        ("voice", "job.accepted", "job.result.delivered", "voice-1"),
        ("memory", "proposal.created", "proposal.approved", "memory-1"),
        ("notification", "notice.queued", "notice.delivered", "notice-1"),
        ("tool", "call.started", "call.completed", "tool-1"),
        ("chat", "turn.started", "turn.completed", "chat-1"),
    ],
)
def test_each_migrated_surface_opens_and_closes_its_obligation(
    tmp_path, surface, open_event, close_event, job
):
    store = _store(tmp_path)

    store.append_event(
        surface=surface, event_type=open_event, lifecycle_state="started", job_id=job
    )
    assert [item.state for item in store.obligations(surface=surface)] == ["open"]

    store.append_event(
        surface=surface, event_type=close_event, lifecycle_state="completed", job_id=job
    )
    assert [item.state for item in store.obligations(surface=surface)] == ["fulfilled"]


def test_a_failed_delivery_keeps_the_obligation_open_with_evidence(tmp_path):
    store = _store(tmp_path)
    store.append_event(
        surface="voice", event_type="job.accepted", lifecycle_state="accepted", job_id="voice-1"
    )

    for index in range(3):
        store.append_event(
            surface="voice",
            event_type="job.result.failed",
            lifecycle_state="failed",
            job_id="voice-1",
            event_id=f"voice-fail-{index}",
            payload={"error": "the voice bridge was down"},
        )

    obligation = store.obligations(surface="voice")[0]
    assert obligation.state == "open"
    assert obligation.attempts == 3
    assert obligation.last_error == "the voice bridge was down"


def test_cancelling_closes_without_claiming_delivery(tmp_path):
    store = _store(tmp_path)
    store.append_event(
        surface="voice", event_type="job.accepted", lifecycle_state="accepted", job_id="voice-1"
    )
    store.append_event(
        surface="voice", event_type="job.cancelled", lifecycle_state="cancelled", job_id="voice-1"
    )

    obligation = store.obligations(surface="voice")[0]
    assert obligation.state == "cancelled"
    assert obligation.fulfillment_event_id is None


def test_surfaces_do_not_close_each_others_obligations(tmp_path):
    store = _store(tmp_path)
    store.append_event(
        surface="voice", event_type="job.accepted", lifecycle_state="accepted", job_id="shared-id"
    )

    # Same job id, different surface. This must not resolve the voice promise.
    store.append_event(
        surface="tool", event_type="call.completed", lifecycle_state="completed", job_id="shared-id"
    )

    assert store.obligations(surface="voice")[0].state == "open"


def test_replaying_a_delivery_event_is_idempotent(tmp_path):
    store = _store(tmp_path)
    store.append_event(
        surface="voice", event_type="job.accepted", lifecycle_state="accepted", job_id="voice-1"
    )
    for _ in range(3):
        store.append_event(
            surface="voice",
            event_type="job.result.failed",
            lifecycle_state="failed",
            job_id="voice-1",
            event_id="one-and-only-failure",
        )

    assert store.obligations(surface="voice")[0].attempts == 1


# ---- transactional outbox -------------------------------------------------


def test_outbox_publishes_idempotently(tmp_path):
    control = _store(tmp_path)
    outbox = SurfaceOutbox("voice", tmp_path / "voice.sqlite3")
    with outbox.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        outbox.stage_event(
            connection,
            event_type="job.accepted",
            lifecycle_state="accepted",
            job_id="voice-1",
            event_id="voice-evt-1",
        )

    assert outbox.pending() == 1
    assert outbox.flush(control) == 1
    assert outbox.pending() == 0
    assert outbox.flush(control) == 0
    assert len(control.events(surface="voice")) == 1


def test_outbox_survives_a_crash_between_commit_and_publish(tmp_path):
    """The surface committed its own state, then the process died."""

    control = _store(tmp_path)
    outbox = SurfaceOutbox("voice", tmp_path / "voice.sqlite3")
    with outbox.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO jobs(id) VALUES ('voice-1')")
        outbox.stage_event(
            connection,
            event_type="job.accepted",
            lifecycle_state="accepted",
            job_id="voice-1",
            event_id="voice-evt-1",
        )
    # No flush happened before the "crash".

    reopened = SurfaceOutbox("voice", tmp_path / "voice.sqlite3")
    assert reopened.pending() == 1
    assert reopened.flush(control) == 1
    assert control.obligations(surface="voice")[0].state == "open"


def test_a_failing_control_plane_preserves_the_staged_event(tmp_path):
    outbox = SurfaceOutbox("voice", tmp_path / "voice.sqlite3")
    with outbox.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        outbox.stage_event(
            connection,
            event_type="job.accepted",
            lifecycle_state="accepted",
            job_id="voice-1",
            event_id="voice-evt-1",
        )

    class Broken:
        def append_envelope(self, _envelope):
            raise RuntimeError("control plane is unavailable")

    assert outbox.flush(Broken()) == 0
    assert outbox.pending() == 1


def test_outbox_rollback_discards_the_event_with_the_state(tmp_path):
    """If the surface's own transaction rolls back, so does the envelope."""

    outbox = SurfaceOutbox("voice", tmp_path / "voice.sqlite3")
    connection = outbox.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        outbox.stage_event(
            connection,
            event_type="job.accepted",
            lifecycle_state="accepted",
            job_id="voice-1",
            event_id="voice-evt-1",
        )
        connection.rollback()
    finally:
        connection.close()

    assert outbox.pending() == 0


def test_outbox_rejects_an_unknown_surface(tmp_path):
    with pytest.raises(ValueError):
        SurfaceOutbox("telepathy", tmp_path / "nope.sqlite3")


# ---- crash recovery -------------------------------------------------------


def test_recovery_redelivers_a_stale_open_obligation(tmp_path):
    store = _store(tmp_path)
    store.append_event(
        surface="voice",
        event_type="job.accepted",
        lifecycle_state="accepted",
        job_id="voice-1",
        occurred_at=1_000.0,
    )
    seen = []

    report = reconcile_obligations(
        store,
        handlers={"voice": lambda obligation: seen.append(obligation.obligation_id) or True},
        now=store.obligations()[0].created_at + 10_000,
    )

    assert seen == ["voice:voice-1:result-delivery"]
    assert [item.action for item in report.recovered] == ["redelivered"]
    # Redelivery does not get to declare success. Only a real delivery event does.
    assert store.obligations(surface="voice")[0].state == "open"


def test_recovery_leaves_a_fresh_obligation_to_its_live_owner(tmp_path):
    store = _store(tmp_path)
    store.append_event(
        surface="voice", event_type="job.accepted", lifecycle_state="accepted", job_id="voice-1"
    )
    seen = []

    report = reconcile_obligations(
        store, handlers={"voice": lambda obligation: seen.append(obligation) or True}
    )

    assert seen == []
    assert report.total == 0


def test_recovery_never_touches_a_resolved_obligation(tmp_path):
    store = _store(tmp_path)
    store.append_event(
        surface="voice",
        event_type="job.accepted",
        lifecycle_state="accepted",
        job_id="voice-1",
        occurred_at=1_000.0,
    )
    store.append_event(
        surface="voice",
        event_type="job.result.delivered",
        lifecycle_state="delivered",
        job_id="voice-1",
    )

    report = reconcile_obligations(
        store, handlers={"voice": lambda _o: True}, now=1e10
    )

    assert report.total == 0


def test_exhausted_attempts_become_ambiguous_never_fulfilled(tmp_path):
    store = _store(tmp_path)
    store.append_event(
        surface="voice",
        event_type="job.accepted",
        lifecycle_state="accepted",
        job_id="voice-1",
        occurred_at=1_000.0,
    )
    for index in range(5):
        store.append_event(
            surface="voice",
            event_type="job.result.failed",
            lifecycle_state="failed",
            job_id="voice-1",
            event_id=f"fail-{index}",
        )

    report = reconcile_obligations(
        store, handlers={"voice": lambda _o: True}, max_attempts=5, now=1e10
    )

    obligation = store.obligations(surface="voice")[0]
    assert obligation.state == "ambiguous"
    assert obligation.state != "fulfilled"
    assert [item.action for item in report.abandoned] == ["abandoned"]


def test_a_surface_without_a_handler_is_reported_not_dropped(tmp_path):
    store = _store(tmp_path)
    store.append_event(
        surface="memory",
        event_type="proposal.created",
        lifecycle_state="created",
        job_id="memory-1",
        occurred_at=1_000.0,
    )

    report = reconcile_obligations(store, handlers={}, now=1e10)

    assert [item.action for item in report.skipped] == ["skipped"]
    assert store.obligations(surface="memory")[0].state == "open"


def test_a_broken_handler_cannot_stall_the_sweep(tmp_path):
    store = _store(tmp_path)
    for job in ("a", "b"):
        store.append_event(
            surface="voice",
            event_type="job.accepted",
            lifecycle_state="accepted",
            job_id=f"voice-{job}",
            occurred_at=1_000.0,
        )
    calls = []

    def handler(obligation):
        calls.append(obligation.job_id)
        if obligation.job_id == "voice-a":
            raise RuntimeError("this handler is broken")
        return True

    report = reconcile_obligations(store, handlers={"voice": handler}, now=1e10)

    assert sorted(calls) == ["voice-a", "voice-b"]
    assert len(report.recovered) == 1
    assert len(report.skipped) == 1


def test_outstanding_summary_reports_what_serena_owes(tmp_path):
    store = _store(tmp_path)
    store.append_event(
        surface="voice", event_type="job.accepted", lifecycle_state="accepted", job_id="voice-1"
    )
    store.append_event(
        surface="fleet",
        event_type="run.created",
        lifecycle_state="created",
        job_id="run-1",
        payload={"summary": "ship it"},
    )

    summary = outstanding_summary(store)

    assert summary["open"] == 2
    assert summary["by_surface"] == {"fleet": 1, "voice": 1}
    assert {item["surface"] for item in summary["items"]} == {"fleet", "voice"}
