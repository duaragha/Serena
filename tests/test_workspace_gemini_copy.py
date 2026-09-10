"""The offline migration probe must reject unsafe sources before vendor import."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify-workspace-gemini-copy.py"
SID = "11111111-2222-4333-8444-555555555555"


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_rejects_sqlite_sidecars_without_touching_source(tmp_path, suffix):
    source = tmp_path / f"{SID}.db"
    source.write_bytes(b"original")
    sidecar = Path(str(source) + suffix)
    sidecar.write_bytes(b"active")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "missing.par"), str(source)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 1
    assert "inactive conversation" in result.stderr
    assert source.read_bytes() == b"original"
    assert sidecar.read_bytes() == b"active"


def test_rejects_symlink_before_vendor_import(tmp_path):
    original = tmp_path / "original.db"
    original.write_bytes(b"original")
    source = tmp_path / f"{SID}.db"
    try:
        source.symlink_to(original)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "missing.par"), str(source)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 1
    assert "must not contain symlinks" in result.stderr
    assert original.read_bytes() == b"original"
