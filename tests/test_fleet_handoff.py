"""Terminal delivery handoff: root-owed debt completes tracked, never parks."""

import json

from test_fleet_supervisor import _successful_fake, fleet_env  # noqa: F401

from core.commitments import CommitmentStore
from fleet import supervisor
from fleet.delivery import (
    HANDOFF_EVENT,
    HANDOFF_MARKER,
    HANDOFF_SOURCE,
    file_delivery_handoffs,
    handoff_source_ref,
)

# ruff: noqa: F811


def debt_run(fleet_env, owner="root", requirements=1):
    store = supervisor._store()
    run = supervisor.start_run("implement bounded behavior", activity="coding", cwd=str(fleet_env), worker_count=1)
    available = run["policy"]["work_units"][0]["completion_contract"]["delivery_requirements"]
    debts = [
        {"unit_id": "ws-1", "requirement": available[index], "owner": owner,
         "reason": "integration pending"}
        for index in range(requirements)
    ]
    store.append_event(run["run_id"], "leg.completion_evidence_accepted", {
        "units": [{"unit_id": "ws-1", "accepted": True, "deferred_delivery": debts}]},
        attempt_id="writer")
    return store, run, debts


def handoff_events(store, run_id):
    return [event for event in store.events(run_id, limit=2_000)
            if event["type"] == HANDOFF_EVENT]


def test_root_owed_terminal_debt_completes_with_handoff(fleet_env, monkeypatch):
    store, run, debts = debt_run(fleet_env)
    calls = []
    monkeypatch.setattr(supervisor, "run_worker", _successful_fake(calls))
    finished = supervisor.run_supervisor(run["run_id"])
    assert finished["state"] == "completed"
    assert finished["progress"] == {"completed": 4, "total": 4}
    assert len(calls) == 4

    filed = CommitmentStore().list(source=HANDOFF_SOURCE)
    assert len(filed) == 1
    assert filed[0].source_ref == handoff_source_ref(
        run["run_id"], "ws-1", debts[0]["requirement"])
    assert "ws-1" in filed[0].title
    assert run["run_id"] in filed[0].detail
    assert debts[0]["requirement"] in filed[0].detail

    result = supervisor.get_result(run["run_id"])
    assert HANDOFF_MARKER in result["result_text"]
    assert filed[0].commitment_id in result["result_text"]
    assert "ws-1" in result["result_text"]

    events = handoff_events(store, run["run_id"])
    assert len(events) == 1
    assert events[0]["payload"]["marker"] == HANDOFF_MARKER
    assert events[0]["payload"]["count"] == 1
    assert events[0]["payload"]["handoffs"][0]["commitment_id"] == filed[0].commitment_id
    assert not store.has_event(run["run_id"], "run.delivery_outstanding")


def test_handoff_filing_is_idempotent(fleet_env):
    store, run, debts = debt_run(fleet_env)
    first = file_delivery_handoffs(store, run["run_id"], debts)
    second = file_delivery_handoffs(store, run["run_id"], debts)
    assert len(first) == 1 == len(second)
    assert first[0]["commitment_id"] == second[0]["commitment_id"]
    assert len(CommitmentStore().list(source=HANDOFF_SOURCE)) == 1


def test_zero_debt_terminal_path_files_nothing(fleet_env, monkeypatch):
    store = supervisor._store()
    run = supervisor.start_run("implement bounded behavior", activity="coding", cwd=str(fleet_env), worker_count=1)
    monkeypatch.setattr(supervisor, "run_worker", _successful_fake([]))
    finished = supervisor.run_supervisor(run["run_id"])
    assert finished["state"] == "completed"
    assert CommitmentStore().list(source=HANDOFF_SOURCE) == []
    assert not store.has_event(run["run_id"], HANDOFF_EVENT)
    result = supervisor.get_result(run["run_id"])
    assert HANDOFF_MARKER not in (result["result_text"] or "")


def test_root_alias_terminal_debt_completes_with_handoff(fleet_env, monkeypatch):
    store, run, _debts = debt_run(fleet_env, owner="root coordinator")
    monkeypatch.setattr(supervisor, "run_worker", _successful_fake([]))
    finished = supervisor.run_supervisor(run["run_id"])
    assert finished["state"] == "completed"
    assert len(CommitmentStore().list(source=HANDOFF_SOURCE)) == 1
    assert store.has_event(run["run_id"], HANDOFF_EVENT)


def test_non_root_terminal_debt_still_parks(fleet_env, monkeypatch):
    store, run, _debts = debt_run(fleet_env, owner="codex:0")
    monkeypatch.setattr(supervisor, "run_worker", _successful_fake([]))
    parked = supervisor.run_supervisor(run["run_id"])
    assert parked["state"] == "waiting_for_input"
    assert CommitmentStore().list(source=HANDOFF_SOURCE) == []
    assert not store.has_event(run["run_id"], HANDOFF_EVENT)
    assert store.has_event(run["run_id"], "run.delivery_outstanding")


def test_mixed_root_and_real_owner_debt_parks_without_filing(fleet_env, monkeypatch):
    store = supervisor._store()
    run = supervisor.start_run("implement bounded behavior", activity="coding", cwd=str(fleet_env), worker_count=1)
    available = run["policy"]["work_units"][0]["completion_contract"]["delivery_requirements"]
    store.append_event(run["run_id"], "leg.completion_evidence_accepted", {
        "units": [{"unit_id": "ws-1", "accepted": True, "deferred_delivery": [
            {"unit_id": "ws-1", "requirement": available[0], "owner": "root",
             "reason": "daemon restart pending"},
            {"unit_id": "ws-1", "requirement": available[1], "owner": "codex:0",
             "reason": "teammate owns the deploy"},
        ]}]}, attempt_id="writer")
    monkeypatch.setattr(supervisor, "run_worker", _successful_fake([]))
    parked = supervisor.run_supervisor(run["run_id"])
    assert parked["state"] == "waiting_for_input"
    assert CommitmentStore().list(source=HANDOFF_SOURCE) == []
    assert not store.has_event(run["run_id"], HANDOFF_EVENT)


def test_commitments_failure_fails_open_to_parked(fleet_env, monkeypatch):
    store, run, _debts = debt_run(fleet_env)

    def broken(self, **kwargs):
        raise RuntimeError("commitments unavailable")

    monkeypatch.setattr(CommitmentStore, "propose", broken)
    monkeypatch.setattr(supervisor, "run_worker", _successful_fake([]))
    parked = supervisor.run_supervisor(run["run_id"])
    assert parked["state"] == "waiting_for_input"
    assert store.has_event(run["run_id"], "run.delivery_outstanding")
    assert not store.has_event(run["run_id"], HANDOFF_EVENT)


def test_evidence_between_evaluation_and_filing_narrows_the_handoff(fleet_env):
    from fleet.delivery import PREFIX, accept_operator_evidence

    store, run, debts = debt_run(fleet_env, requirements=2)
    store.wait_for_delivery(run["run_id"], "awaiting verification")
    accept_operator_evidence(store, run["run_id"], PREFIX + json.dumps([{
        "unit_id": "ws-1", "requirement": debts[0]["requirement"],
        "evidence": "Observed clean committed owned paths and passing independent tests.",
    }]))
    handoffs = file_delivery_handoffs(store, run["run_id"], debts)
    assert [item["requirement"] for item in handoffs] == [debts[1]["requirement"]]
    assert len(CommitmentStore().list(source=HANDOFF_SOURCE)) == 1


def test_handoff_report_actions_end_to_end(fleet_env, monkeypatch):
    from fleet import reports

    store, run, debts = debt_run(fleet_env)
    monkeypatch.setattr(supervisor, "run_worker", _successful_fake([]))
    assert supervisor.run_supervisor(run["run_id"])["state"] == "completed"
    # fleet_env disables the narrative provider, so this is the deterministic
    # report a dead provider still ships.
    report = reports.generate_report(run["run_id"], store=store)
    assert report["generator"].startswith("none (")
    assert report["actions"] is not None and len(report["actions"]) == 1
    [action] = report["actions"]
    filed = CommitmentStore().list(source=HANDOFF_SOURCE)[0]
    assert filed.commitment_id in action["action"]
    assert "ws-1" in action["action"]
    assert filed.commitment_id in action["reason"]
    assert set(action) == {"action", "reason"}


def test_handoff_report_leads_model_actions(tmp_path):
    import uuid

    from fleet import reports
    from fleet.policy import build_policy, builtin_config
    from fleet.store import FleetStore
    from fleet.workers import WorkerResult

    store = FleetStore(tmp_path / "fleet.sqlite3")
    policy = build_policy("coding", "hand off delivery", config=builtin_config(),
                          provider_mode="codex", worker_count=1).to_dict()
    run = store.create_run(task="hand off delivery", activity="coding", cwd=str(tmp_path),
                           origin_session_id=None, origin_agent="codex",
                           dry_run=False, policy=policy)
    commitment_id = uuid.uuid4().hex
    store.append_event(run["run_id"], HANDOFF_EVENT, {
        "marker": HANDOFF_MARKER, "run_id": run["run_id"], "count": 1,
        "handoffs": [{"run_id": run["run_id"], "unit_id": "ws-1",
                      "requirement": "the live surface is verified",
                      "reason": "sandbox cannot reach prod",
                      "owner": "root", "commitment_id": commitment_id,
                      "title": "[ws-1] the live surface is verified",
                      "source_ref": "fixture", "leg_id": None, "attempt_id": None}]})
    for phase in run["phases"]:
        for leg in phase["legs"]:
            attempt = store.begin_attempt(leg["leg_id"])
            store.finish_attempt(attempt["attempt_id"], state="completed",
                                 output_text="Fix evidence: done.")
    store.complete_run(run["run_id"], "done")

    def provider(request, **kwargs):
        return WorkerResult(True, json.dumps({
            "narrative": "Finished with one handoff", "next_prompt": "Verify prod",
            "actions": [{"action": "Add coverage", "reason": "Prevent regressions"}],
            "lesson_votes": []}), None, request.model, request.effort, 0)

    report = reports.generate_report(run["run_id"], store=store, runner=provider)
    assert [item["action"] for item in report["actions"]] == [
        report["actions"][0]["action"], "Add coverage"]
    assert commitment_id in report["actions"][0]["action"]
    assert "ws-1" in report["actions"][0]["action"]
    assert commitment_id in report["actions"][0]["reason"]


def test_prompt_prefers_not_applicable_and_honest_deferral():
    from fleet.completion import render_evidence_instructions

    units = [{"id": "ws-1"}]
    write_text = render_evidence_instructions(units, ["ws-1"], access_mode="write")
    assert "answer not_applicable with a reason" in write_text
    assert "That is the honest answer, not a deferral" in write_text
    assert "defer ONLY work a real owner will actually perform" in write_text
    read_text = render_evidence_instructions(units, ["ws-1"], access_mode="read")
    assert "answer not_applicable with a reason" not in read_text
    assert "defer ONLY work a real owner will actually perform" not in read_text
    assert "owed by write legs" in read_text
