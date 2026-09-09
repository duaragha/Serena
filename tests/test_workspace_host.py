import asyncio
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from flask import Flask

from core.workspace_codex import CodexWorkspace
from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from core.workspace_rpc import WorkspaceRpc
from ui.workspace_web import workspace_blueprint


class Owner:
    instances = []

    def __init__(self, *, session_id, cwd, publish):
        self.sid, self.publish = session_id, publish
        self.state, self.active_turn = "opening", None
        self.sent, self.closed = [], False
        self.instances.append(self)

    async def open(self):
        await asyncio.sleep(0.04)
        self.state = "ready"
        await self.publish(
            {"method": "workspace/history", "params": {"thread": {"id": self.sid, "turns": []}}}
        )

    async def submit(self, inputs, options=None):
        self.sent.append(inputs)
        await asyncio.sleep(0.04)
        await self.publish(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": self.sid,
                    "turnId": "t",
                    "itemId": "a",
                    "delta": "adapter fixture output",
                },
            }
        )
        return {"turn": {"id": "turn-1"}}

    async def close(self):
        self.closed = True


@pytest.fixture
def host(tmp_path):
    Owner.instances = []
    value = WorkspaceHost(
        journal=WorkspaceJournal(tmp_path / "events.db"),
        resolve=lambda sid: {"session_id": sid, "provider": "codex", "cwd": str(tmp_path)},
        factories={"codex": Owner},
    )
    yield value
    value.shutdown()


def test_polling_never_launches_and_concurrent_attach_has_one_owner(host):
    assert host.events("exact")["events"] == []
    assert host._loop is None
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: host.attach("exact"), range(6)))
    assert all(result["ok"] for result in results)
    assert len(Owner.instances) == 1
    assert len(host.events("exact")["events"]) == 1
    assert not Owner.instances[0].closed


def test_timeout_retry_does_not_cancel_or_send_again(host):
    assert host.attach("exact", timeout=0.001)["pending"]
    assert host.attach("exact")["ok"]
    payload = {"inputs": [{"type": "text", "text": "once"}]}
    assert host.command("exact", "stable", "submit", payload, timeout=0.001)["pending"]
    receipt = host.command("exact", "stable", "submit", payload)
    assert receipt["ok"]
    assert host.command("exact", "stable", "submit", payload) == receipt
    assert len(Owner.instances[0].sent) == 1
    with pytest.raises(ValueError, match="different content"):
        host.command("exact", "stable", "submit", {"inputs": []})
    assert host.events("other")["events"] == []


def test_crash_ambiguous_command_is_not_repeated(host):
    host.attach("exact")
    payload = {"inputs": [{"type": "text", "text": "once"}]}
    host.journal.claim_command("exact", "uncertain", {"action": "submit", "payload": payload})
    receipt = host.command("exact", "uncertain", "submit", payload)
    assert receipt["uncertain"]
    assert Owner.instances[0].sent == []


def test_explicit_shutdown_waits_for_admitted_attach_and_is_idempotent(host):
    assert host.attach("exact", timeout=0.001)["pending"]
    host.shutdown()
    assert len(Owner.instances) == 1
    assert Owner.instances[0].closed
    host.shutdown()
    with pytest.raises(RuntimeError, match="stopped"):
        host.attach("exact")


def test_resolver_mismatch_and_unattached_controls_never_spawn(host):
    host.resolve = lambda sid: {"session_id": "wrong", "provider": "codex"}
    with pytest.raises(ValueError, match="different session"):
        host.attach("exact")
    with pytest.raises(ValueError, match="attach"):
        host.command("exact", "r", "interrupt", {})
    assert Owner.instances == []


def test_http_authentication_replay_and_disconnect_are_non_cancelling(host):
    app = Flask(__name__)
    token = "s" * 40
    app.register_blueprint(workspace_blueprint(host, token=token))
    headers = {"X-Serena-Workspace-Token": token}
    with app.test_client() as client:
        assert (
            client.post("/api/workspace/exact/attach", base_url="http://127.0.0.1").status_code
            == 403
        )
        assert (
            client.post(
                "/api/workspace/exact/attach", base_url="http://evil.test", headers=headers
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/workspace/exact/attach",
                base_url="http://127.0.0.1",
                headers={**headers, "Origin": "https://evil.test"},
            ).status_code
            == 403
        )
        kwargs = {"base_url": "http://127.0.0.1", "headers": headers}
        assert client.get("/api/workspace/exact/events", **kwargs).json["events"] == []
        assert Owner.instances == []
        assert client.post("/api/workspace/exact/attach", **kwargs).json["ok"]
        data = {
            "request_id": "once",
            "action": "submit",
            "payload": {"inputs": [{"type": "text", "text": "hi"}]},
        }
        first = client.post("/api/workspace/exact/commands", json=data, **kwargs)
        assert first.json["ok"]
        assert client.post("/api/workspace/exact/commands", json=data, **kwargs).json == first.json
    assert not Owner.instances[0].closed
    with app.test_client() as reconnected:
        assert len(reconnected.get("/api/workspace/exact/events", **kwargs).json["events"]) == 2
    assert len(Owner.instances) == 1


def test_browser_composer_reaches_host_and_reloads_same_owner(host):
    playwright = pytest.importorskip("playwright.sync_api")
    from werkzeug.serving import make_server

    app = Flask(__name__, static_folder=str(Path(__file__).resolve().parents[1] / "ui/static"))
    app.register_blueprint(workspace_blueprint(host, token="s" * 40))

    @app.get("/")
    def page():
        return """<!doctype html><html><head><link rel="stylesheet" href="/static/workspace-pane.css"></head>
<body style="margin:0;background:#000"><button id="connect">Open session</button><main style="height:90vh" id="pane"></main>
<script type="module">
import {WorkspacePane} from '/static/workspace-pane.mjs';
import {WorkspaceConnection} from '/static/workspace-connection.mjs';
window.connection=new WorkspaceConnection({sessionId:'exact',token:'ssssssssssssssssssssssssssssssssssssssss',receive:e=>pane.receive(e),error:e=>pane.error(e)});
window.pane=new WorkspacePane(document.querySelector('#pane'),{sessionId:'exact',provider:'Codex',controls:connection.controls()});
document.querySelector('#connect').onclick=()=>connection.connect().catch(e=>pane.error(e));
window.addEventListener('pagehide',()=>connection.dispose());
</script></body></html>"""

    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1200, "height": 800})
            page.set_default_timeout(5000)
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{server.server_port}")
            page.wait_for_function("window.connection !== undefined")
            assert Owner.instances == []
            page.get_by_role("button", name="Open session").click()
            page.wait_for_function("pane.conversation.status === 'ready' || !pane.alert.hidden")
            assert page.locator(".aw-error").is_hidden(), page.locator(".aw-error").inner_text()
            page.wait_for_function("pane.conversation.status === 'ready'")
            page.get_by_role("textbox", name="Message Codex").fill("through the actual HTTP host")
            page.get_by_role("button", name="Send message", exact=True).click()
            page.get_by_text("adapter fixture output", exact=True).wait_for()
            assert Owner.instances[0].sent == [
                [{"type": "text", "text": "through the actual HTTP host"}]
            ]
            page.reload()
            page.get_by_role("button", name="Open session").click()
            page.get_by_text("adapter fixture output", exact=True).wait_for()
            assert len(Owner.instances) == 1
            assert not Owner.instances[0].closed
            assert not errors
            browser.close()
        assert not Owner.instances[0].closed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


PEER = r"""
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if 'id' not in m: continue
    method = m.get('method')
    if method == 'initialize': result = {'userAgent': 'controlled-test-peer'}
    elif method == 'thread/resume': result = {'thread': {'id': m['params']['threadId'], 'turns': []}}
    elif method == 'turn/start':
        result = {'turn': {'id': 't'}}
        print(json.dumps({'method': 'item/agentMessage/delta', 'params': {'threadId': m['params']['threadId'], 'delta': m['params']['input'][0]['text']}}), flush=True)
    else: result = {}
    print(json.dumps({'id': m['id'], 'result': result}), flush=True)
"""


def test_host_routes_real_bidirectional_pipes_into_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENA_RUNTIME_LEASE_DIR", str(tmp_path / "leases"))

    class PeerRpc(WorkspaceRpc):
        async def start(self, command, **kwargs):
            await super().start([sys.executable, "-u", "-c", PEER], **kwargs)

    class PeerCodex(CodexWorkspace):
        async def open(self):
            return await super().open(binary=sys.executable)

    owners = []

    def factory(**kwargs):
        owner = PeerCodex(rpc=PeerRpc(), **kwargs)
        owners.append(owner)
        return owner

    host = WorkspaceHost(
        journal=WorkspaceJournal(tmp_path / "journal.db"),
        resolve=lambda sid: {"session_id": sid, "provider": "codex", "cwd": str(tmp_path)},
        factories={"codex": factory},
    )
    try:
        assert host.attach("exact")["ok"]
        process = owners[0].rpc.process
        assert host.command(
            "exact", "one", "submit", {"inputs": [{"type": "text", "text": "through real pipes"}]}
        )["ok"]
        deadline = time.monotonic() + 3
        while len(host.events("exact")["events"]) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (
            host.events("exact")["events"][-1]["event"]["params"]["delta"] == "through real pipes"
        )
        assert host.attach("exact")["ok"]
        assert owners[0].rpc.process is process
    finally:
        host.shutdown()
    assert process.returncode is not None
