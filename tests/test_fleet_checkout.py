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


def setup_run(tmp_path):
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
        task=f"- MANDATORY start point: branch `team-baseline` at commit {baseline}. DO NOT start from main.",
        activity="coding", cwd=str(root), origin_session_id=None, origin_agent="codex",
        dry_run=False, policy=build_policy("coding", config=builtin_config(),
                                         provider_mode="codex", worker_count=1).to_dict(),
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


def test_real_scheduler_uses_baseline_for_every_phase(fleet_env, monkeypatch):
    from fleet import supervisor
    from fleet.workers import WorkerResult

    root, baseline, store, run = setup_run(fleet_env)
    monkeypatch.setenv("SERENA_FLEET_ISOLATION", "on")
    seen = []

    def worker(request, **kwargs):
        seen.append(request.phase)
        result = subprocess.run(["git", "-C", request.cwd, "merge-base", "--is-ancestor",
                                 baseline, "HEAD"], capture_output=True)
        assert result.returncode == 0
        assert (Path(request.cwd) / "required.txt").is_file()
        assert Path(request.cwd) != root
        return WorkerResult(True, "baseline verified", "baseline-session", request.model,
                            request.effort, 0)

    monkeypatch.setattr(supervisor, "run_worker", worker)
    result = supervisor.run_supervisor(run["run_id"])
    assert result["state"] == "completed", result.get("error")
    assert seen == ["discover", "execute", "verify", "finalize"]
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
