"""Actual Git application/rollback preserves LF, CRLF and binary payloads."""

from pathlib import Path

import pytest

from fleet.isolation import ensure_workspace, integrate_workspace, rollback_integration
from test_fleet_isolation import _git, _repo, _store


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
@pytest.mark.parametrize("binary", [False, True])
def test_apply_and_rollback_preserve_exact_patch_bytes(tmp_path, newline, binary):
    root = _repo(tmp_path)
    _git(root, "config", "core.whitespace", "cr-at-eol")
    prefix = b"\x00\xff" if binary else b""
    before, after = prefix + b"before" + newline, prefix + b"after" + newline
    (root / "core/alpha.py").write_bytes(before)
    _git(root, "add", "core/alpha.py")
    _git(root, "commit", "-qm", "exact byte baseline")
    store = _store(tmp_path)
    store.claim_paths(run_id="bytes", worker_key="agent:a", paths=["core/alpha.py"])
    workspace = ensure_workspace(store, run_id="bytes", worker_key="agent:a", cwd=root)
    (Path(workspace.path) / "core/alpha.py").write_bytes(after)
    result = integrate_workspace(store, run_id="bytes", worker_key="agent:a", cwd=root)
    assert result.ok, result.reason
    assert (root / "core/alpha.py").read_bytes() == after
    rolled_back = rollback_integration(store, run_id="bytes", worker_key="agent:a", cwd=root)
    assert rolled_back["ok"], rolled_back
    assert (root / "core/alpha.py").read_bytes() == before


@pytest.mark.parametrize("checkout_crlf", [False, True])
def test_autocrlf_integration_and_recovery_preserve_real_checkout_bytes(tmp_path, checkout_crlf):
    root = _repo(tmp_path)
    _git(root, "config", "core.autocrlf", "true")
    before = b"alpha = 1\r\n" if checkout_crlf else b"alpha = 1\n"
    (root / "core/alpha.py").write_bytes(before)
    store = _store(tmp_path)
    store.claim_paths(run_id="eol", worker_key="agent:a", paths=["core/alpha.py"])
    workspace = ensure_workspace(store, run_id="eol", worker_key="agent:a", cwd=root)
    (Path(workspace.path) / "core/alpha.py").write_bytes(b"alpha = 2\n")
    result = integrate_workspace(store, run_id="eol", worker_key="agent:a", cwd=root)
    assert result.ok, result.reason
    assert (root / "core/alpha.py").read_bytes() == b"alpha = 2\r\n"
    # Recovered postimages still pass real gates and restore the original raw
    # preimage on failure, including a user's unnormalized LF checkout.
    import sys
    result = integrate_workspace(store, run_id="eol", worker_key="agent:a", cwd=root,
                                 test_gate=[sys.executable, "-c", "raise SystemExit(7)"])
    assert not result.ok and result.test_gate["exit_code"] == 7
    assert (root / "core/alpha.py").read_bytes() == before


def test_autocrlf_never_accepts_foreign_edits_and_ignores_empty_normalized_delta(tmp_path):
    from fleet.isolation import workspace_changed_paths
    root = _repo(tmp_path)
    _git(root, "config", "core.autocrlf", "true")
    store = _store(tmp_path)
    store.claim_paths(run_id="eol", worker_key="agent:a", paths=["core/alpha.py"])
    workspace = ensure_workspace(store, run_id="eol", worker_key="agent:a", cwd=root)
    worker = Path(workspace.path) / "core/alpha.py"
    worker.write_bytes(b"alpha = 1\n")
    assert workspace_changed_paths(workspace) == []
    worker.write_bytes(b"alpha = 2\n")
    (root / "core/alpha.py").write_bytes(b"foreign = True\r\n")
    result = integrate_workspace(store, run_id="eol", worker_key="agent:a", cwd=root)
    assert not result.ok and "diverged" in result.reason
    assert (root / "core/alpha.py").read_bytes() == b"foreign = True\r\n"
