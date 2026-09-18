import json

import pytest
from test_fleet_supervisor import _successful_fake, fleet_env  # noqa: F401

from fleet import supervisor
from fleet.delivery import PREFIX, accept_operator_evidence

# ruff: noqa: F811


def debt_run(fleet_env, owner="root"):
    store = supervisor._store()
    run = supervisor.start_run("implement bounded behavior", activity="coding", cwd=str(fleet_env), worker_count=1)
    requirement = run["policy"]["work_units"][0]["completion_contract"]["delivery_requirements"][0]
    debt = {"unit_id": "ws-1", "requirement": requirement, "owner": owner, "reason": "integration pending"}
    store.append_event(run["run_id"], "leg.completion_evidence_accepted", {
        "units": [{"unit_id": "ws-1", "accepted": True, "deferred_delivery": [debt]}]}, attempt_id="writer")
    return store, run, debt


@pytest.mark.parametrize("ok,applied,attempt,remaining", [(True, True, "writer", 0),
    (False, True, "writer", 1), (True, False, "writer", 1), (True, True, "peer", 1)])
def test_integration_receipts_are_scoped_and_require_success(fleet_env, ok, applied, attempt, remaining):
    store, run, _ = debt_run(fleet_env)
    store.append_event(run["run_id"], "worker.integration.accepted", {"ok": ok, "applied": applied}, attempt_id=attempt)
    assert len(supervisor._outstanding_delivery(store, run["run_id"])) == remaining


def test_all_steps_complete_parks_delivery_then_receipt_retry_runs_no_workers(fleet_env, monkeypatch):
    # Debt owed by a real Fleet owner still parks: only root-owed remainder
    # completes with a handoff (see tests/test_fleet_handoff.py).
    store, run, debt = debt_run(fleet_env, owner="codex:0")
    calls = []
    monkeypatch.setattr(supervisor, "run_worker", _successful_fake(calls))
    parked = supervisor.run_supervisor(run["run_id"])
    assert parked["state"] == "waiting_for_input"
    assert parked["progress"] == {"completed": 4, "total": 4}
    assert len(calls) == 4
    entries = [{"unit_id": "ws-1", "requirement": debt["requirement"],
                "evidence": "Verified base integration at the recorded Git revision with clean owned paths and passing tests."}]
    # Simulate a still-open older MCP connection that only understands steering.
    store.add_steering(run["run_id"], PREFIX + json.dumps(entries))
    supervisor.retry_run(run["run_id"])
    assert supervisor.run_supervisor(run["run_id"])["state"] == "completed"
    assert len(calls) == 4


def test_repeated_operator_proof_cannot_clear_a_newer_deferral(fleet_env):
    store, run, debt = debt_run(fleet_env)
    store.wait_for_delivery(run["run_id"], "awaiting delivery")
    message = PREFIX + json.dumps([{**debt, "evidence": "Observed clean committed owned paths and passing independent tests."}])
    accept_operator_evidence(store, run["run_id"], message)
    store.append_event(run["run_id"], "leg.completion_evidence_accepted", {
        "units": [{"unit_id": "ws-1", "accepted": True, "deferred_delivery": [debt]}]})
    accept_operator_evidence(store, run["run_id"], message)
    assert len(supervisor._outstanding_delivery(store, run["run_id"])) == 1


def test_operator_receipt_refuses_unknown_requirements_and_cancelled_runs(fleet_env):
    store, run, debt = debt_run(fleet_env)
    store.wait_for_delivery(run["run_id"], "awaiting verification")
    entry = {"unit_id": "ws-1", "requirement": "invented", "evidence": "observed proof " * 10}
    with pytest.raises(ValueError, match="exact frozen"):
        accept_operator_evidence(store, run["run_id"], PREFIX + json.dumps([entry]))
    entry["requirement"] = debt["requirement"]
    store.request_cancel(run["run_id"])
    with pytest.raises(ValueError, match="uncancelled"):
        accept_operator_evidence(store, run["run_id"], PREFIX + json.dumps([entry]))


def test_integration_cannot_discharge_live_deployment_or_a_later_change(fleet_env):
    store, run, debt = debt_run(fleet_env)
    live = run["policy"]["work_units"][0]["completion_contract"]["delivery_requirements"][1]
    store.append_event(run["run_id"], "leg.completion_evidence_accepted", {
        "units": [{"unit_id": "ws-1", "accepted": True,
                   "deferred_delivery": [{**debt, "requirement": live}]}]}, attempt_id="writer")
    store.append_event(run["run_id"], "worker.integration.accepted", {"ok": True, "applied": True}, attempt_id="writer")
    assert [item["requirement"] for item in supervisor._outstanding_delivery(store, run["run_id"])] == [live]
    store.append_event(run["run_id"], "leg.completion_evidence_accepted", {
        "units": [{"unit_id": "ws-1", "accepted": True, "deferred_delivery": [debt]}]}, attempt_id="new-fix")
    assert len(supervisor._outstanding_delivery(store, run["run_id"])) == 2
