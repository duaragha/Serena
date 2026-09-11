"""Service runtime selection must not retain an obsolete versioned Node PATH."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux systemd launcher")
ROOT = Path(__file__).resolve().parents[1]


def _fixture(tmp_path):
    script = tmp_path / "scripts/serena-fleet-service.sh"
    script.parent.mkdir()
    shutil.copyfile(ROOT / "scripts/serena-fleet-service.sh", script)
    cli = tmp_path / ".venv/bin/chats"
    cli.parent.mkdir(parents=True)
    cli.write_text('#!/bin/bash\nprintf "cli:%s\\n" "$*"\nnode --version\n')
    cli.chmod(0o755)
    default = tmp_path / "selected/bin"
    default.mkdir(parents=True)
    node = default / "node"
    node.write_text('#!/bin/bash\necho v24.15.0\n')
    node.chmod(0o755)
    nvm = tmp_path / "nvm"
    nvm.mkdir()
    (nvm / "nvm.sh").write_text(
        '[[ "$1" == "--no-use" ]] || return 1\n'
        'nvm() { [[ "$*" == "use --silent default" ]] || return 1; '
        f'export PATH="{default}:$PATH"; }}\n'
    )
    env = dict(os.environ, NVM_DIR=str(nvm), PATH="/usr/bin:/bin")
    return script, nvm, env


def test_service_exec_uses_configured_default_node(tmp_path):
    script, _, env = _fixture(tmp_path)
    result = subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["cli:fleet serve", "v24.15.0"]


def test_runtime_check_never_starts_fleet(tmp_path):
    script, _, env = _fixture(tmp_path)
    result = subprocess.run(["bash", str(script), "--check"], env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "v24.15.0" in result.stdout
    assert "cli:fleet serve" not in result.stdout


def test_missing_configured_default_refuses_instead_of_using_wrong_node(tmp_path):
    script, nvm, env = _fixture(tmp_path)
    (nvm / "nvm.sh").write_text('nvm() { return 3; }\n')
    result = subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode != 0
    assert "configured NVM default is unavailable" in result.stderr
    assert "cli:fleet serve" not in result.stdout


def test_without_nvm_preserves_operator_path(tmp_path):
    script, nvm, env = _fixture(tmp_path)
    env["NVM_DIR"] = str(nvm / "not-installed")
    env["PATH"] = str(tmp_path / "selected/bin") + ":/usr/bin:/bin"
    result = subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "v24.15.0" in result.stdout


def test_unit_has_no_versioned_node_pin_and_keeps_security_contract():
    unit = (ROOT / "systemd/serena-fleet.service").read_text()
    assert ".nvm/versions/node/" not in unit
    assert "ExecStart=%h/Documents/Projects/serena/scripts/serena-fleet-service.sh" in unit
    assert "Type=simple" in unit
    assert "UnsetEnvironment=OPENAI_API_KEY" in unit
    assert "KillMode=control-group" in unit
