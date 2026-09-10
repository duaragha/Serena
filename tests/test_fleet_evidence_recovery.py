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


def test_honest_blocker_is_visible_while_sibling_still_runs(tmp_path):
    from test_fleet_policy_store import _create

    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store)
    rid = run["run_id"]
    store.claim_run(rid)
    blocked, healthy = run["phases"][0]["legs"]
    first = store.begin_attempt(blocked["leg_id"])
    sibling = store.begin_attempt(healthy["leg_id"])
    reason = "work stopped before completion: authenticated readback is unavailable"
    store.finish_attempt(first["attempt_id"], state="failed", error=reason,
                         input_blocker_reason=reason)
    # Reconciliation must not turn a resumable blocker into runnable or dead work.
    store = FleetStore(store.path)
    assert blocked["leg_id"] not in store.prepare_phase_runnable(rid, 0)["runnable_leg_ids"]
    store.prepare_phase_runnable(rid, 1)
    current = store.get_run(rid)
    assert current["state"] == "running"
    assert current["phases"][0]["legs"][0]["state"] == "waiting_for_input"
    assert current["phases"][0]["legs"][0]["current_attempt"]["state"] == "failed"
    assert current["phases"][0]["legs"][1]["state"] == "running"
    assert current["phases"][0]["legs"][1]["current_attempt"]["attempt_id"] == sibling["attempt_id"]
    assert all(leg["state"] == "waiting_for_dependencies" for leg in current["phases"][1]["legs"])
    with store._connect() as db:
        assert db.execute("SELECT completed_at FROM fleet_work_unit_phases WHERE leg_id = ?",
                          (blocked["leg_id"],)).fetchone()[0] is None
    assert store.has_event(rid, "leg.waiting_for_input")
    assert not store.has_event(rid, "leg.completion_repair_requested")


def test_an_authority_stop_is_not_overridden_by_disk_words(tmp_path):
    store, run, leg, attempt = _attempt(tmp_path)
    reason = "work stopped before completion: [Errno 28], no authority to delete customer data"
    store.finish_attempt(attempt["attempt_id"], state="failed", error=reason,
                         input_blocker_reason=reason)
    current = store.get_run(run["run_id"])
    assert current["phases"][0]["legs"][0]["state"] == "waiting_for_input"
    assert current["resource_waits"] == []


def test_targeted_input_resume_preserves_siblings_resource_wait(tmp_path):
    from test_fleet_policy_store import _create

    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store)
    rid = run["run_id"]
    store.claim_run(rid)
    blocked, disk = run["phases"][0]["legs"]
    first = store.begin_attempt(blocked["leg_id"])
    second = store.begin_attempt(disk["leg_id"])
    reason = "work stopped before completion: evidence unavailable"
    store.finish_attempt(first["attempt_id"], state="failed", error=reason, input_blocker_reason=reason)
    store.finish_attempt(second["attempt_id"], state="failed", error="[Errno 28] No space left on device")
    parked = store.resolve_phase_failure(rid, "discover", reason)
    assert parked["state"] == "waiting_for_resources"
    original_wait = parked["resource_waits"]
    resumed = store.request_leg_retry(rid, blocked["leg_id"])
    assert resumed["state"] == "queued"
    assert resumed["phases"][0]["legs"][0]["state"] == "queued"
    assert resumed["phases"][0]["legs"][1]["state"] == "waiting_for_resources"
    assert resumed["resource_waits"] == original_wait
