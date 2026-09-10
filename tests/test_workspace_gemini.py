import asyncio
import json
import os
import sys
import time

import pytest

from core.workspace_acp import WorkspaceAcpRpc
from core.workspace_gemini import GeminiWorkspace
from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from core.workspace_lease import SessionLease, SessionOwnedError

SID = "11111111-2222-4333-8444-555555555555"


def test_host_routes_real_pipe_prompt_permission_output_and_receipt(tmp_path):
    prepare(tmp_path)

    class InteractiveRpc(WorkspaceAcpRpc):
        async def start(self, command, *, cwd, env):
            code = '''
import json, sys
sid = "11111111-2222-4333-8444-555555555555"
def read():
    message = json.loads(sys.stdin.readline())
    assert message["jsonrpc"] == "2.0"
    return message
def send(message):
    print(json.dumps({"jsonrpc": "2.0", **message}), flush=True)
message = read()
assert message["method"] == "initialize"
send({"id": message["id"], "result": {"protocolVersion": 1,
      "agentCapabilities": {"loadSession": True}, "authMethods": [],
      "agentInfo": {"name": "antigravity-acp"}}})
message = read()
assert message["method"] == "session/load" and message["params"]["sessionId"] == sid
def config(current):
    return [{"id": "model", "category": "model", "type": "select", "currentValue": current,
             "options": [{"value": "first", "name": "First"}, {"value": "second", "name": "Second"}]}]
send({"id": message["id"], "result": {"configOptions": config("first")}})
send({"method": "session/update", "params": {"sessionId": sid, "update": {
    "sessionUpdate": "available_commands_update", "availableCommands": [
        {"name": "plan", "description": "Plan work"}]}}})
message = read()
assert message["method"] == "session/set_config_option"
assert message["params"] == {"sessionId": sid, "configId": "model", "value": "second"}
send({"id": message["id"], "result": {"configOptions": config("second")}})
prompt = read()
assert prompt["method"] == "session/prompt" and prompt["params"]["sessionId"] == sid
assert prompt["params"]["prompt"] == [{"type": "text", "text": "/plan inspect"}]
send({"id": "permission", "method": "session/request_permission", "params": {
    "sessionId": sid, "toolCall": {"toolCallId": "read", "title": "Inspect"},
    "options": [{"optionId": "once", "name": "Allow once", "kind": "allow_once"}]}})
answer = read()
assert answer == {"jsonrpc": "2.0", "id": "permission", "result": {
    "outcome": {"outcome": "selected", "optionId": "once"}}}
send({"method": "session/update", "params": {"sessionId": sid,
      "update": {"sessionUpdate": "agent_message_chunk", "content": {
          "type": "text", "text": "pipe result"}}}})
send({"id": prompt["id"], "result": {"stopReason": "end_turn"}})
assert sys.stdin.read() == "", "Unexpected duplicate delivery"
'''
            await super().start([sys.executable, "-c", code], cwd=cwd, env=env)

    rpc = InteractiveRpc()
    def factory(**kwargs):
        return GeminiWorkspace(**kwargs, gemini_home=tmp_path, binary=sys.executable, rpc=rpc,
            lease_factory=lambda sid: SessionLease(sid, directory=tmp_path / "leases"))

    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "host.db"),
        resolve=lambda sid: {"session_id": sid, "provider": "gemini", "cwd": str(tmp_path)},
        factories={"gemini": factory})
    process = None
    def wait_event(method):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            events = [entry["event"] for entry in host.events(SID)["events"]]
            found = next((event for event in events if event["method"] == method), None)
            if found:
                return found
            time.sleep(0.01)
        pytest.fail(f"Missing event: {method}")
    try:
        host.events(SID)
        assert rpc.process is None
        assert host.attach(SID)["state"] == "ready"
        process = rpc.process
        models = host.command(SID, "models", "models", {})
        assert models["ok"] and [model["model"] for model in models["result"]["data"]] == ["first", "second"]
        wait_event("workspace/commands")
        commands = host.command(SID, "commands", "commands", {})
        assert commands["ok"] and commands["result"]["data"][0]["name"] == "plan"
        payload = {"inputs": [{"type": "text", "text": "/plan inspect"}], "options": {"model": "second"}}
        sent = host.command(SID, "send", "submit", payload)
        assert sent["ok"]
        assert host.command(SID, "send", "submit", payload) == sent
        question = wait_event("session/request_permission")
        assert question["id"] == "permission" and question["params"]["threadId"] == SID
        bad = host.command(SID, "bad", "answer", {"request_id": "permission", "answer": {
            "outcome": {"outcome": "selected", "optionId": "not-offered"}}})
        assert not bad["ok"]
        answer = {"request_id": "permission", "answer": {"outcome": {
            "outcome": "selected", "optionId": "once"}}}
        receipt = host.command(SID, "answer", "answer", answer)
        assert receipt["ok"] and host.command(SID, "answer", "answer", answer) == receipt
        final = wait_event("turn/completed")["params"]["turn"]
        assert final["id"] == sent["result"]["turn"]["id"]
        assert final["items"][0]["origin"] == "client"
        assert final["items"][0]["content"] == payload["inputs"]
        assert final["items"][1]["text"] == "pipe result"
        assert host.attach(SID)["state"] == "ready" and rpc.process is process
        assert process.returncode is None
    finally:
        host.shutdown()
    assert process is not None and process.returncode == 0


def prepare(tmp_path):
    root = tmp_path / "antigravity-acp"
    (root / "conversations").mkdir(parents=True)
    (root / "conversations" / f"{SID}.db").write_bytes(b"fixture")
    (root / "conversations" / f"{SID}.meta").write_text(json.dumps({"cwd": str(tmp_path)}))
    (root / "settings.json").write_text('{"auth":{"type":"oauth-personal"}}')
    (root / "acp_token.json").write_text("fixture-not-a-credential")
    return root


@pytest.mark.parametrize("settings", [
    '{\n// personal account\n"auth": {"type": "oauth-personal",},\n}',
    'auth: {\n  type: oauth-personal\n}\n',
])
def test_native_hjson_settings_are_accepted_without_rewriting(tmp_path, settings):
    root = prepare(tmp_path)
    path = root / "settings.json"
    path.write_text(settings, encoding="utf-8")
    owner = make(tmp_path, ProbeRpc())
    assert owner._validate_native_target().name == f"{SID}.db"
    assert path.read_text(encoding="utf-8") == settings
    assert owner.rpc.process is None


@pytest.mark.parametrize("settings", ['[]', '{"auth": null}', '{"auth": "oauth-personal"}',
                                      '{auth: {type: "gemini-api-key"}}', '{"auth":'])
def test_native_settings_invalid_shape_or_auth_remains_unavailable(tmp_path, settings):
    root = prepare(tmp_path)
    (root / "settings.json").write_text(settings)
    owner = make(tmp_path, ProbeRpc())
    with pytest.raises(ValueError):
        owner._validate_native_target()
    assert owner.rpc.process is None


class ProbeRpc(WorkspaceAcpRpc):
    launched = False

    async def start(self, command, *, cwd, env):
        self.launched = True
        self.environment = env
        code = """
import json, sys
for expected in ['initialize','session/load']:
    message=json.loads(sys.stdin.readline())
    assert message['method']==expected
    if expected=='initialize':
        result={'protocolVersion':1,'agentCapabilities':{'loadSession':True},
          'authMethods':[],'agentInfo':{'name':'antigravity-acp'}}
    else:
        assert message['params']['sessionId']=='11111111-2222-4333-8444-555555555555'
        result={}
    print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':result}),flush=True)
assert sys.stdin.read()==''
"""
        await super().start([sys.executable, "-c", code], cwd=cwd, env=env)


def make(tmp_path, rpc):
    async def publish(event):
        pass
    return GeminiWorkspace(session_id=SID, cwd=tmp_path, gemini_home=tmp_path,
        binary=sys.executable, publish=publish, rpc=rpc,
        lease_factory=lambda sid: SessionLease(sid, directory=tmp_path / "leases"))


def test_owner_retains_single_process_and_releases_only_after_shutdown(tmp_path):
    prepare(tmp_path)
    async def run():
        rpc = ProbeRpc()
        owner = make(tmp_path, rpc)
        try:
            await owner.open(env={**os.environ, "GEMINI_API_KEY": "must-be-removed"})
            process = rpc.process
            assert owner.state == "ready"
            assert rpc.environment["GEMINI_HOME"] == str(tmp_path)
            assert "GEMINI_API_KEY" not in rpc.environment
            other = make(tmp_path, ProbeRpc())
            with pytest.raises(SessionOwnedError):
                await other.open()
            assert not other.rpc.launched and process.returncode is None
        finally:
            await owner.close()
        assert process.returncode == 0
        lease = SessionLease(SID, directory=tmp_path / "leases")
        lease.release()
    asyncio.run(run())


@pytest.mark.parametrize("problem", ["missing-token", "api-billing", "wrong-project", "cli-symlink", "cli-hardlink"])
def test_invalid_native_target_cannot_launch_or_migrate(tmp_path, problem):
    root = prepare(tmp_path)
    if problem == "missing-token":
        (root / "acp_token.json").unlink()
    elif problem == "api-billing":
        (root / "settings.json").write_text('{"auth":{"type":"gemini-api-key"}}')
    elif problem == "wrong-project":
        (root / "conversations" / f"{SID}.meta").write_text('{"cwd":"/wrong"}')
    elif problem == "cli-hardlink":
        os.link(root / "conversations" / f"{SID}.db", tmp_path / "cli.db")
    else:
        db = root / "conversations" / f"{SID}.db"
        original = tmp_path / "cli.db"
        db.rename(original)
        db.symlink_to(original)
    async def run():
        rpc = ProbeRpc()
        owner = make(tmp_path, rpc)
        with pytest.raises(ValueError):
            await owner.open()
        assert not rpc.launched and not (tmp_path / "leases").exists()
    asyncio.run(run())


def test_owner_submit_returns_exact_turn_without_blocking_interrupt(tmp_path):
    async def run():
        owner = make(tmp_path, ProbeRpc())
        owner.session.state = "ready"
        finished = asyncio.Event()
        async def prompt(inputs):
            owner.session.state = "running"
            owner.session.events.begin("native-turn")
            owner.session.last_turn_id = "native-turn"
            await finished.wait()
            owner.session.events.complete("cancelled")
            owner.session.state = "ready"
        async def cancel():
            finished.set()
        owner.session.prompt = prompt
        owner.session.cancel = cancel
        result = await owner.submit([{"type": "text", "text": "one"}])
        assert result == {"turn": {"id": "native-turn"}}
        with pytest.raises(ValueError, match="not ready"):
            await owner.submit([{"type": "text", "text": "two"}])
        await owner.interrupt()
        await owner._turn_task
        assert owner.state == "ready" and owner.active_turn is None
        with pytest.raises(ValueError, match="Invalid ACP"):
            await owner.answer("request", {"outcome": {"outcome": "selected", "optionId": None}})
    asyncio.run(run())


def test_owner_shutdown_cancels_prompt_waiting_on_stopped_event_reader(tmp_path):
    async def run():
        owner = make(tmp_path, ProbeRpc())
        waiting = asyncio.Event()
        order = []

        async def prompt():
            waiting.set()
            try:
                await asyncio.Future()
            finally:
                order.append("prompt-stopped")

        async def close_rpc():
            order.append("native-stopped")

        owner.rpc.close = close_rpc
        owner._turn_task = asyncio.create_task(prompt())
        await waiting.wait()
        await asyncio.wait_for(owner.close(), timeout=1)
        assert owner._turn_task.cancelled()
        assert order == ["native-stopped", "prompt-stopped"]
        assert owner.state == "unavailable"
    asyncio.run(run())


def test_owner_acknowledges_turn_that_completes_before_submit_returns(tmp_path):
    async def run():
        owner = make(tmp_path, ProbeRpc())
        owner.session.state = "ready"

        async def prompt(inputs):
            owner.session.events.begin("fast-turn")
            owner.session.last_turn_id = "fast-turn"
            owner.session.events.complete("end_turn")

        owner.session.prompt = prompt
        assert await owner.submit([{"type": "text", "text": "hello"}]) == {
            "turn": {"id": "fast-turn"}}
        assert owner.active_turn is None
        assert owner._turn_task.done()
    asyncio.run(run())


@pytest.mark.parametrize("inputs", [[], [{"type": "text", "text": None}],
    [{"type": "image", "data": "AA==", "mimeType": "image/png"}],
    [{"type": "image", "data": "AA=="}], [{"type": "resource_link", "uri": "file:///x"}],
    [{"type": "resource", "resource": {"uri": "file:///x"}}]])
def test_invalid_submission_preserves_owner_and_never_changes_model(tmp_path, inputs):
    async def run():
        owner = make(tmp_path, ProbeRpc())
        owner.session.state = "ready"
        output, requests = [], []
        async def publish(event):
            output.append(event)
        async def request(method, params, **kwargs):
            requests.append((method, params))
            return {"stopReason": "end_turn"}
        owner.session.publish = publish
        owner.rpc.request = request
        with pytest.raises(ValueError):
            await owner.submit(inputs, options={"model": "must-not-change"})
        assert owner.state == "ready" and owner.active_turn is None
        assert requests == [] and output == []
        result = await owner.submit([{"type": "text", "text": "valid next input"}])
        await owner._turn_task
        assert result["turn"]["id"] == output[-1]["params"]["turn"]["id"]
        assert len(requests) == 1 and requests[0][0] == "session/prompt"
        assert owner.state == "ready"
    asyncio.run(run())
