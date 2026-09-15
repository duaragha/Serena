import json
from types import SimpleNamespace

import pytest

from fleet.collaboration import PeerStore
from fleet.dependencies import declare_dependency, declared_wait
from fleet.policy import build_policy, builtin_config
from fleet.store import FleetStore


@pytest.fixture
def dependency_team(tmp_path):
    task = "tasks:\n- scheduler\n- webhook\n- queue"
    policy = build_policy("coding", task, config=builtin_config(), worker_count=3).to_dict()
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = store.create_run(task=task, activity="coding", cwd=str(tmp_path),
                           origin_session_id=None, origin_agent=None, dry_run=False, policy=policy)
    peers = PeerStore(store)
    legs = run["phases"][0]["legs"]
    attempts = [store.begin_attempt(leg["leg_id"]) for leg in legs]
    tokens = [peers.issue(run["run_id"], leg, attempt["attempt_id"])
              for leg, attempt in zip(legs, attempts, strict=True)]
    return store, run, peers, attempts, tokens


def test_declared_edges_persist_and_gate_dispatch(dependency_team):
    store, run, peers, attempts, tokens = dependency_team
    declare_dependency(peers, tokens[0], "ws-1", "ws-3", "requires queue API")
    declare_dependency(peers, tokens[1], "ws-2", "ws-3", "requires enqueue API")
    fresh = FleetStore(store.path).get_run(run["run_id"])
    assert [u["dependency_ids"] for u in fresh["policy"]["work_units"]] == [["ws-3"], ["ws-3"], []]
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM fleet_work_unit_dependencies").fetchone()[0] == 2
        contract = json.loads(db.execute("SELECT contract_json FROM fleet_work_units WHERE unit_id='ws-1'").fetchone()[0])
        assert contract["dependency_ids"] == ["ws-3"]
    for a in attempts:
        store.finish_attempt(a["attempt_id"], state="completed")
    ready = store.prepare_phase_runnable(run["run_id"], 1)
    assert ready["waiting_unit_ids"] == ["ws-1", "ws-2"]
    assert len(ready["runnable_leg_ids"]) == 1


def test_cycles_foreign_ownership_and_stale_tokens_are_refused(dependency_team):
    store, run, peers, attempts, tokens = dependency_team
    declare_dependency(peers, tokens[0], "ws-1", "ws-3", "requires queue")
    with pytest.raises(ValueError):
        declare_dependency(peers, tokens[2], "ws-3", "ws-1", "cycle")
    with pytest.raises(PermissionError):
        declare_dependency(peers, tokens[1], "ws-1", "ws-2", "foreign")
    with pytest.raises(ValueError):
        declare_dependency(peers, tokens[1], "ws-2", "foreign-run", "invalid")
    store.finish_attempt(attempts[0]["attempt_id"], state="completed")
    with pytest.raises(PermissionError):
        declare_dependency(peers, tokens[0], "ws-1", "ws-2", "stale")
    assert store.get_run(run["run_id"])["policy"]["work_units"][2]["dependency_ids"] == []


def test_only_exact_declared_dependency_stops_can_auto_resume(dependency_team):
    store, _, peers, attempts, tokens = dependency_team
    marker = declare_dependency(peers, tokens[0], "ws-1", "ws-3", "queue API")["stop_condition"]
    unit = SimpleNamespace(unit_id="ws-1", claimed_status="blocked", stop_condition=marker)
    verdict = SimpleNamespace(terminal_stop=True, units=[unit])
    with store._connect() as db:
        assert declared_wait(db, attempts[0]["attempt_id"], verdict)
        assert not declared_wait(db, attempts[1]["attempt_id"], verdict)
        unit.stop_condition = "requires private credentials"
        assert not declared_wait(db, attempts[0]["attempt_id"], verdict)


def test_explicit_static_dependency_is_validated_before_dispatch():
    policy = build_policy("coding", "tasks:\n- scheduler depends on ws-3\n- webhook depends on ws-3\n- queue",
                          config=builtin_config(), worker_count=3).to_dict()
    assert [u["dependency_ids"] for u in policy["work_units"]] == [["ws-3"], ["ws-3"], []]
    with pytest.raises(ValueError):
        build_policy("coding", "tasks:\n- scheduler depends on ws-2\n- queue depends on ws-1",
                     config=builtin_config(), worker_count=2)


def _park_pair(team):
    store, run, peers, research, _ = team
    for attempt in research:
        store.finish_attempt(attempt["attempt_id"], state="completed", session_id="preserved-research")
    code = store.get_run(run["run_id"])["phases"][1]["legs"]
    for index in (0, 1):
        attempt = store.begin_attempt(code[index]["leg_id"])
        token = peers.issue(run["run_id"], code[index], attempt["attempt_id"])
        marker = declare_dependency(peers, token, f"ws-{index + 1}", "ws-3", "missing queue")["stop_condition"]
        verdict = SimpleNamespace(terminal_stop=True, units=[SimpleNamespace(
            unit_id=f"ws-{index + 1}", claimed_status="blocked", stop_condition=marker)])
        store.finish_attempt(attempt["attempt_id"], state="failed", exit_code=0,
                             input_blocker_reason=marker, dependency_verdict=verdict)
    return code


def test_dependency_wait_preserves_receipts_and_resumes_once(dependency_team):
    store, run, peers, research, _ = dependency_team
    code = _park_pair(dependency_team)
    snapshot = store.get_run(run["run_id"])
    assert [leg["state"] for leg in snapshot["phases"][1]["legs"][:2]] == ["waiting_for_dependencies"] * 2
    with pytest.raises(RuntimeError, match="not yet integrated"):
        store.begin_attempt(code[0]["leg_id"])
    upstream = store.begin_attempt(code[2]["leg_id"])
    store.finish_attempt(upstream["attempt_id"], state="completed")
    ready = store.prepare_phase_runnable(run["run_id"], 1)
    assert set(ready["runnable_leg_ids"]) == {code[0]["leg_id"], code[1]["leg_id"]}
    retry = store.begin_attempt(code[0]["leg_id"])
    assert retry["attempt_number"] == 2
    token = peers.issue(run["run_id"], code[0], retry["attempt_id"])
    marker = declare_dependency(peers, token, "ws-1", "ws-3", "still missing")["stop_condition"]
    verdict = SimpleNamespace(terminal_stop=True, units=[SimpleNamespace(
        unit_id="ws-1", claimed_status="blocked", stop_condition=marker)])
    store.finish_attempt(retry["attempt_id"], state="failed", input_blocker_reason=marker, dependency_verdict=verdict)
    final = store.get_run(run["run_id"])
    assert final["phases"][1]["legs"][0]["state"] == "waiting_for_input"
    assert [leg["current_attempt"]["attempt_id"] for leg in final["phases"][0]["legs"]] == [a["attempt_id"] for a in research]


def test_cancelled_dependency_wait_never_wakes(dependency_team):
    store, run, _, _, _ = dependency_team
    _park_pair(dependency_team)
    store.request_cancel(run["run_id"])
    from fleet.ready_resume import resume_ready_input_runs
    assert resume_ready_input_runs(store) == []
    with pytest.raises(RuntimeError, match="cancellation"):
        store.begin_attempt(store.get_run(run["run_id"])["phases"][1]["legs"][0]["leg_id"])


def test_resident_probe_wakes_only_verified_dependency_work(dependency_team):
    from fleet.ready_resume import resume_ready_input_runs
    store, run, _, _, _ = dependency_team
    code = _park_pair(dependency_team)
    store.resolve_phase_failure(run["run_id"], "execute", "work stopped before completion: dependency")
    assert resume_ready_input_runs(store) == []
    upstream = store.begin_attempt(code[2]["leg_id"])
    store.finish_attempt(upstream["attempt_id"], state="completed")
    assert resume_ready_input_runs(store) == [run["run_id"]]
    assert resume_ready_input_runs(store) == []
    snapshot = store.get_run(run["run_id"])
    assert snapshot["state"] == "queued"
    assert [leg["state"] for leg in snapshot["phases"][1]["legs"]] == ["queued", "queued", "completed"]


def test_declaration_is_idempotent_per_attempt(dependency_team):
    store, run, peers, _, tokens = dependency_team
    for _ in range(3):
        declare_dependency(peers, tokens[0], "ws-1", "ws-3", "queue")
    assert len([e for e in store.events(run["run_id"]) if e["type"] == "worker.dependency_declared"]) == 1
