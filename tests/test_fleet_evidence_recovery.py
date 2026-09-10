"""Completion failure and bounded correction are one durable transition."""

import pytest
from test_fleet_supervision import _run

from fleet.store import FleetStore


def _attempt(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(store)
    store.claim_run(run["run_id"])
    leg = run["phases"][0]["legs"][0]
    return store, run, leg, store.begin_attempt(leg["leg_id"])


def _reject(store, attempt):
    store.finish_attempt(attempt["attempt_id"], state="failed", session_id="same-native-session",
                         error="completion evidence rejected: missing envelope",
                         completion_repair_reason="missing envelope")


def test_repair_receipt_and_failed_attempt_rollback_together(tmp_path, monkeypatch):
    store, run, leg, attempt = _attempt(tmp_path)
    original = store._insert_event

    def interrupt(connection, **kwargs):
        if kwargs["event_type"] == "leg.completion_repair_requested":
            raise RuntimeError("simulated crash before repair receipt commit")
        return original(connection, **kwargs)

    monkeypatch.setattr(store, "_insert_event", interrupt)
    with pytest.raises(RuntimeError, match="simulated crash"):
        _reject(store, attempt)
    restarted = FleetStore(store.path)
    current = restarted.get_run(run["run_id"])["phases"][0]["legs"][0]
    assert current["state"] == "running"
    assert current["current_attempt"]["state"] == "running"
    assert not restarted.has_event(run["run_id"], "leg.completion_repair_requested")
    _reject(restarted, attempt)
    current = restarted.get_run(run["run_id"])["phases"][0]["legs"][0]
    assert current["state"] == "queued"
    assert current["current_attempt"]["state"] == "failed"
    assert restarted.has_event(run["run_id"], "leg.completion_repair_requested")


def test_evidence_budget_survives_restart_and_duplicate_callback(tmp_path):
    store, run, leg, first = _attempt(tmp_path)
    _reject(store, first)
    _reject(store, first)
    store = FleetStore(store.path)
    second = store.begin_attempt(leg["leg_id"])
    assert second["resume_session_id"] == "same-native-session"
    _reject(store, second)
    parked = store.resolve_phase_failure(run["run_id"], "discover", "completion evidence rejected")
    assert parked["state"] == "waiting_for_input"
    assert parked["completed_at"] is None
    current = parked["phases"][0]["legs"][0]
    assert current["state"] == "waiting_for_input"
    assert current["current_attempt"]["state"] == "failed"
    assert current["attempt_count"] == 2
    assert store.next_queued_run() is None
    events = store.events(run["run_id"], limit=100)
    assert sum(e["type"] == "leg.completion_repair_requested" for e in events) == 1
    assert sum(e["type"] == "leg.completion_repair_exhausted" for e in events) == 1
    assert store.request_leg_retry(run["run_id"], leg["leg_id"])["state"] == "queued"


def test_cancellation_never_schedules_evidence_repair(tmp_path):
    store, run, leg, attempt = _attempt(tmp_path)
    store.request_cancel(run["run_id"])
    _reject(store, attempt)
    cancelled = store.resolve_phase_failure(run["run_id"], "discover", "cancelled")
    assert cancelled["state"] == "cancelled"
    assert not store.has_event(run["run_id"], "leg.completion_repair_requested")


def test_disk_readiness_precedes_evidence_repair(tmp_path):
    store, run, leg, attempt = _attempt(tmp_path)
    store.finish_attempt(attempt["attempt_id"], state="failed", error="[Errno 28] No space left on device",
                         completion_repair_reason="evidence infrastructure unavailable")
    current = store.get_run(run["run_id"])["phases"][0]["legs"][0]
    assert current["state"] == "waiting_for_resources"
    assert not store.has_event(run["run_id"], "leg.completion_repair_requested")
