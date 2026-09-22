"""Windows Git modes must not turn unchanged executable files into conflicts."""

import os
import subprocess
from types import SimpleNamespace

import pytest
from test_fleet_isolation import _git, _repo, _store

from fleet import isolation


@pytest.fixture
def executable_repo(tmp_path):
    root = _repo(tmp_path)
    _git(root, "config", "core.filemode", "false")
    _git(root, "update-index", "--chmod=+x", "core/alpha.py")
    _git(root, "commit", "-qm", "record executable flag")
    return root


def windows_modes(monkeypatch):
    # Replace this module's OS view; changing os.name globally breaks pathlib
    # and subprocess when this regression also runs on a POSIX CI host.
    monkeypatch.setattr(isolation, "os", SimpleNamespace(name="nt", readlink=os.readlink))


def test_windows_unchanged_executable_accepts_content_and_preserves_mode(executable_repo, monkeypatch):
    root = executable_repo
    windows_modes(monkeypatch)
    baseline = isolation._baseline_path_entry(root, "HEAD", "core/alpha.py")
    assert baseline[0] == "100755"
    assert isolation._filesystem_path_entry(root, "core/alpha.py") == baseline
    (root / "core/alpha.py").write_bytes(b"changed = True\n")
    assert isolation._filesystem_path_entry(root, "core/alpha.py") != baseline


def test_windows_explicit_index_mode_change_remains_drift(executable_repo, monkeypatch):
    root = executable_repo
    windows_modes(monkeypatch)
    baseline = isolation._baseline_path_entry(root, "HEAD", "core/alpha.py")
    _git(root, "update-index", "--chmod=-x", "core/alpha.py")
    assert isolation._filesystem_path_entry(root, "core/alpha.py")[0] == "100644"
    assert isolation._filesystem_path_entry(root, "core/alpha.py") != baseline


def test_windows_unknown_index_mode_fails_closed(executable_repo, monkeypatch):
    windows_modes(monkeypatch)
    monkeypatch.setattr(isolation, "_git", lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", b"error"))
    with pytest.raises(isolation.IsolationError, match="live Git mode"):
        isolation._filesystem_path_entry(executable_repo, "core/alpha.py")


def test_windows_executable_integrates_with_real_gate(executable_repo, tmp_path, monkeypatch):
    root = executable_repo
    store = _store(tmp_path)
    workspace = isolation.ensure_workspace(store, run_id="windows-mode", worker_key="agent:a", cwd=root)
    store.claim_paths(run_id="windows-mode", worker_key="agent:a", paths=["core/alpha.py"])
    import sys
    from pathlib import Path
    (Path(workspace.path) / "core/alpha.py").write_bytes(b"alpha = 2\n")
    # Keep the full module OS API for integration, overriding only the name.
    monkeypatch.setattr(isolation, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    result = isolation.integrate_workspace(store, run_id="windows-mode", worker_key="agent:a", cwd=root,
        test_gate=[sys.executable, "-c", "from pathlib import Path; assert Path('core/alpha.py').read_text() == 'alpha = 2\\n'"],
        apply_changes=True)
    assert result.ok, result.reason
    assert result.test_gate["ran"] and result.test_gate["ok"]
    assert _git(root, "ls-files", "--stage", "core/alpha.py").startswith("100755")
