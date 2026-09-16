import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from fleet.worker_runtime import preflight, prepare_runtime, read_sandbox_flags, runtime_directory
from fleet.workers import WorkerRequest, _worker_environment, worker_command


@pytest.fixture
def runtime_request(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENA_FLEET_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SERENA_FLEET_READ_MCP_SERVERS", "none")
    cwd = tmp_path / "repo"
    cwd.mkdir()
    return WorkerRequest(run_id="run", leg_id="leg", attempt_id="attempt", task="test",
                         activity="coding", phase="execute", role="implementer", provider="codex",
                         model="gpt-6-astra", effort="medium", access_mode="write", cwd=str(cwd), prompt="test")


def test_runtime_is_private_per_attempt_and_overrides_operator_databases(runtime_request, monkeypatch):
    # This checks command construction, not a native provider launch. CI must
    # not need a locally installed/authenticated Codex CLI for that assertion.
    monkeypatch.setattr("fleet.workers._binary", lambda provider: f"/test/{provider}")
    monkeypatch.setenv("SERENA_CONTROL_PLANE_DB_PATH", "/operator/control.sqlite3")
    monkeypatch.setenv("SERENA_NOTIFICATION_DB_PATH", "/operator/notifications.sqlite3")
    env = _worker_environment(runtime_request)
    root = runtime_directory(runtime_request)
    assert Path(env["TMPDIR"]).parent == root
    assert Path(env["SERENA_CONTROL_PLANE_DB_PATH"]).parent == root
    assert Path(env["SERENA_NOTIFICATION_DB_PATH"]).parent == root
    assert runtime_directory(replace(runtime_request, attempt_id="second")) != root
    assert preflight(runtime_request)["passed"]
    assert list(Path(env["TMPDIR"]).iterdir()) == []
    command = worker_command(runtime_request)
    assert command[command.index("--add-dir") + 1] == str(root)


def test_preflight_failure_precedes_provider_dispatch(runtime_request, monkeypatch):
    def refused(*args, **kwargs):
        raise PermissionError("scratch unavailable")
    monkeypatch.setattr("fleet.worker_runtime.tempfile.TemporaryDirectory", refused)
    with pytest.raises(PermissionError, match="scratch unavailable"):
        preflight(runtime_request)


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("codex"), reason="native Codex Linux sandbox required")
@pytest.mark.parametrize("access_mode", ["write", "read_only", "review"])
def test_native_sandbox_can_write_private_test_databases_but_not_git_metadata(runtime_request, access_mode):
    runtime_request = replace(runtime_request, access_mode=access_mode)
    root = Path(runtime_request.cwd)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    env = dict(os.environ, **prepare_runtime(runtime_request))
    script = """
import os, sqlite3, tempfile, errno
from pathlib import Path
with tempfile.TemporaryFile() as f:
    f.write(b'private scratch')
db = sqlite3.connect(os.environ['SERENA_CONTROL_PLANE_DB_PATH'])
db.execute('CREATE TABLE probe(value INTEGER)')
db.close()
try:
    Path('.git/fleet-probe').write_text('must be denied')
except OSError as exc:
    assert exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS), exc
else:
    raise AssertionError('shared Git metadata unexpectedly writable')
print('private scratch and database writable; Git metadata protected')
"""
    if access_mode != "write":
        script += "\ntry:\n    Path('repository-write').write_text('denied')\nexcept OSError:\n    pass\nelse:\n    raise AssertionError('read-only repository writable')\n"
        flags = read_sandbox_flags(runtime_request)
    else:
        flags = ["-c", 'sandbox_mode="workspace-write"',
               "-c", "sandbox_workspace_write.writable_roots=" + json.dumps([str(runtime_directory(runtime_request))]),
        ]
    command = [shutil.which("codex"), "sandbox", *flags, "--", sys.executable, "-c", script]
    result = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
