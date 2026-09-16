"""Release edition contracts exercised in fresh backend processes."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def edition_env(home, version):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the desktop environment contract")
    result = subprocess.run(
        [node, "-e", "const p=require('./apps/desktop/profile');"
         "console.log(JSON.stringify(p.backendEnvironment(p.desktopProfile(process.argv[1]), process.argv[2])))",
         version, str(home)], cwd=ROOT, capture_output=True, check=True, text=True,
    )
    return {**os.environ, **json.loads(result.stdout), "HOME": str(home),
            "USERPROFILE": str(home), "PYTHONPATH": str(ROOT)}


@pytest.mark.parametrize("version,structured,title", [
    ("0.3.0", False, "Serena"), ("0.3.0-dev.1", True, "Serena Dev"),
])
def test_fresh_backend_mounts_only_its_editions_view(tmp_path, version, structured, title):
    result = subprocess.run([sys.executable, "-c", """
import json
from ui.web import app, _UI_STATE_PATH
from core.config import DATA_DIR
with app.test_client() as client:
    health = client.get('/api/health').get_json()
    html = client.get('/').get_data(as_text=True)
print(json.dumps({'health': health, 'html': html, 'ui': str(_UI_STATE_PATH),
                  'data': str(DATA_DIR), 'routes': [str(r) for r in app.url_map.iter_rules()]}))
"""], cwd=ROOT, env=edition_env(tmp_path, version), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout.strip().splitlines()[-1])
    assert proof["health"]["desktop"] == {"version": version, "channel": "dev" if structured else "stable"}
    assert proof["health"]["capabilities"]["structuredWorkspace"] == int(structured)
    assert f'"structuredWorkspace": {str(structured).lower()}' in proof["html"]
    assert f"<title>{title}</title>" in proof["html"]
    assert any(route.startswith("/workspace/") for route in proof["routes"]) is structured
    assert proof["data"].endswith("chats-dev" if structured else "chats")
    assert Path(proof["ui"]).parent.name == ("serena-dev" if structured else "serena")


def test_quit_closes_only_the_hosts_own_providers_and_terminals(monkeypatch):
    from ui import web
    calls = []

    class Owner:
        def shutdown(self):
            calls.append("structured")

    monkeypatch.setitem(web.app.extensions, "workspace_host", Owner())
    monkeypatch.setattr(web.pty_terminal, "shutdown_all", lambda: calls.append("pty"))
    web._shutdown_owned_runtimes()
    assert calls == ["structured", "pty"]


def test_pty_shutdown_still_runs_if_structured_cleanup_fails(monkeypatch):
    from ui import web
    calls = []

    class Owner:
        def shutdown(self):
            raise RuntimeError("provider refused shutdown")

    monkeypatch.setitem(web.app.extensions, "workspace_host", Owner())
    monkeypatch.setattr(web.pty_terminal, "shutdown_all", lambda: calls.append("pty"))
    with pytest.raises(RuntimeError, match="provider refused"):
        web._shutdown_owned_runtimes()
    assert calls == ["pty"]


def test_dev_cannot_take_a_session_held_by_stable(tmp_path):
    stable = subprocess.Popen([sys.executable, "-u", "-c", """
import sys
from core.workspace_lease import SessionLease
lease = SessionLease('shared-native-chat')
print('held', flush=True)
sys.stdin.readline()
lease.release()
"""], cwd=ROOT, env=edition_env(tmp_path, "0.3.0"), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert stable.stdout.readline().strip() == "held"
        attempt = subprocess.run([sys.executable, "-c", """
from core.workspace_lease import SessionLease, SessionOwnedError
try:
    lease = SessionLease('shared-native-chat')
except SessionOwnedError:
    print('refused')
else:
    lease.release()
    raise SystemExit('duplicate writer admitted')
"""], cwd=ROOT, env=edition_env(tmp_path, "0.3.0-dev.1"), capture_output=True, text=True, timeout=15)
        assert attempt.returncode == 0, attempt.stderr
        assert attempt.stdout.strip() == "refused"
        assert stable.poll() is None
    finally:
        stable.communicate("close\n", timeout=10)
