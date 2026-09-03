from __future__ import annotations

import sqlite3

from core.fleet_policy import build_policy, builtin_config
from core.fleet_store import FleetStore


def _policy():
    # Four agents against three named workstreams, so Fleet appends the
    # synthetic integration unit that depends on the other three. That
    # dependency barrier is what these tests are about.
    return build_policy(
        "coding",
        "tasks:\n- parser\n- storage\n- renderer",
        config=builtin_config(),
        worker_count=4,
    ).to_dict()


def _run(store: FleetStore, tmp_path):
    return store.create_run(
        task="tasks:\n- parser\n- storage\n- renderer",
        activity="coding",
        cwd=str(tmp_path),
        origin_session_id=None,
        origin_agent="codex",
        dry_run=False,
        policy=_policy(),
    )


def _finish(store: FleetStore, leg: dict, state: str = "completed") -> None:
    attempt = store.begin_attempt(leg["leg_id"])
    store.finish_attempt(
        attempt["attempt_id"],
        state=state,
        output_text=f"evidence for {leg['worker_key']}",
        error="controlled failure" if state == "failed" else None,
        actual_model=leg["model"],
        actual_effort=leg["effort"],
        exit_code=0 if state == "completed" else 1,
    )


def test_dag_materializes_executor_phases_and_selects_only_runnable_units(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(store, tmp_path)

    units = {unit["id"]: unit for unit in run["work_units"]}
    assert set(units) == {"ws-1", "ws-2", "ws-3", "ws-4"}
    assert units["ws-4"]["dependency_ids"] == ["ws-1", "ws-2", "ws-3"]
    assert all(len(unit["phase_executions"]) == 4 for unit in units.values())

    selected = store.prepare_phase_runnable(run["run_id"], 0)
    assert selected is not None
    refreshed = store.get_run(run["run_id"])
    assert refreshed is not None
    first_phase = refreshed["phases"][0]
    integration_leg = next(
        leg for leg in first_phase["legs"] if leg["assignment_ids"] == ["ws-4"]
    )
    assert integration_leg["state"] == "waiting_for_dependencies"
    assert integration_leg["leg_id"] not in selected["runnable_leg_ids"]
    assert selected["waiting_unit_ids"] == ["ws-4"]

    for leg in first_phase["legs"]:
        if leg["leg_id"] != integration_leg["leg_id"]:
            _finish(store, leg)

    selected = store.prepare_phase_runnable(run["run_id"], 0)
    assert selected is not None
    assert selected["runnable_leg_ids"] == [integration_leg["leg_id"]]


def test_prepare_phase_repairs_completed_leg_with_stale_waiting_projection(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(store, tmp_path)
    run_id = str(run["run_id"])

    store.prepare_phase_runnable(run_id, 0)
    snapshot = store.get_run(run_id)
    assert snapshot is not None
    research = next(
        leg
        for leg in snapshot["phases"][0]["legs"]
        if leg["assignment_ids"] == ["ws-1"]
    )
    _finish(store, research)

    # Reproduce the durable inconsistency from run 60fd2548: the leg and its
    # successful attempt are completed, but a stale projection says the unit
    # is still waiting. A completed leg must win, otherwise there is no leg the
    # supervisor can dispatch and it reports "phase did not complete".
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE fleet_work_unit_phases "
            "SET state = 'waiting_for_dependencies', attempt_id = NULL, "
            "blocked_by_json = '[\"ws-1:prior\"]' "
            "WHERE run_id = ? AND unit_id = 'ws-1' AND phase_index = 0",
            (run_id,),
        )

    selected = store.prepare_phase_runnable(run_id, 0)
    assert selected is not None
    assert research["leg_id"] not in selected["runnable_leg_ids"]

    repaired = store.get_run(run_id)
    assert repaired is not None
    unit = next(item for item in repaired["work_units"] if item["id"] == "ws-1")
    phase = unit["phase_executions"][0]
    assert phase["state"] == "completed"
    assert phase["attempt_id"]
    assert phase["blocked_by"] == []


def test_rotated_review_waits_for_target_code_and_fix_waits_for_peer_review(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    policy = build_policy(
        "coding",
        "tasks:\n- auth guard\n- cart contract",
        config=builtin_config(),
        worker_count=2,
    ).to_dict()
    run = store.create_run(
        task="tasks:\n- auth guard\n- cart contract",
        activity="coding",
        cwd=str(tmp_path),
        origin_session_id=None,
        origin_agent="codex",
        dry_run=False,
        policy=policy,
    )

    store.prepare_phase_runnable(run["run_id"], 0)
    snapshot = store.get_run(run["run_id"])
    assert snapshot is not None
    for leg in snapshot["phases"][0]["legs"]:
        _finish(store, leg)

    store.prepare_phase_runnable(run["run_id"], 1)
    snapshot = store.get_run(run["run_id"])
    assert snapshot is not None
    code_ws1 = next(
        leg
        for leg in snapshot["phases"][1]["legs"]
        if leg["assignment_ids"] == ["ws-1"]
    )
    _finish(store, code_ws1)

    selected_review = store.prepare_phase_runnable(run["run_id"], 2)
    assert selected_review is not None
    snapshot = store.get_run(run["run_id"])
    assert snapshot is not None
    review_ws1 = next(
        leg
        for leg in snapshot["phases"][2]["legs"]
        if leg["review_target_ids"] == ["ws-1"]
    )
    review_ws2 = next(
        leg
        for leg in snapshot["phases"][2]["legs"]
        if leg["review_target_ids"] == ["ws-2"]
    )
    assert selected_review["runnable_leg_ids"] == [review_ws1["leg_id"]]
    assert review_ws2["state"] == "waiting_for_dependencies"

    ws1 = next(unit for unit in snapshot["work_units"] if unit["id"] == "ws-1")
    assert ws1["phase_executions"][2]["leg_id"] == review_ws1["leg_id"]

    _finish(store, review_ws1)
    selected_fix = store.prepare_phase_runnable(run["run_id"], 3)
    assert selected_fix is not None
    snapshot = store.get_run(run["run_id"])
    assert snapshot is not None
    fix_ws1 = next(
        leg
        for leg in snapshot["phases"][3]["legs"]
        if leg["assignment_ids"] == ["ws-1"]
    )
    assert selected_fix["runnable_leg_ids"] == [fix_ws1["leg_id"]]


def test_retry_repairs_legacy_review_map_and_reopens_stale_review_chain(tmp_path):
    store = FleetStore(tmp_path / "legacy-review.sqlite3")
    policy = build_policy(
        "coding",
        "tasks:\n- auth guard\n- cart contract",
        config=builtin_config(),
        worker_count=2,
    ).to_dict()
    run = store.create_run(
        task="tasks:\n- auth guard\n- cart contract",
        activity="coding",
        cwd=str(tmp_path),
        origin_session_id=None,
        origin_agent="codex",
        dry_run=False,
        policy=policy,
    )
    run_id = str(run["run_id"])

    # Recreate the old snapshot defect: Review advanced each reviewer's owned
    # unit instead of the rotated target unit.
    with sqlite3.connect(store.path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM fleet_work_unit_phases "
            "WHERE run_id = ? AND phase_index = 2 ORDER BY unit_id",
            (run_id,),
        ).fetchall()
        leg_by_unit = {
            unit_id: connection.execute(
                "SELECT leg_id FROM fleet_legs WHERE run_id = ? AND phase_index = 2 "
                "AND ordinal = ?",
                (run_id, ordinal),
            ).fetchone()["leg_id"]
            for ordinal, unit_id in enumerate(("ws-1", "ws-2"))
        }
        connection.execute(
            "DELETE FROM fleet_work_unit_phases WHERE run_id = ? AND phase_index = 2",
            (run_id,),
        )
        for row in rows:
            unit_id = str(row["unit_id"])
            connection.execute(
                """
                INSERT INTO fleet_work_unit_phases(
                    run_id, unit_id, phase_index, phase, leg_id, state,
                    blocked_by_json, updated_at
                ) VALUES (?, ?, 2, 'verify', ?, ?, ?, ?)
                """,
                (
                    run_id,
                    unit_id,
                    leg_by_unit[unit_id],
                    str(row["state"]),
                    str(row["blocked_by_json"]),
                    float(row["updated_at"]),
                ),
            )

    store.prepare_phase_runnable(run_id, 0)
    snapshot = store.get_run(run_id)
    assert snapshot is not None
    for leg in snapshot["phases"][0]["legs"]:
        _finish(store, leg)

    store.prepare_phase_runnable(run_id, 1)
    snapshot = store.get_run(run_id)
    assert snapshot is not None
    code_ws1 = next(
        leg for leg in snapshot["phases"][1]["legs"] if leg["assignment_ids"] == ["ws-1"]
    )
    _finish(store, code_ws1)

    selected_review = store.prepare_phase_runnable(run_id, 2)
    assert selected_review is not None
    snapshot = store.get_run(run_id)
    assert snapshot is not None
    stale_review = next(
        leg for leg in snapshot["phases"][2]["legs"] if leg["worker_key"] == "agent:a"
    )
    assert selected_review["runnable_leg_ids"] == [stale_review["leg_id"]]
    _finish(store, stale_review)

    selected_fix = store.prepare_phase_runnable(run_id, 3)
    assert selected_fix is not None
    snapshot = store.get_run(run_id)
    assert snapshot is not None
    wrong_fix = next(
        leg for leg in snapshot["phases"][3]["legs"] if leg["assignment_ids"] == ["ws-1"]
    )
    assert selected_fix["runnable_leg_ids"] == [wrong_fix["leg_id"]]
    _finish(store, wrong_fix)

    store.fail_run(run_id, "legacy review ordering detected")
    retried = store.retry_run(run_id)

    units = {unit["id"]: unit for unit in retried["work_units"]}
    review_ws1 = units["ws-1"]["phase_executions"][2]
    review_ws2 = units["ws-2"]["phase_executions"][2]
    review_leg_a = next(
        leg for leg in retried["phases"][2]["legs"] if leg["worker_key"] == "agent:a"
    )
    review_leg_b = next(
        leg for leg in retried["phases"][2]["legs"] if leg["worker_key"] == "agent:b"
    )
    assert review_ws1["leg_id"] == review_leg_b["leg_id"]
    assert review_ws2["leg_id"] == review_leg_a["leg_id"]
    assert all(leg["state"] != "completed" for leg in retried["phases"][2]["legs"])
    assert all(leg["state"] != "completed" for leg in retried["phases"][3]["legs"])

    retry_event = store.events(run_id)[-1]
    assert retry_event["payload"]["legacy_review_legs_remapped"] == sorted(
        [review_leg_a["leg_id"], review_leg_b["leg_id"]]
    )
    assert retry_event["payload"]["stale_review_legs_reopened"] == [
        review_leg_a["leg_id"]
    ]


def test_dependency_failure_propagates_and_blocks_descendant_executor(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(store, tmp_path)
    selected = store.prepare_phase_runnable(run["run_id"], 0)
    assert selected is not None
    snapshot = store.get_run(run["run_id"])
    assert snapshot is not None
    failed_leg = next(
        leg for leg in snapshot["phases"][0]["legs"] if leg["assignment_ids"] == ["ws-1"]
    )
    _finish(store, failed_leg, state="failed")

    snapshot = store.get_run(run["run_id"])
    assert snapshot is not None
    units = {unit["id"]: unit for unit in snapshot["work_units"]}
    assert units["ws-1"]["state"] == "failed"
    assert units["ws-4"]["state"] == "blocked_dependency_failed"
    assert units["ws-4"]["blocked_dependency_ids"] == ["ws-1"]
    integration_leg = next(
        leg
        for leg in snapshot["phases"][0]["legs"]
        if leg["assignment_ids"] == ["ws-4"]
    )
    assert integration_leg["state"] == "failed"


def test_retried_dependency_wakes_descendant_after_dependency_recovers(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(store, tmp_path)
    store.prepare_phase_runnable(run["run_id"], 0)
    snapshot = store.get_run(run["run_id"])
    assert snapshot is not None
    first_phase = snapshot["phases"][0]
    failed_leg = next(
        leg for leg in first_phase["legs"] if leg["assignment_ids"] == ["ws-1"]
    )
    integration_leg = next(
        leg for leg in first_phase["legs"] if leg["assignment_ids"] == ["ws-4"]
    )

    _finish(store, failed_leg, state="failed")
    for leg in first_phase["legs"]:
        if leg["assignment_ids"] in (["ws-2"], ["ws-3"]):
            _finish(store, leg)

    store.fail_run(run["run_id"], "controlled dependency failure")
    store.retry_run(run["run_id"])
    selected = store.prepare_phase_runnable(run["run_id"], 0)
    assert selected is not None
    assert failed_leg["leg_id"] in selected["runnable_leg_ids"]
    assert integration_leg["leg_id"] not in selected["runnable_leg_ids"]

    retried = store.get_run(run["run_id"])
    assert retried is not None
    retried_failed_leg = next(
        leg
        for leg in retried["phases"][0]["legs"]
        if leg["assignment_ids"] == ["ws-1"]
    )
    _finish(store, retried_failed_leg)

    selected = store.prepare_phase_runnable(run["run_id"], 0)
    assert selected is not None
    assert selected["runnable_leg_ids"] == [integration_leg["leg_id"]]
    recovered = store.get_run(run["run_id"])
    assert recovered is not None
    recovered_unit = next(
        unit for unit in recovered["work_units"] if unit["id"] == "ws-4"
    )
    assert recovered_unit["state"] == "queued"
    assert recovered_unit["failure_reason"] is None


def test_retry_reopens_legacy_stopped_research_without_replaying_completed_code(tmp_path):
    store = FleetStore(tmp_path / "legacy-stop.sqlite3")
    policy = build_policy(
        "coding",
        "repair one bounded issue",
        config=builtin_config(),
        worker_count=1,
    ).to_dict()
    run = store.create_run(
        task="repair one bounded issue",
        activity="coding",
        cwd=str(tmp_path),
        origin_session_id=None,
        origin_agent="codex",
        dry_run=False,
        policy=policy,
    )
    run_id = str(run["run_id"])

    store.prepare_phase_runnable(run_id, 0)
    snapshot = store.get_run(run_id)
    assert snapshot is not None
    research = snapshot["phases"][0]["legs"][0]
    attempt = store.begin_attempt(research["leg_id"])
    store.append_event(
        run_id,
        "leg.completion_evidence_accepted",
        {
            "accepted": True,
            "reason": "legacy gate accepted a stop",
            "units": [
                {
                    "unit_id": "ws-1",
                    "accepted": True,
                    "claimed_status": "stopped",
                    "stop_condition": "read-only boundary",
                }
            ],
        },
        leg_id=research["leg_id"],
        attempt_id=attempt["attempt_id"],
    )
    store.finish_attempt(attempt["attempt_id"], state="completed", exit_code=0)

    store.prepare_phase_runnable(run_id, 1)
    snapshot = store.get_run(run_id)
    assert snapshot is not None
    code = snapshot["phases"][1]["legs"][0]
    _finish(store, code)
    store.fail_run(run_id, "legacy supervisor failed after dispatching downstream")

    retried = store.retry_run(run_id)
    assert retried["state"] == "queued"
    research_after = retried["phases"][0]["legs"][0]
    code_after = retried["phases"][1]["legs"][0]
    assert research_after["state"] == "queued"
    assert code_after["state"] == "completed"
    retry_event = store.events(run_id)[-1]
    assert retry_event["type"] == "run.retried"
    assert retry_event["payload"]["legacy_stopped_legs_reopened"] == [
        research["leg_id"]
    ]


def test_dependency_failure_blocks_every_unit_sharing_the_same_provider_turn(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    policy = build_policy(
        "coding",
        "tasks:\n- one\n- two\n- three\n- four\n- five",
        config=builtin_config(),
    ).to_dict()
    shared = policy["phases"][0]["workers"][0]["assignment_ids"]
    assert shared == ["ws-1", "ws-5"]
    next(unit for unit in policy["work_units"] if unit["id"] == "ws-5")[
        "dependency_ids"
    ] = ["ws-2"]
    run = store.create_run(
        task="tasks:\n- one\n- two\n- three\n- four\n- five",
        activity="coding",
        cwd=str(tmp_path),
        origin_session_id=None,
        origin_agent="codex",
        dry_run=False,
        policy=policy,
    )
    store.prepare_phase_runnable(run["run_id"], 0)
    snapshot = store.get_run(run["run_id"])
    assert snapshot is not None
    dependency_leg = next(
        leg for leg in snapshot["phases"][0]["legs"] if leg["assignment_ids"] == ["ws-2"]
    )
    _finish(store, dependency_leg, state="failed")
    store.prepare_phase_runnable(run["run_id"], 0)

    snapshot = store.get_run(run["run_id"])
    assert snapshot is not None
    units = {unit["id"]: unit for unit in snapshot["work_units"]}
    assert units["ws-5"]["state"] == "blocked_dependency_failed"
    assert units["ws-1"]["state"] == "blocked_dependency_failed"
    assert "co-owned executor blocked" in units["ws-1"]["failure_reason"]


def test_dag_migration_backfills_preexisting_policy_without_losing_leg_state(tmp_path):
    path = tmp_path / "fleet.sqlite3"
    store = FleetStore(path)
    run = _run(store, tmp_path)
    first_leg = run["phases"][0]["legs"][0]
    _finish(store, first_leg)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM fleet_work_units WHERE run_id = ?", (run["run_id"],))

    reopened = FleetStore(path)
    snapshot = reopened.get_run(run["run_id"])
    assert snapshot is not None
    unit = next(
        item for item in snapshot["work_units"] if item["id"] in first_leg["assignment_ids"]
    )
    assert unit["phase_executions"][0]["state"] == "completed"
    assert unit["progress"]["completed"] == 1


def test_dag_projects_machine_accepted_evidence_instead_of_only_worker_prose(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(store, tmp_path)
    leg = run["phases"][0]["legs"][0]
    unit_id = leg["assignment_ids"][0]
    attempt = store.begin_attempt(leg["leg_id"])
    store.append_event(
        run["run_id"],
        "leg.completion_evidence_accepted",
        {
            "accepted": True,
            "reason": "completion evidence satisfied the work-unit contract",
            "failures": [],
            "units": [
                {
                    "unit_id": unit_id,
                    "accepted": True,
                    "changed_paths": ["core/parser.py"],
                }
            ],
        },
        leg_id=leg["leg_id"],
        attempt_id=attempt["attempt_id"],
    )
    store.finish_attempt(
        attempt["attempt_id"],
        state="completed",
        output_text="worker prose that is not itself the verdict",
        exit_code=0,
    )

    snapshot = store.get_run(run["run_id"])
    unit = next(item for item in snapshot["work_units"] if item["id"] == unit_id)
    receipt = unit["evidence_receipts"][0]

    assert receipt["accepted"] is True
    assert receipt["reason"] == "completion evidence satisfied the work-unit contract"
    assert receipt["units"][0]["changed_paths"] == ["core/parser.py"]
