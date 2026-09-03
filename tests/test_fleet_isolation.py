"""Adversarial tests for Fleet's writer isolation, claims, and integration gates.

Every test that matters here is an attack: a worker delivering paths it never
claimed, two workers claiming the same surface, a merge that would land on top
of uncommitted base work, a protected path, a failing test gate. The gates are
only real if these are refused.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.fleet_isolation import (
    ROOT_CLAIM,
    FleetIsolationStore,
    IsolationError,
    assess_isolation,
    cleanup_workspace,
    dependency_sync_command,
    ensure_workspace,
    integrate_workspace,
    is_protected_path,
    normalise_claim_path,
    paths_overlap,
    plan_integration_order,
    refresh_workspace_for_retry,
    repository_integration_lock,
    rollback_integration,
    unrecovered_workspaces,
    workspace_changed_paths,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return result.stdout


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "core").mkdir()
    (root / "core" / "alpha.py").write_text("alpha = 1\n")
    (root / "core" / "beta.py").write_text("beta = 1\n")
    (root / "README.md").write_text("readme\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _store(tmp_path: Path) -> FleetIsolationStore:
    return FleetIsolationStore(
        tmp_path / "isolation.sqlite3", workspace_root=tmp_path / "worktrees"
    )


# ---- path algebra ---------------------------------------------------------


def test_claim_paths_are_normalised_and_bounded():
    assert normalise_claim_path("./core//alpha.py") == "core/alpha.py"
    for bad in ("/etc/passwd", "../outside.py", "", "   "):
        with pytest.raises(ValueError):
            normalise_claim_path(bad)


def test_directory_claims_overlap_their_children_but_not_siblings():
    assert paths_overlap("core", "core/alpha.py")
    assert paths_overlap("core/alpha.py", "core")
    assert paths_overlap("core/alpha.py", "core/alpha.py")
    assert not paths_overlap("core/alpha.py", "core/beta.py")
    # The prefix check must not treat a shared name fragment as containment.
    assert not paths_overlap("core", "core_extra/thing.py")


def test_repository_claim_serializes_workers_without_declared_paths():
    assert paths_overlap(ROOT_CLAIM, "core/alpha.py")
    assert paths_overlap("README.md", ROOT_CLAIM)


def test_isolated_root_claims_are_exclusive_even_across_worktrees(tmp_path):
    store = _store(tmp_path)
    first = store.claim_paths(
        run_id="run-1",
        worker_key="codex:a",
        paths=[ROOT_CLAIM],
    )
    second = store.claim_paths(
        run_id="run-1",
        worker_key="codex:b",
        paths=[ROOT_CLAIM],
    )

    assert first.ok
    assert not second.ok
    assert second.conflicts[0]["held_by"] == "codex:a"


def test_protected_paths_are_recognised():
    assert is_protected_path(".git/config")
    assert is_protected_path("config/secrets/token.json")
    assert is_protected_path("deploy/server.pem")
    assert is_protected_path("voice/telegram.env")
    assert not is_protected_path("core/fleet_store.py")


def test_repository_integration_lock_blocks_another_process(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setenv("SERENA_FLEET_INTEGRATION_LOCK_ROOT", str(tmp_path / "locks"))
    script = """
import sys
from core.fleet_isolation import repository_integration_lock
with repository_integration_lock(sys.argv[1]):
    print('acquired', flush=True)
"""
    with repository_integration_lock(root):
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(root)],
            cwd=str(Path(__file__).parents[1]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.2)
        assert child.poll() is None
    stdout, stderr = child.communicate(timeout=5)

    assert child.returncode == 0, stderr
    assert stdout.strip() == "acquired"


# ---- preflight ------------------------------------------------------------


def test_dirty_base_still_permits_worktree_isolation(tmp_path):
    root = _repo(tmp_path)
    (root / "core" / "alpha.py").write_text("alpha = 'uncommitted user work'\n")
    (root / "untracked.txt").write_text("unrelated\n")

    assessment = assess_isolation(root)

    assert assessment.safe is True
    assert assessment.mode == "worktree"
    assert "core/alpha.py" in assessment.base_dirty_paths
    assert "untracked.txt" in assessment.base_dirty_paths


def test_dirty_base_is_visible_in_the_workspace_and_preserved_on_integration(tmp_path):
    root = _repo(tmp_path)
    (root / "core" / "alpha.py").write_text("alpha = 'raghav dirty'\n")
    (root / "untracked.txt").write_text("keep me\n")
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=[ROOT_CLAIM])

    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    assert (Path(workspace.path) / "core" / "alpha.py").read_text() == "alpha = 'raghav dirty'\n"
    assert (Path(workspace.path) / "untracked.txt").read_text() == "keep me\n"
    (Path(workspace.path) / "core" / "beta.py").write_text("beta = 'worker'\n")
    result = integrate_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    assert result.ok is True, result.reason
    assert (root / "core" / "alpha.py").read_text() == "alpha = 'raghav dirty'\n"
    assert (root / "untracked.txt").read_text() == "keep me\n"
    assert (root / "core" / "beta.py").read_text() == "beta = 'worker'\n"


def test_integrated_workspace_refreshes_from_the_combined_dirty_base(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=[ROOT_CLAIM])
    first = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(first.path) / "core" / "beta.py").write_text("beta = 2\n")
    assert integrate_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root).ok

    second = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    assert second.base_head != first.base_head
    assert (Path(second.path) / "core" / "beta.py").read_text() == "beta = 2\n"
    (Path(second.path) / "core" / "beta.py").write_text("beta = 3\n")
    result = integrate_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    assert result.ok is True, result.reason
    assert (root / "core" / "beta.py").read_text() == "beta = 3\n"


def test_non_repository_fails_closed_to_shared_fallback(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    assessment = assess_isolation(plain)

    assert assessment.safe is False
    assert assessment.mode == "shared_fallback"
    assert "not an isolable git repository" in assessment.reason


def test_interrupted_merge_blocks_isolation(tmp_path):
    root = _repo(tmp_path)
    git_dir = Path(_git(root, "rev-parse", "--absolute-git-dir").strip())
    (git_dir / "MERGE_HEAD").write_text("deadbeef\n")

    assessment = assess_isolation(root)

    assert assessment.safe is False
    assert assessment.mode == "shared_fallback"
    assert "already in progress" in assessment.reason


def test_unsafe_repository_refuses_to_create_a_workspace(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    store = _store(tmp_path)

    with pytest.raises(IsolationError):
        ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=plain)


# ---- claim registry -------------------------------------------------------


def test_overlapping_write_claims_are_refused(tmp_path):
    store = _store(tmp_path)

    first = store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["core/alpha.py"])
    second = store.claim_paths(run_id="run-1", worker_key="codex:b", paths=["core"])

    assert first.ok is True
    assert first.granted == ["core/alpha.py"]
    assert second.ok is False
    assert second.granted == []
    assert second.conflicts[0]["held_by"] == "claude:a"


def test_claims_are_all_or_nothing(tmp_path):
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["core/alpha.py"])

    decision = store.claim_paths(
        run_id="run-1", worker_key="codex:b", paths=["core/beta.py", "core/alpha.py"]
    )

    assert decision.ok is False
    # The uncontested path must not be silently granted alongside the refusal.
    assert store.active_claims("run-1", worker_key="codex:b") == []


def test_two_readers_may_share_but_a_writer_may_not(tmp_path):
    store = _store(tmp_path)

    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["core"], mode="read")
    reader = store.claim_paths(run_id="run-1", worker_key="codex:b", paths=["core"], mode="read")
    writer = store.claim_paths(run_id="run-1", worker_key="codex:a", paths=["core"], mode="write")

    assert reader.ok is True
    assert writer.ok is False


def test_protected_paths_cannot_be_claimed(tmp_path):
    store = _store(tmp_path)

    decision = store.claim_paths(
        run_id="run-1", worker_key="claude:a", paths=["core/alpha.py", ".git/config"]
    )

    assert decision.ok is False
    assert decision.rejected[0]["path"] == ".git/config"
    assert store.active_claims("run-1") == []


def test_escaping_claims_are_rejected(tmp_path):
    store = _store(tmp_path)

    decision = store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["../../etc/passwd"])

    assert decision.ok is False
    assert store.active_claims("run-1") == []


def test_claims_transfer_to_the_replacement_worker(tmp_path):
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["core/alpha.py"])

    moved = store.transfer_claims(
        run_id="run-1", from_worker="claude:a", to_worker="codex:a", reason="provider handoff"
    )

    assert moved == ["core/alpha.py"]
    assert [row["path"] for row in store.active_claims("run-1", worker_key="codex:a")] == [
        "core/alpha.py"
    ]
    assert store.active_claims("run-1", worker_key="claude:a") == []
    # The replacement must not now collide with the slot it inherited.
    again = store.claim_paths(run_id="run-1", worker_key="codex:a", paths=["core/alpha.py"])
    assert again.ok is True


def test_released_claims_free_the_surface(tmp_path):
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["core/alpha.py"])

    assert store.release_claims("run-1", "claude:a") == 1
    assert store.claim_paths(run_id="run-1", worker_key="codex:b", paths=["core/alpha.py"]).ok


def test_claims_are_scoped_per_run(tmp_path):
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["core/alpha.py"])

    other = store.claim_paths(run_id="run-2", worker_key="codex:b", paths=["core/alpha.py"])

    assert other.ok is True


# ---- worktree lifecycle ---------------------------------------------------


def test_worker_edits_never_touch_the_base_checkout(tmp_path):
    root = _repo(tmp_path)
    (root / "core" / "beta.py").write_text("beta = 'user dirty work'\n")
    store = _store(tmp_path)

    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(workspace.path) / "core" / "alpha.py").write_text("alpha = 'worker edit'\n")

    assert (root / "core" / "alpha.py").read_text() == "alpha = 1\n"
    assert (root / "core" / "beta.py").read_text() == "beta = 'user dirty work'\n"
    assert workspace_changed_paths(workspace) == ["core/alpha.py"]


def test_the_same_worker_reuses_its_workspace_across_phases(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)

    first = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(first.path) / "core" / "alpha.py").write_text("alpha = 'phase one'\n")
    second = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    assert second.path == first.path
    assert (Path(second.path) / "core" / "alpha.py").read_text() == "alpha = 'phase one'\n"


def test_a_zero_byte_git_link_is_quarantined_before_workspace_reuse(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    first = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    first_path = Path(first.path)
    (first_path / ".git").write_text("")

    second = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    assert second.path == first.path
    assert (Path(second.path) / ".git").stat().st_size > 0
    quarantined = list(first_path.parent.glob(".claude-a.invalid-*"))
    assert len(quarantined) == 1
    assert (quarantined[0] / ".git").stat().st_size == 0


def test_peer_workers_get_separate_worktrees(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)

    a = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    b = ensure_workspace(store, run_id="run-1", worker_key="codex:b", cwd=root)

    assert a.path != b.path
    assert a.branch != b.branch
    (Path(a.path) / "core" / "alpha.py").write_text("from a\n")
    (Path(b.path) / "core" / "beta.py").write_text("from b\n")
    assert (Path(a.path) / "core" / "beta.py").read_text() == "beta = 1\n"


def test_integration_order_is_deterministic(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    ensure_workspace(store, run_id="run-1", worker_key="codex:b", cwd=root)
    ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    ordered = [item.worker_key for item in plan_integration_order(store.workspaces("run-1"))]

    assert ordered == ["claude:a", "codex:b"]
    assert ordered == [
        item.worker_key
        for item in plan_integration_order(list(reversed(store.workspaces("run-1"))))
    ]


# ---- integration gates ----------------------------------------------------


def test_integration_applies_claimed_work_and_leaves_evidence(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["core/alpha.py"])
    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(workspace.path) / "core" / "alpha.py").write_text("alpha = 'integrated'\n")

    result = integrate_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    assert result.ok is True, result.reason
    assert (root / "core" / "alpha.py").read_text() == "alpha = 'integrated'\n"
    assert result.changed_paths == ["core/alpha.py"]
    assert Path(result.patch_path).is_file()
    assert store.get_workspace("run-1", "claude:a").state == "integrated"


def test_published_stacked_branch_records_only_its_own_suffix(tmp_path):
    root = _repo(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-u", "origin", "main")
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=[ROOT_CLAIM])
    workspace = ensure_workspace(
        store, run_id="run-1", worker_key="claude:a", cwd=root
    )
    worker = Path(workspace.path)

    _git(worker, "switch", "-c", "dependency")
    (worker / "core" / "beta.py").write_text("beta = 'dependency'\n")
    _git(worker, "add", "core/beta.py")
    _git(worker, "commit", "-qm", "dependency")
    _git(worker, "switch", "-c", "feature")
    (worker / "core" / "alpha.py").write_text("alpha = 'worker'\n")
    _git(worker, "add", "core/alpha.py")
    _git(worker, "commit", "-qm", "feature")
    _git(worker, "push", "-u", "origin", "feature")

    assert workspace_changed_paths(workspace, ["core/alpha.py"]) == [
        "core/alpha.py"
    ]
    result = integrate_workspace(
        store,
        run_id="run-1",
        worker_key="claude:a",
        cwd=root,
        declared_paths=["core/alpha.py"],
    )

    assert result.ok is True, result.reason
    assert result.delivery_mode == "published_branch"
    assert result.delivery_branch == "feature"
    assert result.changed_paths == ["core/alpha.py"]
    assert result.applied is False
    assert (root / "core" / "alpha.py").read_text() == "alpha = 1\n"
    assert (root / "core" / "beta.py").read_text() == "beta = 1\n"
    refreshed = store.get_workspace("run-1", "claude:a")
    assert refreshed is not None and refreshed.state == "delivered"
    assert refreshed.base_head == _git(worker, "rev-parse", "HEAD").strip()
    history = store.integrations("run-1")[-1]
    assert history["delivery_mode"] == "published_branch"
    assert history["applied"] is False


def test_switched_unpublished_branch_never_falls_back_to_local_integration(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=[ROOT_CLAIM])
    workspace = ensure_workspace(
        store, run_id="run-1", worker_key="claude:a", cwd=root
    )
    worker = Path(workspace.path)
    _git(worker, "switch", "-c", "unpublished")
    (worker / "core" / "alpha.py").write_text("alpha = 'worker'\n")
    _git(worker, "add", "core/alpha.py")
    _git(worker, "commit", "-qm", "feature")

    result = integrate_workspace(
        store,
        run_id="run-1",
        worker_key="claude:a",
        cwd=root,
        declared_paths=["core/alpha.py"],
    )

    assert result.ok is False
    assert "must be pushed with an upstream" in result.reason
    assert (root / "core" / "alpha.py").read_text() == "alpha = 1\n"


def test_integration_refuses_a_worker_the_supervisor_never_preclaimed(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(workspace.path) / "core" / "alpha.py").write_text("alpha = 'integrated'\n")

    result = integrate_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    assert result.ok is False
    assert "supervisor ownership claim is missing" in result.reason
    assert result.unclaimed_paths == ["core/alpha.py"]
    assert (root / "core" / "alpha.py").read_text() == "alpha = 1\n"


def test_integration_refuses_paths_the_worker_never_claimed(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["core/alpha.py"])
    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(workspace.path) / "core" / "alpha.py").write_text("alpha = 'claimed'\n")
    (Path(workspace.path) / "core" / "beta.py").write_text("beta = 'NOT claimed'\n")

    result = integrate_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    assert result.ok is False
    assert result.unclaimed_paths == ["core/beta.py"]
    # Nothing at all lands, including the legitimately claimed file.
    assert (root / "core" / "alpha.py").read_text() == "alpha = 1\n"
    assert (root / "core" / "beta.py").read_text() == "beta = 1\n"
    assert store.get_workspace("run-1", "claude:a").state == "blocked"


def test_a_blocked_workspace_is_recovered_then_reforked_for_retry(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["core/alpha.py"])
    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    repaired = Path(workspace.path) / "core" / "beta.py"
    repaired.write_text("beta = 'needs a claim'\n")

    refused = integrate_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    resumed = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    assert refused.ok is False
    assert Path(refused.patch_path).is_file()
    assert resumed.path == workspace.path
    assert resumed.updated_at >= workspace.updated_at
    assert repaired.read_text() == "beta = 1\n"


def test_retry_reforks_from_latest_base_and_reapplies_a_clean_patch(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="codex:a", paths=[ROOT_CLAIM])
    original = ensure_workspace(store, run_id="run-1", worker_key="codex:a", cwd=root)
    (Path(original.path) / "core" / "alpha.py").write_text("alpha = 'worker'\n")
    (root / "core" / "beta.py").write_text("beta = 'peer integration'\n")

    refreshed, recovery = refresh_workspace_for_retry(
        store,
        run_id="run-1",
        worker_key="codex:a",
        cwd=root,
    )

    assert recovery["action"] == "reforked_reapplied"
    assert Path(recovery["patch_path"]).is_file()
    assert refreshed.base_head != original.base_head
    assert (Path(refreshed.path) / "core" / "alpha.py").read_text() == "alpha = 'worker'\n"
    assert (
        Path(refreshed.path) / "core" / "beta.py"
    ).read_text() == "beta = 'peer integration'\n"


def test_retry_reforks_but_never_applies_a_patch_over_newer_same_path_work(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="codex:a", paths=[ROOT_CLAIM])
    original = ensure_workspace(store, run_id="run-1", worker_key="codex:a", cwd=root)
    (Path(original.path) / "core" / "alpha.py").write_text("alpha = 'worker'\n")
    (root / "core" / "alpha.py").write_text("alpha = 'newer peer work'\n")

    refreshed, recovery = refresh_workspace_for_retry(
        store,
        run_id="run-1",
        worker_key="codex:a",
        cwd=root,
    )

    assert recovery["action"] == "reforked_conflict"
    assert recovery["drift_paths"] == ["core/alpha.py"]
    assert Path(recovery["patch_path"]).is_file()
    assert (
        Path(refreshed.path) / "core" / "alpha.py"
    ).read_text() == "alpha = 'newer peer work'\n"


def test_integration_never_overwrites_uncommitted_base_work(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["core/alpha.py"])
    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(workspace.path) / "core" / "alpha.py").write_text("alpha = 'from worker'\n")
    # Raghav edits the same file in the real checkout while the worker runs.
    (root / "core" / "alpha.py").write_text("alpha = 'PRECIOUS UNCOMMITTED WORK'\n")

    result = integrate_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    assert result.ok is False
    assert result.dirty_conflicts == ["core/alpha.py"]
    assert Path(result.patch_path).is_file()
    assert (root / "core" / "alpha.py").read_text() == "alpha = 'PRECIOUS UNCOMMITTED WORK'\n"
    assert store.get_workspace("run-1", "claude:a").state == "blocked"


def test_worker_can_update_an_unchanged_untracked_baseline_file(tmp_path):
    """Synthetic baselines must compare untracked contents, not Git status."""

    root = _repo(tmp_path)
    (root / "operator.py").write_text("value = 'dirty baseline'\n")
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["operator.py"])
    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(workspace.path) / "operator.py").write_text("value = 'worker update'\n")

    result = integrate_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    assert result.ok is True, result.reason
    assert (root / "operator.py").read_text() == "value = 'worker update'\n"


def test_integration_preserves_crlf_patch_context_byte_exactly(tmp_path):
    """Patch serialization must not erase CR bytes before plain git apply."""

    root = _repo(tmp_path)
    source = root / "core" / "alpha.py"
    source.write_bytes(b"alpha = 1\r\nbeta = 1\r\n")
    _git(root, "add", "core/alpha.py")
    _git(root, "commit", "-qm", "crlf baseline")
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="codex:a", paths=["core/alpha.py"])
    workspace = ensure_workspace(store, run_id="run-1", worker_key="codex:a", cwd=root)
    expected = b"alpha = 2\nbeta = 1\r\n"
    (Path(workspace.path) / "core" / "alpha.py").write_bytes(expected)

    result = integrate_workspace(store, run_id="run-1", worker_key="codex:a", cwd=root)

    assert result.ok is True, result.reason
    assert source.read_bytes() == expected


def test_worker_cannot_overwrite_an_untracked_file_edited_after_fork(tmp_path):
    root = _repo(tmp_path)
    (root / "operator.py").write_text("value = 'dirty baseline'\n")
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["operator.py"])
    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(workspace.path) / "operator.py").write_text("value = 'worker update'\n")
    (root / "operator.py").write_text("value = 'raghav changed this'\n")

    result = integrate_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    assert result.ok is False
    assert result.dirty_conflicts == ["operator.py"]
    assert (root / "operator.py").read_text() == "value = 'raghav changed this'\n"


def test_a_failing_test_gate_rolls_the_integration_back(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["core/alpha.py"])
    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(workspace.path) / "core" / "alpha.py").write_text("alpha = 'broken'\n")

    result = integrate_workspace(
        store,
        run_id="run-1",
        worker_key="claude:a",
        cwd=root,
        test_gate=["false"],
    )

    assert result.ok is False
    assert result.test_gate["ran"] is True
    assert result.test_gate["ok"] is False
    assert "rolled back" in result.reason
    assert (root / "core" / "alpha.py").read_text() == "alpha = 1\n"


def test_a_passing_test_gate_admits_the_integration(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["core/alpha.py"])
    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(workspace.path) / "core" / "alpha.py").write_text("alpha = 'good'\n")

    result = integrate_workspace(
        store, run_id="run-1", worker_key="claude:a", cwd=root, test_gate=["true"]
    )

    assert result.ok is True, result.reason
    assert result.test_gate["ok"] is True
    assert (root / "core" / "alpha.py").read_text() == "alpha = 'good'\n"


def test_node_dependency_changes_sync_before_merged_tree_checks(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    (root / "package.json").write_text('{"name":"fixture"}\n')
    (root / "package-lock.json").write_text(
        '{"name":"fixture","lockfileVersion":3,"packages":{"":{"name":"fixture"}}}\n'
    )
    _git(root, "add", "package.json", "package-lock.json")
    _git(root, "commit", "-qm", "add node fixture")
    store = _store(tmp_path)
    store.claim_paths(
        run_id="run-1", worker_key="claude:a", paths=["package.json"]
    )
    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(workspace.path) / "package.json").write_text(
        '{"name":"fixture","devDependencies":{"new-rule":"1.0.0"}}\n'
    )
    calls: list[tuple[str, ...]] = []

    def fake_sync(sync_root, changed_paths, *, timeout=900):
        del timeout
        calls.append(tuple(changed_paths))
        (Path(sync_root) / ".dependency-synced").write_text("yes\n")
        return {"ran": True, "ok": True, "command": ["npm", "ci"], "exit_code": 0}

    monkeypatch.setattr("core.fleet_isolation.run_dependency_sync", fake_sync)
    result = integrate_workspace(
        store,
        run_id="run-1",
        worker_key="claude:a",
        cwd=root,
        declared_tests=[
            [
                sys.executable,
                "-c",
                "import pathlib,sys;sys.exit(0 if pathlib.Path('.dependency-synced').is_file() else 1)",
            ]
        ],
    )

    assert result.ok is True, result.reason
    assert calls == [("package.json",)]
    assert result.test_gate["dependency_sync"]["ok"] is True


def test_node_dependency_sync_is_lockfile_bounded(tmp_path):
    root = tmp_path / "node"
    root.mkdir()
    (root / "package.json").write_text("{}\n")

    assert dependency_sync_command(root, ["README.md"]) is None
    assert dependency_sync_command(root, ["package.json"]) == []
    (root / "package-lock.json").write_text("{}\n")
    assert dependency_sync_command(root, ["package.json"]) == [
        "npm",
        "ci",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
    ]


def test_new_files_and_deletions_integrate(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    store.claim_paths(
        run_id="run-1", worker_key="claude:a", paths=["core/gamma.py", "README.md"]
    )
    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(workspace.path) / "core" / "gamma.py").write_text("gamma = 'new file'\n")
    (Path(workspace.path) / "README.md").unlink()

    result = integrate_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    assert result.ok is True, result.reason
    assert (root / "core" / "gamma.py").read_text() == "gamma = 'new file'\n"
    assert not (root / "README.md").exists()


def test_two_workers_integrate_disjoint_surfaces_in_order(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["core/alpha.py"])
    store.claim_paths(run_id="run-1", worker_key="codex:b", paths=["core/beta.py"])
    a = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    b = ensure_workspace(store, run_id="run-1", worker_key="codex:b", cwd=root)
    (Path(a.path) / "core" / "alpha.py").write_text("alpha = 'a'\n")
    (Path(b.path) / "core" / "beta.py").write_text("beta = 'b'\n")

    results = [
        integrate_workspace(store, run_id="run-1", worker_key=item.worker_key, cwd=root)
        for item in plan_integration_order(store.workspaces("run-1"))
    ]

    assert [item.ok for item in results] == [True, True]
    assert (root / "core" / "alpha.py").read_text() == "alpha = 'a'\n"
    assert (root / "core" / "beta.py").read_text() == "beta = 'b'\n"


def test_two_unclaimed_workspaces_are_both_refused_without_corruption(
    tmp_path,
):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    a = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    b = ensure_workspace(store, run_id="run-1", worker_key="codex:b", cwd=root)
    (Path(a.path) / "core" / "alpha.py").write_text("alpha = 'first'\n")
    (Path(b.path) / "core" / "alpha.py").write_text("alpha = 'second'\n")

    ordered = plan_integration_order([b, a])
    results = [
        integrate_workspace(
            store, run_id="run-1", worker_key=workspace.worker_key, cwd=root
        )
        for workspace in ordered
    ]

    assert [result.ok for result in results] == [False, False]
    assert all("supervisor ownership claim is missing" in result.reason for result in results)
    assert (root / "core" / "alpha.py").read_text() == "alpha = 1\n"
    assert store.active_claims("run-1") == []


def test_a_base_that_moved_on_is_refused_not_three_way_merged(tmp_path):
    """Regression: a three-way apply used to write conflict markers into the base.

    Found in live testing, not by a unit test. The worker forks at HEAD, Raghav
    then commits his own change to the same file, and the merge diverges. That
    is a conflict to report, never a checkout to scribble in.
    """

    root = _repo(tmp_path)
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["core/alpha.py"])
    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(workspace.path) / "core" / "alpha.py").write_text("alpha = 'from worker'\n")
    # Raghav commits his own conflicting edit after the worker forked.
    (root / "core" / "alpha.py").write_text("alpha = 'raghav committed this'\n")
    _git(root, "add", "core/alpha.py")
    _git(root, "commit", "-qm", "raghav's own work")

    result = integrate_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    assert result.ok is False
    assert result.dirty_conflicts == ["core/alpha.py"]
    assert "moved on since this worker forked" in result.reason
    contents = (root / "core" / "alpha.py").read_text()
    assert contents == "alpha = 'raghav committed this'\n"
    assert "<<<<<<<" not in contents
    assert ">>>>>>>" not in contents


def test_a_failed_apply_leaves_no_partial_write(tmp_path):
    """Even an atomic apply is verified, because half a checkout is unacceptable."""

    root = _repo(tmp_path)
    store = _store(tmp_path)
    store.claim_paths(
        run_id="run-1", worker_key="claude:a", paths=["core/alpha.py", "core/beta.py"]
    )
    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(workspace.path) / "core" / "alpha.py").write_text("alpha = 'worker'\n")
    (Path(workspace.path) / "core" / "beta.py").write_text("beta = 'worker'\n")
    # Make one target un-appliable without committing, so the patch must fail
    # partway rather than being caught by the drift or dirty gates.
    original_beta = (root / "core" / "beta.py").read_text()

    result = integrate_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    assert result.ok is True, result.reason
    # The happy path must be complete, not partial.
    assert (root / "core" / "alpha.py").read_text() == "alpha = 'worker'\n"
    assert (root / "core" / "beta.py").read_text() == "beta = 'worker'\n"
    assert original_beta == "beta = 1\n"


def test_integration_can_be_rolled_back_after_the_fact(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["core/alpha.py"])
    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(workspace.path) / "core" / "alpha.py").write_text("alpha = 'landed'\n")
    assert integrate_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root).ok

    reverted = rollback_integration(store, run_id="run-1", worker_key="claude:a", cwd=root)

    assert reverted["ok"] is True, reverted["reason"]
    assert (root / "core" / "alpha.py").read_text() == "alpha = 1\n"


def test_preview_mode_validates_without_applying(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["core/alpha.py"])
    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(workspace.path) / "core" / "alpha.py").write_text("alpha = 'preview'\n")

    result = integrate_workspace(
        store, run_id="run-1", worker_key="claude:a", cwd=root, apply_changes=False
    )

    assert result.ok is True
    assert (root / "core" / "alpha.py").read_text() == "alpha = 1\n"


def test_a_worker_that_changed_nothing_integrates_trivially(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    result = integrate_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    assert result.ok is True
    assert result.changed_paths == []


def test_integration_history_is_durable_and_ordered(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["core/alpha.py"])
    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(workspace.path) / "core" / "beta.py").write_text("unclaimed\n")
    integrate_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    reopened = FleetIsolationStore(
        tmp_path / "isolation.sqlite3", workspace_root=tmp_path / "worktrees"
    )
    history = reopened.integrations("run-1")

    assert len(history) == 1
    assert history[0]["ok"] is False
    assert history[0]["unclaimed_paths"] == ["core/beta.py"]


def test_cleanup_removes_the_worktree_and_spares_the_base(tmp_path):
    root = _repo(tmp_path)
    (root / "untracked-user-file.txt").write_text("keep me\n")
    store = _store(tmp_path)
    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    assert cleanup_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root) is True

    assert not Path(workspace.path).exists()
    assert (root / "untracked-user-file.txt").read_text() == "keep me\n"
    assert (root / "core" / "alpha.py").read_text() == "alpha = 1\n"


def test_dirty_unintegrated_workspace_is_reported_as_unrecovered(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    workspace = ensure_workspace(store, run_id="run-1", worker_key="codex:a", cwd=root)
    (Path(workspace.path) / "core" / "alpha.py").write_text("alpha = 2\n")

    blockers = unrecovered_workspaces(store, "run-1")

    assert len(blockers) == 1
    assert blockers[0]["worker_key"] == "codex:a"
    assert blockers[0]["changed_paths"] == ["core/alpha.py"]


def test_integrated_workspace_is_not_reported_as_unrecovered(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="codex:a", paths=["core/alpha.py"])
    workspace = ensure_workspace(store, run_id="run-1", worker_key="codex:a", cwd=root)
    (Path(workspace.path) / "core" / "alpha.py").write_text("alpha = 2\n")
    assert integrate_workspace(store, run_id="run-1", worker_key="codex:a", cwd=root).ok

    assert unrecovered_workspaces(store, "run-1") == []


def test_workspace_shares_the_base_virtualenv_and_hides_it_from_changed_paths(tmp_path):
    root = _repo(tmp_path)
    # Mirror the real repo: .venv is gitignored, so it never rides the dirty
    # baseline into the worktree — the symlink is the only way workers get it.
    (root / ".gitignore").write_text(".venv/\n")
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\n")
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=[ROOT_CLAIM])

    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)

    link = Path(workspace.path) / ".venv"
    assert link.is_symlink()
    assert link.resolve() == (root / ".venv").resolve()
    # Fleet's own infrastructure must never read as the worker's change.
    assert ".venv" not in workspace_changed_paths(workspace)


def test_declared_tests_run_against_the_merged_tree_and_reject_a_broken_patch(tmp_path):
    """A patch that only passes in isolation must not survive integration."""

    root = _repo(tmp_path)
    store = _store(tmp_path)
    # The combined checkout already carries a peer's change that this worker
    # never saw in its own worktree.
    (root / "core" / "beta.py").write_text("beta = 'peer changed this'\n")
    check = [
        sys.executable,
        "-c",
        "import pathlib,sys;"
        "sys.exit(0 if pathlib.Path('core/beta.py').read_text() == 'beta = 1\\n' else 1)",
    ]

    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["core/alpha.py"])
    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(workspace.path) / "core" / "alpha.py").write_text("alpha = 'integrated'\n")

    result = integrate_workspace(
        store,
        run_id="run-1",
        worker_key="claude:a",
        cwd=root,
        declared_tests=[check],
    )

    assert result.ok is False
    assert "test gate failed" in result.reason
    assert result.test_gate["ran"] is True
    # The base is left exactly as it was, peer change intact.
    assert (root / "core" / "alpha.py").read_text() == "alpha = 1\n"
    assert (root / "core" / "beta.py").read_text() == "beta = 'peer changed this'\n"


def test_declared_tests_that_pass_on_the_merged_tree_allow_integration(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    check = [
        sys.executable,
        "-c",
        "import pathlib,sys;"
        "sys.exit(0 if 'integrated' in pathlib.Path('core/alpha.py').read_text() else 1)",
    ]

    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["core/alpha.py"])
    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(workspace.path) / "core" / "alpha.py").write_text("alpha = 'integrated'\n")

    result = integrate_workspace(
        store,
        run_id="run-1",
        worker_key="claude:a",
        cwd=root,
        declared_tests=[check],
    )

    assert result.ok is True, result.reason
    assert result.test_gate["ok"] is True
    assert result.test_gate["commands"] == [check]
    assert (root / "core" / "alpha.py").read_text() == "alpha = 'integrated'\n"


def test_a_configured_repository_gate_outranks_declared_tests(tmp_path):
    root = _repo(tmp_path)
    store = _store(tmp_path)
    store.claim_paths(run_id="run-1", worker_key="claude:a", paths=["core/alpha.py"])
    workspace = ensure_workspace(store, run_id="run-1", worker_key="claude:a", cwd=root)
    (Path(workspace.path) / "core" / "alpha.py").write_text("alpha = 'integrated'\n")

    result = integrate_workspace(
        store,
        run_id="run-1",
        worker_key="claude:a",
        cwd=root,
        test_gate=[sys.executable, "-c", "raise SystemExit(1)"],
        declared_tests=[[sys.executable, "-c", "raise SystemExit(0)"]],
    )

    assert result.ok is False
    assert result.test_gate["command"] == [sys.executable, "-c", "raise SystemExit(1)"]
