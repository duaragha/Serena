"""Durable replay admission, cancellation, and saved-patch integrity."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fleet import integration_recovery as recovery
from fleet.isolation import FleetIsolationStore, ensure_workspace, integrate_workspace
from fleet.store import FleetStore
from test_fleet_isolation import _git, _repo
from test_fleet_policy_store import _create


def _failed(tmp_path, monkeypatch, phase_index=3):
    root = _repo(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setenv("SERENA_FLEET_DB_PATH", str(tmp_path / "fleet.sqlite3"))
    monkeypatch.setenv("SERENA_FLEET_ISOLATION_DB_PATH", str(tmp_path / "isolation.sqlite3"))
    monkeypatch.setenv("SERENA_FLEET_WORKSPACE_ROOT", str(tmp_path / "worktrees"))
    (root / "package.json").write_text('{"scripts":{"codegen":"generator"}}')
    _git(root, "add", "package.json")
    _git(root, "commit", "-qm", "generator contract")
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store, activity="coding")
    rid = run["run_id"]
    store.claim_run(rid)
    for leg in run["phases"][0]["legs"]:
        attempt = store.begin_attempt(leg["leg_id"])
        store.finish_attempt(attempt["attempt_id"], state="completed", output_text="research")
    leg = run["phases"][phase_index]["legs"][0]
    sibling = run["phases"][1]["legs"][1]
    attempt = store.begin_attempt(sibling["leg_id"])
    store.finish_attempt(attempt["attempt_id"], state="failed", error="private evidence required",
                         input_blocker_reason="private evidence required")
    attempt = store.begin_attempt(leg["leg_id"])
    isolation = FleetIsolationStore()
    isolation.claim_paths(run_id=rid, worker_key="agent:a", paths=["*"])
    workspace = ensure_workspace(isolation, run_id=rid, worker_key="agent:a", cwd=root)
    (Path(workspace.path) / "core/alpha.py").write_text("alpha = 2\n")
    # Record a real rejected integration and its durable recovery patch.
    import sys
    result = integrate_workspace(isolation, run_id=rid, worker_key="agent:a", cwd=root,
                                 test_gate=[sys.executable, "-c", "raise SystemExit(2)"])
    payload = result.to_dict()
    assert result.patch_path, result.reason
    payload["test_gate"] = {"ran": True, "ok": False, "exit_code": 2,
                            "command": ["npm", "run", "typecheck"],
                            "output_tail": "error TS2307: Cannot find module 'storefrontapi.generated'"}
    store.append_event(rid, "leg.completion_evidence_accepted", {"completion_allowed": True},
                       leg_id=leg["leg_id"], attempt_id=attempt["attempt_id"])
    store.append_event(rid, "worker.integration.rejected", payload,
                       leg_id=leg["leg_id"], attempt_id=attempt["attempt_id"])
    contract = run["policy"]["work_units"][0]["completion_contract"]
    evidence = {"schema_version": 1, "units": [{
        "id": "ws-1", "status": "completed", "constraints_respected": True,
        "changed_paths": ["core/alpha.py"], "stop_condition": "",
        "acceptance": [{"criterion": item, "met": True,
                        "evidence": "Updated core/alpha.py in the isolated checkout; git diff --check passed; peer files unchanged."}
                       for item in contract["acceptance_criteria"]],
        "tests": [{"command": "git diff --check", "exit_code": 0}],
    }]}
    output = "Updated alpha in the isolated checkout and verified the patch; no external operations were performed.\n"
    output += "<serena-evidence>" + json.dumps(evidence) + "</serena-evidence>"
    store.finish_attempt(attempt["attempt_id"], state="failed", exit_code=0,
                         error=result.reason, output_text=output)
    isolation.release_claims(rid, "agent:a")
    store.resolve_phase_failure(rid, "execute", result.reason)
    return store, rid, leg, sibling, workspace, payload


def test_queue_is_once_only_and_preserves_sibling_and_original_attempt(tmp_path, monkeypatch):
    store, rid, leg, sibling, _, _ = _failed(tmp_path, monkeypatch)
    before = store.get_run(rid)
    assert recovery.resume_saved_integrations(store) == [rid]
    assert recovery.resume_saved_integrations(store) == []
    after = store.get_run(rid)
    assert after["phases"][3]["legs"][0]["state"] == "queued"
    assert after["phases"][3]["legs"][0]["current_attempt"] == before["phases"][3]["legs"][0]["current_attempt"]
    assert after["phases"][1]["legs"][1] == before["phases"][1]["legs"][1]


def test_cancellation_wins_before_queue(tmp_path, monkeypatch):
    store, rid, *_ = _failed(tmp_path, monkeypatch)
    store.request_cancel(rid)
    assert recovery.resume_saved_integrations(store) == []
    assert store.get_run(rid)["state"] == "cancelled"


@pytest.mark.parametrize("changed", [False, True])
def test_replay_checks_real_patch_and_never_calls_provider(tmp_path, monkeypatch, changed):
    from fleet import supervisor
    store, rid, leg, _, workspace, _ = _failed(tmp_path, monkeypatch)
    assert recovery.resume_saved_integrations(store) == [rid]
    if changed:
        (Path(workspace.path) / "core/alpha.py").write_text("alpha = 999\n")
    monkeypatch.setattr(supervisor, "run_worker", lambda *a, **kw: pytest.fail("provider called during replay"))
    monkeypatch.setattr(supervisor, "_completion_verdict", lambda *a, **kw: SimpleNamespace(
        completion_allowed=True, units=[SimpleNamespace(changed_paths=["core/alpha.py"])],
    ))
    monkeypatch.setattr(supervisor, "_declared_integration_tests", lambda *a: [["git", "diff", "--check"]])
    fresh = store.get_run(rid)["phases"][3]["legs"][0]
    result = supervisor._execute_leg(store, rid, fresh)
    assert result.ok is (not changed)
    current = store.get_run(rid)["phases"][3]["legs"][0]
    assert current["attempt_count"] == 2
    assert current["current_attempt"]["actual_model"] is None
    assert current["current_attempt"]["actual_effort"] is None
    assert current["state"] == ("waiting_for_input" if changed else "completed")
    assert (Path(store.get_run(rid)["cwd"]) / "core/alpha.py").read_text() == ("alpha = 1\n" if changed else "alpha = 2\n")
    assert recovery.resume_saved_integrations(store) == []


def test_patch_fingerprint_guard_prevents_integration(tmp_path, monkeypatch):
    store, rid, _, _, _, payload = _failed(tmp_path, monkeypatch)
    isolation = FleetIsolationStore()
    isolation.claim_paths(run_id=rid, worker_key="agent:a", paths=["*"])
    digest = hashlib.sha256(Path(payload["patch_path"]).read_bytes()).hexdigest()
    result = integrate_workspace(isolation, run_id=rid, worker_key="agent:a", cwd=store.get_run(rid)["cwd"],
                                 expected_patch_sha256="0" * len(digest))
    assert not result.ok
    assert "fingerprint changed" in result.reason


def test_replay_uses_real_completion_validator_and_git_gate(tmp_path, monkeypatch):
    from fleet import supervisor
    store, rid, _, _, _, _ = _failed(tmp_path, monkeypatch)
    assert recovery.resume_saved_integrations(store) == [rid]
    monkeypatch.setattr(supervisor, "run_worker", lambda *a, **kw: pytest.fail("provider called"))
    leg = store.get_run(rid)["phases"][3]["legs"][0]
    result = supervisor._execute_leg(store, rid, leg)
    assert result.ok, result.error
    events = store.events(rid)
    accepted = [e for e in events if e["type"] == "leg.completion_evidence_accepted"]
    assert len(accepted) == 2
    assert accepted[-1]["payload"]["completion_allowed"] is True


def test_cancellation_after_queue_does_not_launch_replay(tmp_path, monkeypatch):
    store, rid, _, _, _, _ = _failed(tmp_path, monkeypatch)
    assert recovery.resume_saved_integrations(store) == [rid]
    store.request_cancel(rid)
    leg = store.get_run(rid)["phases"][3]["legs"][0]
    assert recovery.execute_saved_integration(store, rid, leg) is None
    assert leg["attempt_count"] == 1


def test_phase_projected_input_wait_can_recover_with_accepted_evidence(tmp_path, monkeypatch):
    store, rid, *_ = _failed(tmp_path, monkeypatch, phase_index=1)
    assert store.get_run(rid)["phases"][1]["legs"][0]["state"] == "waiting_for_input"
    assert recovery.resume_saved_integrations(store) == [rid]
    assert store.get_run(rid)["phases"][1]["legs"][1]["state"] == "waiting_for_input"


def test_without_accepted_completion_evidence_no_replay_is_queued(tmp_path, monkeypatch):
    store, rid, *_ = _failed(tmp_path, monkeypatch)
    with store._connect() as db:
        db.execute("DELETE FROM fleet_events WHERE type='leg.completion_evidence_accepted'")
    assert recovery.resume_saved_integrations(store) == []


def test_concurrent_polls_create_only_one_replay(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    store, rid, *_ = _failed(tmp_path, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: recovery.resume_saved_integrations(store), range(2)))
    assert sum(result == [rid] for result in results) == 1
    assert len([e for e in store.events(rid) if e["type"] == "leg.integration_replay_queued"]) == 1


@pytest.mark.parametrize("checkout_state", ["ready", "pending"])
def test_recovery_reads_frozen_checkout_not_source(tmp_path, monkeypatch, checkout_state):
    from fleet.observability import autonomy_projection
    store, rid, *_ = _failed(tmp_path, monkeypatch)
    original = store.get_run(rid)["cwd"]
    source = tmp_path / "unrelated-source"
    source.mkdir()
    with store._connect() as db:
        db.execute("UPDATE fleet_runs SET cwd=? WHERE run_id=?", (str(source), rid))
        db.execute("INSERT INTO fleet_run_checkouts VALUES (?,?,?,?,?)",
                   (rid, str(source), "fixture-baseline", original, checkout_state))
    assert recovery.resume_saved_integrations(store) == ([rid] if checkout_state == "ready" else [])
    timeline = autonomy_projection(store, rid)["timeline"]
    assert any(e["type"] == "leg.integration_replay_queued" for e in timeline) is (checkout_state == "ready")
