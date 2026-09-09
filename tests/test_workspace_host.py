import asyncio
import io
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


def test_interrupt_rejects_stale_displayed_turn_and_replays_receipt_without_stopping_new_turn(host):
    host.attach("exact")
    owner = host._sessions["exact"][0]
    calls = []
    async def interrupt():
        calls.append(owner.active_turn)
        return {"interrupted": True}
    owner.interrupt = interrupt
    owner.active_turn = "current"
    stale = host.command("exact", "stale", "interrupt", {"expectedTurnId": "old"})
    assert not stale["ok"] and "no longer active" in stale["error"]
    assert calls == []
    current = host.command("exact", "stop", "interrupt", {"expectedTurnId": "current"})
    assert current["ok"] and calls == ["current"]
    owner.active_turn = "next"
    assert host.command("exact", "stop", "interrupt", {"expectedTurnId": "current"}) == current
    assert calls == ["current"]
    invalid = host.command("exact", "invalid", "interrupt", {"expectedTurnId": None})
    assert not invalid["ok"]


def test_permission_mode_control_requires_explicit_boolean_confirmation(tmp_path):
    calls = []
    class PermissionOwner(Owner):
        async def set_permissions(self, mode, confirmed):
            calls.append((self.sid, mode, confirmed))
            return {"mode": mode}
    value = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "permissions.db"), resolve=lambda sid: {"session_id": sid, "provider": "claude", "cwd": str(tmp_path)}, factories={"claude": PermissionOwner})
    try:
        value.attach("exact")
        assert not value.command("exact", "invalid", "set_permissions", {"mode": "plan", "confirmed": "false"})["ok"]
        payload = {"mode": "plan", "confirmed": False}
        first = value.command("exact", "set", "set_permissions", payload)
        assert first["ok"]
        assert value.command("exact", "set", "set_permissions", payload) == first
        assert calls == [("exact", "plan", False)]
        assert not PermissionOwner.instances[-1].sent
    finally:
        value.shutdown()


def test_context_control_reads_attached_claude_without_query(tmp_path):
    class ContextOwner(Owner):
        async def context_usage(self):
            return {"model": "native", "percentage": 15}
    value = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "context.db"), resolve=lambda sid: {"session_id": sid, "provider": "claude", "cwd": str(tmp_path)}, factories={"claude": ContextOwner})
    try:
        with pytest.raises(ValueError, match="Explicitly attach"):
            value.command("exact", "before", "context_usage", {})
        value.attach("exact")
        assert value.command("exact", "read", "context_usage", {})["result"] == {"model": "native", "percentage": 15}
        assert not value.command("exact", "invalid", "context_usage", {"query": True})["ok"]
        assert not ContextOwner.instances[-1].sent
    finally:
        value.shutdown()


def test_host_routes_skill_steering_to_exact_existing_owner(host):
    host.attach("exact")
    owner = Owner.instances[-1]
    received = []
    async def steer(inputs, *, expected_turn_id, skills):
        received.append((inputs, expected_turn_id, skills))
        return {"turnId": expected_turn_id}
    owner.steer = steer
    result = host.command("exact", "skill-steer", "steer", {"inputs": [{"type": "text", "text": ""}], "expectedTurnId": "active", "skills": ["/skill"]})
    assert result["ok"]
    assert received == [([{"type": "text", "text": ""}], "active", ["/skill"])]
    assert not owner.sent


def test_codex_mcp_discovery_uses_attached_owner_without_claude_mutations(host):
    host.attach("exact")
    owner = Owner.instances[-1]
    async def inventory():
        return {"data": [{"name": "local", "status": "unknown"}]}
    owner.list_mcp_servers = inventory
    assert host.command("exact", "list-mcp", "mcp_servers", {})["result"] == {"data": [{"name": "local", "status": "unknown"}]}
    assert not host.command("exact", "bad-mutation", "mcp_server_control", {"name": "local", "action": "disable"})["ok"]
    assert not owner.sent


def test_mcp_controls_require_attach_and_replay_without_repeating(tmp_path):
    calls = []
    class McpOwner(Owner):
        async def list_mcp_servers(self):
            return {"data": []}
        async def control_mcp_server(self, name, action):
            calls.append((self.sid, name, action))
            return {"data": []}
    value = WorkspaceHost(
        journal=WorkspaceJournal(tmp_path / "mcp.db"),
        resolve=lambda sid: {"session_id": sid, "provider": "claude", "cwd": str(tmp_path)},
        factories={"claude": McpOwner},
    )
    try:
        with pytest.raises(ValueError, match="Explicitly attach"):
            value.command("exact", "before", "mcp_servers", {})
        value.attach("exact")
        assert value.command("exact", "list", "mcp_servers", {})["result"] == {"data": []}
        payload = {"name": "local", "action": "reconnect"}
        first = value.command("exact", "retry", "mcp_server_control", payload)
        assert first["ok"]
        assert value.command("exact", "retry", "mcp_server_control", payload) == first
        assert calls == [("exact", "local", "reconnect")]
        assert not value.command("exact", "bad", "mcp_server_control", {"name": "local"})["ok"]
        assert not McpOwner.instances[-1].sent
    finally:
        value.shutdown()


def test_command_discovery_is_provider_scoped_and_never_submits(tmp_path):
    class CommandOwner(Owner):
        reloads = 0

        async def list_commands(self):
            return {"data": [{"name": "context"}]}

        async def reload_skills(self):
            self.reloads += 1
            return {"data": [{"name": "fresh"}]}

    value = WorkspaceHost(
        journal=WorkspaceJournal(tmp_path / "commands.db"),
        resolve=lambda sid: {"session_id": sid, "provider": "claude", "cwd": str(tmp_path)},
        factories={"claude": CommandOwner},
    )
    try:
        value.attach("exact")
        assert value.command("exact", "list", "commands", {})["result"] == {
            "data": [{"name": "context"}]
        }
        assert not value.command("exact", "bad", "commands", {"run": "context"})["ok"]
        refreshed = value.command("exact", "reload", "reload_skills", {})
        assert refreshed["result"] == {"data": [{"name": "fresh"}]}
        assert value.command("exact", "reload", "reload_skills", {}) == refreshed
        assert CommandOwner.instances[-1].reloads == 1
        assert not value.command("exact", "bad-reload", "reload_skills", {"path": "/elsewhere"})["ok"]
        assert not CommandOwner.instances[-1].sent
    finally:
        value.shutdown()


@pytest.mark.parametrize("registration_fails", [False, True])
def test_fork_receipt_keeps_identity_even_if_indexing_fails(tmp_path, registration_fails):
    class ForkOwner(Owner):
        forks = 0

        async def fork_session(self):
            self.forks += 1
            return {"session_id": "new-fork", "provider": "claude", "cwd": str(tmp_path)}

    registered = []

    def register(target):
        registered.append(target)
        if registration_fails:
            raise RuntimeError("catalog unavailable")

    value = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "fork.db"),
                          resolve=lambda sid: {"session_id": sid, "provider": "claude", "cwd": str(tmp_path)},
                          factories={"claude": ForkOwner}, register_fork=register)
    try:
        value.attach("exact")
        assert not value.command("exact", "bad", "fork_session", {"session_id": "other"})["ok"]
        receipt = value.command("exact", "fork", "fork_session", {})
        assert receipt["ok"] and receipt["result"]["session_id"] == "new-fork"
        assert receipt["result"]["indexed"] is not registration_fails
        assert value.command("exact", "fork", "fork_session", {}) == receipt
        assert ForkOwner.instances[-1].forks == len(registered) == 1
        assert "new-fork" not in value._sessions
        assert not ForkOwner.instances[-1].sent
    finally:
        value.shutdown()


def test_background_controls_require_attach_and_deduplicate_stop(host):
    with pytest.raises(ValueError, match="Explicitly attach"):
        host.command("exact", "before", "background_tasks", {})
    assert not Owner.instances
    host.attach("exact")
    calls = []

    async def tasks():
        return {"data": []}

    async def stop(process_id):
        calls.append(process_id)
        return {"terminated": True}

    owner = Owner.instances[0]
    owner.list_background_tasks = tasks
    owner.terminate_background_task = stop
    assert host.command("exact", "list", "background_tasks", {})["result"] == {"data": []}
    assert not host.command("exact", "bad", "terminate_background_task", {"pid": "p"})["ok"]
    for _ in range(2):
        assert host.command("exact", "stop", "terminate_background_task", {"processId": "p"})["ok"]
    assert calls == ["p"]
    assert not owner.closed


def test_answer_receipts_do_not_store_form_content(host):
    import sqlite3

    host.attach("exact")
    received = []

    async def answer(request_id, content):
        received.append(content)
        return {}

    Owner.instances[0].answer = answer
    payload = {
        "request_id": 1,
        "answer": {"action": "accept", "content": {"field": "private-form-value"}},
    }
    assert host.command("exact", "reply", "answer", payload)["ok"]
    assert host.command("exact", "reply", "answer", payload)["ok"]
    assert len(received) == 1
    with sqlite3.connect(host.journal.path) as conn:
        saved = conn.execute(
            "SELECT payload FROM workspace_commands WHERE request_id='reply'"
        ).fetchone()[0]
    assert "private-form-value" not in saved and "sha256" in saved


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


def test_image_preview_requires_auth_and_exact_session_without_launch(host):
    from PIL import Image

    image = io.BytesIO()
    Image.new("RGB", (8, 8), "green").save(image, format="PNG")
    raw = image.getvalue()
    record = host.uploads.save("exact", "image.png", io.BytesIO(raw))
    app = Flask(__name__)
    token = "s" * 40
    app.register_blueprint(workspace_blueprint(host, token=token))
    headers = {"X-Serena-Workspace-Token": token}
    url = f"/api/workspace/exact/attachments/{record['token']}"
    with app.test_client() as client:
        assert client.get(url, base_url="http://127.0.0.1").status_code == 403
        response = client.get(url, base_url="http://127.0.0.1", headers=headers)
        assert response.status_code == 200 and response.data == raw
        assert response.mimetype == "image/png"
        assert response.headers["Cache-Control"] == "no-store"
        assert (
            client.get(
                url.replace("/exact/", "/different/"), base_url="http://127.0.0.1", headers=headers
            ).status_code
            == 400
        )
    assert Owner.instances == [] and host._loop is None


def test_claude_input_routing_and_duplicate_receipt(host):
    import base64

    from PIL import Image

    host.factories = {"claude": Owner}
    host.resolve = lambda sid: {"session_id": sid, "provider": "claude", "cwd": "."}
    assert host.events("claude-exact")["events"] == []
    assert Owner.instances == []
    assert host.attach("claude-exact")["provider"] == "claude"
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "green").save(stream, format="PNG")
    raw = stream.getvalue()
    upload = host.uploads.save("claude-exact", "photo.png", io.BytesIO(raw))
    payload = {
        "inputs": [
            {"type": "text", "text": "look at this"},
            {"type": "upload", "token": upload["token"]},
        ]
    }
    receipt = host.command("claude-exact", "once", "submit", payload)
    assert receipt["ok"]
    assert host.command("claude-exact", "once", "submit", payload) == receipt
    owner = Owner.instances[0]
    assert owner.sid == "claude-exact"
    assert len(owner.sent) == 1
    assert base64.b64decode(owner.sent[0][1]["source"]["data"]) == raw
    host.attach("different")
    rejected = host.command("different", "not-yours", "submit", payload)
    assert not rejected["ok"]
    assert Owner.instances[1].sent == []
    assert not owner.closed


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
            page.locator("input[type=file]").set_input_files(
                {"name": "notes.txt", "mimeType": "text/plain", "buffer": b"attached document"}
            )
            page.get_by_role("button", name="Send message", exact=True).click()
            page.get_by_text("adapter fixture output", exact=True).wait_for()
            assert Owner.instances[0].sent[0][0] == {
                "type": "text",
                "text": "through the actual HTTP host",
            }
            import json

            attachment = json.loads(
                Owner.instances[0].sent[0][1]["text"].removeprefix("User-attached file: ")
            )
            assert Path(attachment["path"]).read_bytes() == b"attached document"
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


def test_uploaded_image_reaches_same_owner_and_cross_session_token_is_rejected(host):
    from PIL import Image

    image = io.BytesIO()
    Image.new("RGB", (8, 8), "green").save(image, format="PNG")
    image.seek(0)
    app = Flask(__name__)
    app.register_blueprint(workspace_blueprint(host, token="s" * 40))
    kwargs = {"base_url": "http://127.0.0.1", "headers": {"X-Serena-Workspace-Token": "s" * 40}}
    with app.test_client() as client:
        upload = client.post(
            "/api/workspace/exact/uploads", data={"file": (image, "photo.png")}, **kwargs
        )
        assert upload.status_code == 200, upload.json
        assert Owner.instances == []
        token = upload.json["upload"]["token"]
        assert client.post("/api/workspace/exact/attach", **kwargs).json["ok"]
        data = {
            "request_id": "image",
            "action": "submit",
            "payload": {"inputs": [{"type": "upload", "token": token}]},
        }
        assert client.post("/api/workspace/exact/commands", json=data, **kwargs).json["ok"]
        delivered = Owner.instances[0].sent[0][0]
        assert delivered["type"] == "localImage"
        with Image.open(delivered["path"]) as decoded:
            assert decoded.size == (8, 8)
        assert client.post("/api/workspace/other/attach", **kwargs).json["ok"]
        assert not client.post("/api/workspace/other/commands", json=data, **kwargs).json["ok"]
        assert Owner.instances[1].sent == []


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
        while (
            not any(
                e["event"]["method"] == "item/agentMessage/delta"
                for e in host.events("exact")["events"]
            )
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        deltas = [
            e
            for e in host.events("exact")["events"]
            if e["event"]["method"] == "item/agentMessage/delta"
        ]
        assert deltas[0]["event"]["params"]["delta"] == "through real pipes"
        assert host.attach("exact")["ok"]
        assert owners[0].rpc.process is process
    finally:
        host.shutdown()
    assert process.returncode is not None
