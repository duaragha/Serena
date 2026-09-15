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
