"""Cross-platform guards for the Windows frozen-workspace release proof."""

from importlib import util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify-workspace-frozen-windows.py"


def _proof_module():
    spec = util.spec_from_file_location("workspace_frozen_windows_proof", SCRIPT)
    assert spec and spec.loader
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cleanup_retries_a_transient_windows_directory_lock(tmp_path, monkeypatch):
    proof = _proof_module()
    directory = tmp_path / "proof"
    directory.mkdir()
    (directory / "receipt.txt").write_text("done", encoding="utf-8")
    real_rmtree = proof.shutil.rmtree
    calls = []

    def transient_lock(path):
        calls.append(path)
        if len(calls) < 3:
            raise PermissionError("process cwd is still retiring")
        real_rmtree(path)

    monkeypatch.setattr(proof.shutil, "rmtree", transient_lock)
    proof._remove_directory(directory, attempts=3, delay=0)

    assert calls == [directory, directory, directory]
    assert not directory.exists()


def test_cleanup_exhaustion_is_reported_instead_of_hidden(tmp_path, monkeypatch):
    proof = _proof_module()
    directory = tmp_path / "proof"
    directory.mkdir()
    monkeypatch.setattr(
        proof.shutil,
        "rmtree",
        lambda _path: (_ for _ in ()).throw(PermissionError("still locked")),
    )

    with pytest.raises(PermissionError, match="still locked"):
        proof._remove_directory(directory, attempts=2, delay=0)


def test_probe_closes_popen_handles_before_directory_cleanup():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "with subprocess.Popen(" in source
    assert source.index("with subprocess.Popen(") < source.index("_remove_directory(directory)")
