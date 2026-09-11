import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROOF = ROOT / "scripts" / "verify-workspace-mount.py"


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        (None, "mode=default enabled=True"),
        ("--enabled", "mode=explicit-on enabled=True"),
        ("--disabled", "mode=explicit-off enabled=False"),
    ],
)
def test_structured_workspace_mount_contract(argument, expected):
    command = [sys.executable, str(PROOF)]
    if argument:
        command.append(argument)
    env = os.environ.copy()
    env.pop("SERENA_STRUCTURED_WORKSPACE", None)

    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert expected in result.stdout
    assert "no owner loop or provider process started" in result.stdout
