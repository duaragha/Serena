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
