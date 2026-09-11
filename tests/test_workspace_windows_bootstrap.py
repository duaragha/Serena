"""Windows startup gate and packaged entrypoint dispatch, with no providers."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows bootstrap entrypoint")


@pytest.mark.parametrize("entry", ["core/workspace_windows_bootstrap.py", "apps/desktop/windows/sidecar-win.py"])
@pytest.mark.parametrize("gate", [b"", b"{}\n"])
def test_no_command_before_valid_gate(entry, gate, tmp_path):
    executable = getattr(sys, "_base_executable", sys.executable)
    command = [executable, str(ROOT / entry)]
    if "sidecar" in entry:
        command.append("--workspace-child")
    result = subprocess.run(command, input=gate, capture_output=True, cwd=tmp_path, timeout=10)
    assert result.returncode != 0
    assert not result.stdout
    assert "Flask" not in result.stderr.decode(errors="replace")


def test_gate_preserves_subsequent_input(tmp_path):
    executable = getattr(sys, "_base_executable", sys.executable)
    child = [executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"]
    payload = b'{"id":1,"method":"initialize"}\n'
    result = subprocess.run(
        [executable, str(ROOT / "core/workspace_windows_bootstrap.py")],
        input=json.dumps(child).encode() + b"\n" + payload,
        capture_output=True, cwd=tmp_path, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == payload
