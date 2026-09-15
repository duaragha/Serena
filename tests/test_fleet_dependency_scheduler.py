"""Real scheduler and Git regression; provider turns are deterministic fakes."""

from pathlib import Path

from test_fleet_isolation import _repo
from test_fleet_supervisor import fleet_env  # noqa: F401

from fleet import supervisor
from fleet.collaboration import PeerStore
from fleet.completion import CompletionVerdict, UnitVerdict
from fleet.dependencies import declare_dependency
from fleet.policy import build_policy, builtin_config
from fleet.store import FleetStore
from fleet.workers import WorkerResult

# ruff: noqa: F811


def test_scheduler_refreshes_two_waiters_and_preserves_their_patches(fleet_env, monkeypatch):
    root = _repo(fleet_env)
    (root / "private-note.txt").write_text("user-owned sentinel\n")
    monkeypatch.setenv("SERENA_FLEET_ISOLATION", "on")
    monkeypatch.setenv("SERENA_FLEET_WORKSPACE_ROOT", str(fleet_env / "worktrees"))
    task = "tasks:\n- scheduler\n- webhook\n- queue"
    store = FleetStore(fleet_env / "fleet.sqlite3")
    run = store.create_run(task=task, activity="coding", cwd=str(root),
                           origin_session_id=None, origin_agent=None, dry_run=False,
                           policy=build_policy("coding", task, config=builtin_config(),
                                               worker_count=3).to_dict())
    calls = []

    def worker(request, **kwargs):
        calls.append((request.worker_key, request.phase))
        cwd = Path(request.cwd)
        output = "verified"
        if request.phase == "execute":
            unit = request.assignment_ids[0]
            if unit == "ws-3":
                (cwd / "queue_api.py").write_text("def enqueue_task(): return 42\n")
            else:
                patch = cwd / f"consumer_{unit[-1]}.py"
                if not (cwd / "queue_api.py").exists():
                    patch.write_text("# preserved worker patch\n")
                    declare_dependency(PeerStore(store), request.peer_token, unit, "ws-3", "queue API missing")
                    output = "blocked:" + unit
                else:
                    assert patch.read_text() == "# preserved worker patch\n"
                    patch.write_text(patch.read_text() + "from queue_api import enqueue_task\nassert enqueue_task() == 42\n")
        return WorkerResult(True, output, "session-" + request.worker_key, request.model, request.effort, 0)

    def verdict(store, snapshot, leg, attempt, output, **kwargs):
        if not output.startswith("blocked:"):
            return None
        return CompletionVerdict(True, True, "waiting for queue", True, units=(
            UnitVerdict(output.split(":")[1], "blocked", True, stop_condition="dependency:ws-3"),))

    monkeypatch.setattr(supervisor, "run_worker", worker)
    monkeypatch.setattr(supervisor, "_completion_verdict", verdict)
    for leg in run["phases"][0]["legs"]:
        attempt = store.begin_attempt(leg["leg_id"])
        store.finish_attempt(attempt["attempt_id"], state="completed")
    for leg in run["phases"][1]["legs"][:2]:
        supervisor._execute_leg(store, run["run_id"], leg)
    parked = store.get_run(run["run_id"])
    assert [leg["state"] for leg in parked["phases"][1]["legs"][:2]] == ["waiting_for_dependencies"] * 2
    result = supervisor.run_supervisor(run["run_id"])
    assert result["state"] == "completed", result.get("error")
    assert [leg["attempt_count"] for leg in result["phases"][1]["legs"]] == [2, 2, 1]
    import subprocess
    import sys
    for index in (1, 2):
        subprocess.run([sys.executable, str(root / f"consumer_{index}.py")], cwd=root, check=True)
    assert (root / "private-note.txt").read_text() == "user-owned sentinel\n"
    assert len([e for e in store.events(run["run_id"]) if e["type"] == "leg.dependencies_resumed"]) == 2


def test_runtime_preflight_failure_never_calls_provider(fleet_env, monkeypatch):
    store = FleetStore(fleet_env / "fleet.sqlite3")
    run = store.create_run(task="test", activity="coding", cwd=str(fleet_env),
                           origin_session_id=None, origin_agent=None, dry_run=False,
                           policy=build_policy("coding", config=builtin_config(), worker_count=1).to_dict())
    def refused(request):
        raise PermissionError("private scratch unavailable")
    def forbidden(*args, **kwargs):
        raise AssertionError("provider called after preflight failure")
    monkeypatch.setattr("fleet.worker_runtime.preflight", refused)
    monkeypatch.setattr(supervisor, "run_worker", forbidden)
    result = supervisor._execute_leg(store, run["run_id"], run["phases"][0]["legs"][0])
    assert not result.ok
    assert "private scratch unavailable" in result.error
