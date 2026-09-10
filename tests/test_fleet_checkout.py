"""Real Git regression for mandatory baselines on an unrelated dirty checkout."""

import subprocess
from pathlib import Path

import pytest
from test_fleet_isolation import _git, _repo
from test_fleet_supervisor import fleet_env  # noqa: F401

from fleet.checkout import (
    check_checkout_deletable,
    cleanup_run_checkout,
    ensure_run_checkout,
    requested_baseline,
)
from fleet.policy import build_policy, builtin_config
from fleet.store import FleetStore

# ruff: noqa: F811


def setup_run(tmp_path, worker_count=1, branch_template=None):
    root = _repo(tmp_path)
    _git(root, "checkout", "-b", "team-baseline")
    (root / "required.txt").write_text("required baseline\n")
    _git(root, "add", "required.txt")
    _git(root, "commit", "-qm", "team baseline")
    baseline = _git(root, "rev-parse", "HEAD").strip()
    _git(root, "checkout", "main")
    (root / "README.md").write_text("user dirty file\n")
    (root / "private-note.txt").write_text("user untracked file\n")
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = store.create_run(
        task=(f"- MANDATORY start point: branch `team-baseline` at commit {baseline}. DO NOT start from main."
              + (f"\nEach worker creates its own task branch named `{branch_template}` off the baseline."
                 if branch_template else "")),
        activity="coding", cwd=str(root), origin_session_id=None, origin_agent="codex",
        dry_run=False, policy=build_policy("coding", config=builtin_config(),
                                         provider_mode="codex", worker_count=worker_count).to_dict(),
    )
    return root, baseline, store, run


def test_pin_before_research_preserves_original_and_survives_retry(tmp_path):
    root, baseline, store, run = setup_run(tmp_path)
    before = (_git(root, "status", "--porcelain"), _git(root, "rev-parse", "HEAD"),
              (root / ".git" / "index").read_bytes())
    ensure_run_checkout(store, run["run_id"])
    snapshot = store.get_run(run["run_id"])
    checkout = Path(snapshot["cwd"])
    assert checkout != root
    assert _git(checkout, "rev-parse", "HEAD").strip() == baseline
    assert (checkout / "required.txt").read_text() == "required baseline\n"
    assert not (checkout / "private-note.txt").exists()
    (checkout / "accepted-work.txt").write_text("preserve integration\n")
    ensure_run_checkout(FleetStore(store.path), run["run_id"])
    assert (checkout / "accepted-work.txt").read_text() == "preserve integration\n"
    assert before == (_git(root, "status", "--porcelain"), _git(root, "rev-parse", "HEAD"),
                      (root / ".git" / "index").read_bytes())
    assert (root / "README.md").read_text() == "user dirty file\n"
    assert (root / "private-note.txt").read_text() == "user untracked file\n"


def test_baseline_directive_not_arbitrary_citation(tmp_path):
    root = _repo(tmp_path)
    assert requested_baseline("Compare commit abc123 with the old baseline", root) is None
    assert requested_baseline("Fleet baseline: main", root) == _git(root, "rev-parse", "HEAD").strip()
    with pytest.raises(RuntimeError):
        requested_baseline("Fleet baseline: missing", root)


@pytest.mark.parametrize("worker_count,branch_template", [(1, None), (3, None), (3, "codex/team-raghav-hyd-0N")])
def test_real_scheduler_uses_baseline_for_every_phase(fleet_env, monkeypatch, worker_count, branch_template):
    from fleet import supervisor
    from fleet.workers import WorkerResult

    root, baseline, store, run = setup_run(fleet_env, worker_count=worker_count, branch_template=branch_template)
    monkeypatch.setenv("SERENA_FLEET_ISOLATION", "on")
    seen = []
    branches = set()

    def worker(request, **kwargs):
        seen.append(request.phase)
        result = subprocess.run(["git", "-C", request.cwd, "merge-base", "--is-ancestor",
                                 baseline, "HEAD"], capture_output=True)
        assert result.returncode == 0
        assert (Path(request.cwd) / "required.txt").is_file()
        assert Path(request.cwd) != root
        if request.phase in {"execute", "finalize"}:
            assert "Fleet already provisioned your task branch" in request.prompt
            branches.add(_git(Path(request.cwd), "branch", "--show-current").strip())
            owned = Path(request.cwd) / (request.worker_key.replace(":", "-") + ".txt")
            owned.write_text((owned.read_text() if owned.exists() else "") + request.phase + "\n")
        return WorkerResult(True, "baseline verified", "baseline-session", request.model,
                            request.effort, 0)

    monkeypatch.setattr(supervisor, "run_worker", worker)
    result = supervisor.run_supervisor(run["run_id"])
    assert result["state"] == "completed", result.get("error")
    for phase in ("discover", "execute", "verify", "finalize"):
        assert seen.count(phase) == worker_count
    delivered = list(Path(result["cwd"]).glob("agent-*.txt"))
    assert len(delivered) == worker_count
    assert all(path.read_text() == "execute\nfinalize\n" for path in delivered)
    assert not list(root.glob("agent-*.txt"))
    if branch_template:
        assert branches == {"codex/team-raghav-hyd-01", "codex/team-raghav-hyd-02", "codex/team-raghav-hyd-03"}
        assert _git(root, "remote") == ""
    assert _git(root, "branch", "--show-current").strip() == "main"
    assert (root / "README.md").read_text() == "user dirty file\n"


def test_deletion_preserves_accepted_work_and_only_removes_clean_fixture(tmp_path):
    root, baseline, store, run = setup_run(tmp_path)
    ensure_run_checkout(store, run["run_id"])
    run = store.get_run(run["run_id"])
    target = Path(run["cwd"])
    (target / "accepted.txt").write_text("accepted work\n")
    with pytest.raises(RuntimeError, match="retains delivered"):
        check_checkout_deletable(run)
    _git(target, "add", "accepted.txt")
    _git(target, "commit", "-qm", "accepted")
    with pytest.raises(RuntimeError, match="retains delivered"):
        cleanup_run_checkout(run)
    assert (target / "accepted.txt").exists()


def test_empty_baseline_checkout_cleanup_preserves_source(tmp_path):
    root, baseline, store, run = setup_run(tmp_path)
    ensure_run_checkout(store, run["run_id"])
    run = store.get_run(run["run_id"])
    cleanup_run_checkout(run)
    assert not Path(run["cwd"]).exists()
    assert (root / "README.md").read_text() == "user dirty file\n"


def test_learning_identity_requires_matching_checkout_receipt(tmp_path):
    from fleet.learning import project_identity

    root, _, store, run = setup_run(tmp_path)
    ensure_run_checkout(store, run["run_id"])
    run = store.get_run(run["run_id"])
    assert project_identity(run) == str(root.resolve())
    foreign = tmp_path / "other-project"
    assert project_identity({**run, "cwd": str(foreign)}) == str(foreign.resolve())


def test_clean_failed_worker_refreshes_to_required_baseline(tmp_path):
    from fleet.isolation import FleetIsolationStore, ensure_workspace, refresh_workspace_for_retry

    root, baseline, store, run = setup_run(tmp_path)
    isolation = FleetIsolationStore(tmp_path / "isolation.sqlite3", workspace_root=tmp_path / "workers")
    old = ensure_workspace(isolation, run_id=run["run_id"], worker_key="agent:c", cwd=root)
    assert not (Path(old.path) / "required.txt").exists()
    ensure_run_checkout(store, run["run_id"])
    current, receipt = refresh_workspace_for_retry(
        isolation, run_id=run["run_id"], worker_key="agent:c", cwd=store.get_run(run["run_id"])["cwd"],
    )
    assert receipt["action"] == "refreshed"
    assert current.path == old.path
    assert (Path(current.path) / "required.txt").is_file()
    _git(Path(current.path), "merge-base", "--is-ancestor", baseline, "HEAD")


def test_legacy_baseline_adoption_preserves_completed_research(tmp_path):
    root, baseline, store, run = setup_run(tmp_path)
    research = store.begin_attempt(run["phases"][0]["legs"][0]["leg_id"])
    store.finish_attempt(research["attempt_id"], state="completed", output_text="research retained")
    with store._connect() as db:
        db.execute("DELETE FROM fleet_run_checkouts WHERE run_id = ?", (run["run_id"],))
    ensure_run_checkout(store, run["run_id"])
    repaired = store.get_run(run["run_id"])
    assert repaired["checkout"]["baseline"] == baseline
    assert repaired["phases"][0]["legs"][0]["current_attempt"]["attempt_id"] == research["attempt_id"]
    assert repaired["phases"][0]["legs"][0]["state"] == "completed"
    assert (root / "README.md").read_text() == "user dirty file\n"


@pytest.mark.parametrize("state", ["running", "completed"])
def test_legacy_baseline_adoption_refuses_live_or_accepted_write_work(tmp_path, state):
    _, _, store, run = setup_run(tmp_path)
    code = store.begin_attempt(run["phases"][1]["legs"][0]["leg_id"])
    if state == "completed":
        store.finish_attempt(code["attempt_id"], state=state, output_text="accepted work")
    with store._connect() as db:
        db.execute("DELETE FROM fleet_run_checkouts WHERE run_id = ?", (run["run_id"],))
    with pytest.raises(RuntimeError, match="running or accepted"):
        ensure_run_checkout(store, run["run_id"])
    assert store.get_run(run["run_id"])["checkout"] is None


def test_project_checkout_delivery_uses_synced_artifacts(tmp_path, monkeypatch):
    from fleet.checkout import checkout_path

    projects = tmp_path / "Projects"
    source = projects / "company" / "storefront"
    monkeypatch.setattr("core.machine_context.projects_root", lambda: projects)
    database = tmp_path / "private-state" / "fleet.sqlite3"
    assert checkout_path(database, source, "run-id") == projects / "_artifacts" / "fleet-checkouts" / "run-id"
    other = tmp_path / "disposable"
    assert checkout_path(database, other, "run-id") == database.parent / "fleet-checkouts" / "run-id"


def test_requested_branch_never_overwrites_an_existing_user_branch(tmp_path):
    from fleet.isolation import FleetIsolationStore, IsolationError, ensure_workspace

    root = _repo(tmp_path)
    _git(root, "branch", "codex/already-owned")
    old = _git(root, "rev-parse", "codex/already-owned")
    isolation = FleetIsolationStore(tmp_path / "isolation.sqlite3", workspace_root=tmp_path / "workers")
    with pytest.raises(IsolationError, match="outside this worker's ownership"):
        ensure_workspace(isolation, run_id="fixture", worker_key="agent:a", cwd=root,
                         requested_branch="codex/already-owned")
    assert _git(root, "rev-parse", "codex/already-owned") == old
    assert isolation.get_workspace("fixture", "agent:a") is None
