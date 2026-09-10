import asyncio
import os
import sys

import pytest

from core.workspace_acp import WorkspaceAcpRpc
from core.workspace_acp_session import AcpSession


class Rpc:
    def __init__(self):
        self.calls = []
        self.handler = None

    async def request(self, method, params, **kwargs):
        self.calls.append((method, params))
        return await self.handler(method, params) if self.handler else {}

    async def answer(self, request_id, result):
        self.calls.append(("answer", request_id, result))

    async def notify(self, method, params):
        self.calls.append((method, params))


@pytest.mark.parametrize("confirmed", [True, False])
@pytest.mark.parametrize("category", ["model", "mode"])
def test_native_model_change_requires_offered_value_and_confirmed_response(tmp_path, confirmed, category):
    async def run():
        rpc, output = Rpc(), []
        async def publish(event):
            output.append(event)
        owner = AcpSession(session_id="exact", cwd=tmp_path, rpc=rpc, publish=publish)
        def config(current):
            return [{"id": "native-model", "category": category, "type": "select",
                     "currentValue": current, "options": [{"value": "a", "name": "Model A"},
                                                          {"value": "b", "name": "Model B"}]}]
        async def handler(method, params):
            if method == "session/load":
                return {"configOptions": config("a")}
            assert method == "session/set_config_option"
            assert params == {"sessionId": "exact", "configId": "native-model", "value": "b"}
            assert owner.state == "configuring"
            return {"configOptions": config("b" if confirmed else "a")}
        rpc.handler = handler
        await owner.load({"agentCapabilities": {"loadSession": True}}, mcp_servers=[])
        assert len(rpc.calls) == 1
        with pytest.raises(ValueError, match="not offered"):
            await owner.set_selection(category, "invented")
        await owner.set_selection(category, "a")
        assert len(rpc.calls) == 1
        if confirmed:
            await owner.set_selection(category, "b")
            assert owner.state == "ready"
            assert output[-1]["params"][category] == "b"
        else:
            with pytest.raises(ValueError, match="unconfirmed"):
                await owner.set_selection(category, "b")
            assert owner.state == "unavailable"
            assert not any(event["method"] == "workspace/settings" for event in output)
        assert len(rpc.calls) == 2
    asyncio.run(run())


def test_native_config_update_replaces_stale_model_options(tmp_path):
    async def run():
        rpc, output = Rpc(), []
        async def publish(event):
            output.append(event)
        owner = AcpSession(session_id="exact", cwd=tmp_path, rpc=rpc, publish=publish)
        await owner.load({"agentCapabilities": {"loadSession": True}}, mcp_servers=[])
        with pytest.raises(ValueError, match="not advertised"):
            owner.model_option()
        await owner.receive({"method": "session/update", "params": {"sessionId": "exact", "update": {
            "sessionUpdate": "config_option_update", "configOptions": [{"id": "model", "category": "model",
            "type": "select", "currentValue": "new", "options": [{"value": "new", "name": "New model"}]}]}}})
        assert owner.model_option()["currentValue"] == "new"
        model_event = [event for event in output if event["method"] == "workspace/models"][-1]
        assert model_event["params"]["settings"]["model"] == "new"
        await owner.receive({"method": "session/update", "params": {"sessionId": "exact", "update": {
            "sessionUpdate": "config_option_update", "configOptions": []}}})
        with pytest.raises(ValueError, match="not advertised"):
            owner.model_option()
        model_event = [event for event in output if event["method"] == "workspace/models"][-1]
        assert model_event["params"]["data"] == []
        assert model_event["params"]["settings"]["model"] is None
        assert len(rpc.calls) == 1
    asyncio.run(run())


def test_command_notifications_replace_catalog_without_execution(tmp_path):
    async def run():
        rpc, output = Rpc(), []
        async def publish(event):
            output.append(event)
        owner = AcpSession(session_id="exact", cwd=tmp_path, rpc=rpc, publish=publish)
        await owner.load({"agentCapabilities": {"loadSession": True}}, mcp_servers=[])
        async def commands(data):
            await owner.receive({"method": "session/update", "params": {"sessionId": "exact", "update": {
                "sessionUpdate": "available_commands_update", "availableCommands": data}}})
        await commands([{"name": "plan", "description": "Plan work", "input": {"hint": "task"}}])
        assert owner.commands == [{"name": "plan", "description": "Plan work", "argumentHint": "task", "kind": "command"}]
        await commands([])
        assert owner.commands == []
        assert len(rpc.calls) == 1
        with pytest.raises(ValueError, match="Invalid ACP command"):
            await commands([{"name": "plan\nlogout", "description": "invalid"}])
    asyncio.run(run())


def test_context_usage_during_load_survives_history_snapshot(tmp_path):
    async def run():
        rpc, output = Rpc(), []
        async def publish(event):
            output.append(event)
        owner = AcpSession(session_id="exact", cwd=tmp_path, rpc=rpc, publish=publish)
        async def load(method, params):
            await owner.receive({"method": "session/update", "params": {"sessionId": "exact", "update": {
                "sessionUpdate": "usage_update", "used": 123, "size": 1000}}})
            return {}
        rpc.handler = load
        await owner.load({"agentCapabilities": {"loadSession": True}}, mcp_servers=[])
        assert output[-1]["method"] == "workspace/history"
        assert output[-1]["params"]["acpUsage"] == {"used": 123, "size": 1000}
    asyncio.run(run())


def test_exact_load_failure_never_creates_or_retries(tmp_path):
    async def run():
        rpc, output = Rpc(), []
        async def publish(event):
            output.append(event)
        owner = AcpSession(session_id="exact", cwd=tmp_path, rpc=rpc, publish=publish)
        async def fail(method, params):
            raise ValueError("Native session not found")
        rpc.handler = fail
        with pytest.raises(ValueError, match="not found"):
            await owner.load({"agentCapabilities": {"loadSession": True}}, mcp_servers=[])
        with pytest.raises(ValueError, match="already opened"):
            await owner.load({"agentCapabilities": {"loadSession": True}}, mcp_servers=[])
        assert owner.state == "unavailable"
        assert rpc.calls == [("session/load", {"sessionId": "exact", "cwd": str(tmp_path), "mcpServers": []})]
        assert not output
    asyncio.run(run())


def test_history_prompt_and_late_permission_cancel_keep_exact_identity(tmp_path):
    async def run():
        rpc, output = Rpc(), []
        async def publish(event):
            output.append(event)
        owner = AcpSession(session_id="exact", cwd=tmp_path, rpc=rpc, publish=publish)
        done = asyncio.Event()
        async def handler(method, params):
            assert params["sessionId"] == "exact"
            if method == "session/load":
                await owner.receive({"method": "session/update", "params": {"sessionId": "exact", "update": {
                    "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "saved"}}}})
                return {}
            await done.wait()
            return {"stopReason": "cancelled"}
        rpc.handler = handler
        await owner.load({"agentCapabilities": {"loadSession": True}}, mcp_servers=[])
        assert output[-1]["method"] == "workspace/history"
        assert output[-1]["params"]["thread"]["turns"][0]["items"][0]["text"] == "saved"
        with pytest.raises(ValueError, match="capability"):
            await owner.prompt([{"type": "image", "data": "AA==", "mimeType": "image/png"}])
        task = asyncio.create_task(owner.prompt([{"type": "text", "text": "hello"}]))
        await asyncio.sleep(0)
        with pytest.raises(ValueError, match="not ready"):
            await owner.prompt([{"type": "text", "text": "second"}])
        await owner.cancel()
        await owner.receive({"method": "session/request_permission", "id": "late", "params": {
            "sessionId": "exact", "options": [{"optionId": "once", "kind": "allow_once", "name": "Allow"}]}})
        assert rpc.calls[-1] == ("answer", "late", {"outcome": {"outcome": "cancelled"}})
        done.set()
        await task
        assert owner.state == "ready" and not owner.events.questions
        assert output[-1]["params"]["turn"]["status"] == "interrupted"
        assert all(event["params"]["threadId"] == "exact" for event in output)
    asyncio.run(run())


def test_permission_during_load_can_be_answered_without_deadlock(tmp_path):
    async def run():
        rpc = Rpc()
        async def publish(event):
            pass
        owner = AcpSession(session_id="exact", cwd=tmp_path, rpc=rpc, publish=publish)
        async def handler(method, params):
            await owner.receive({"id": "trust", "method": "session/request_permission", "params": {
                "sessionId": "exact", "options": [{"optionId": "deny", "kind": "reject_once", "name": "Deny"}]}})
            await owner.answer("trust", "deny")
            return {}
        rpc.handler = handler
        await asyncio.wait_for(owner.load({"agentCapabilities": {"loadSession": True}}, mcp_servers=[]), 1)
        assert rpc.calls[-1] == ("answer", "trust", {"outcome": {"outcome": "selected", "optionId": "deny"}})
    asyncio.run(run())


def test_submitted_input_is_preserved_before_delivery_failure_without_replay(tmp_path):
    async def run():
        rpc, output = Rpc(), []
        content = [{"type": "text", "text": "original"}]
        async def publish(event):
            output.append(event)
            if event["method"] == "turn/started":
                content[0]["text"] = "changed by caller"
        owner = AcpSession(session_id="exact", cwd=tmp_path, rpc=rpc, publish=publish)
        await owner.load({"agentCapabilities": {"loadSession": True}}, mcp_servers=[])
        async def fail(method, params):
            assert params["prompt"] == [{"type": "text", "text": "original"}]
            assert output[-1]["params"]["item"]["origin"] == "client"
            raise RuntimeError("delivery unconfirmed")
        rpc.handler = fail
        with pytest.raises(RuntimeError, match="unconfirmed"):
            await owner.prompt(content)
        item = output[-1]["params"]["item"]
        assert item["content"] == [{"type": "text", "text": "original"}]
        assert "providerOriginal" not in item
        assert owner.state == "unavailable"
        with pytest.raises(ValueError, match="not ready"):
            await owner.prompt(content)
        assert [method for method, _ in rpc.calls].count("session/prompt") == 1
    asyncio.run(run())


def test_wrong_session_update_disables_further_input(tmp_path):
    async def run():
        rpc = Rpc()
        async def publish(event):
            pass
        owner = AcpSession(session_id="exact", cwd=tmp_path, rpc=rpc, publish=publish)
        await owner.load({"agentCapabilities": {"loadSession": True}}, mcp_servers=[])
        with pytest.raises(ValueError, match="another session"):
            await owner.receive({"method": "session/update", "params": {"sessionId": "wrong"}})
        with pytest.raises(ValueError, match="not ready"):
            await owner.prompt([{"type": "text", "text": "do not send"}])
        assert len(rpc.calls) == 1 and owner.state == "unavailable"
    asyncio.run(run())


def test_real_pipe_reply_waits_for_all_queued_history_and_turn_events(tmp_path):
    async def run():
        rpc, output = WorkspaceAcpRpc(), []
        async def publish(event):
            await asyncio.sleep(0.005)
            output.append(event)
        owner = AcpSession(session_id="exact", cwd=tmp_path, rpc=rpc, publish=publish)
        code = """
import json, sys
def emit(value):
    print(json.dumps({'jsonrpc':'2.0',**value}),flush=True)
for method in ['initialize','session/load','session/prompt']:
    message=json.loads(sys.stdin.readline())
    assert message['method']==method
    if method=='initialize':
        result={'protocolVersion':1,'agentCapabilities':{'loadSession':True},'authMethods':[]}
    else:
        assert message['params']['sessionId']=='exact'
        for i in range(20):
            emit({'method':'session/update','params':{'sessionId':'exact','update':{
                'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':str(i)+','}}}})
        result={} if method=='session/load' else {'stopReason':'end_turn'}
    emit({'id':message['id'],'result':result})
assert sys.stdin.read()==''
"""
        try:
            await rpc.start([sys.executable, "-c", code], cwd=tmp_path, env=dict(os.environ))
            initialization = await rpc.initialize()
            owner.start_event_reader()
            await owner.load(initialization, mcp_servers=[])
            expected = "".join(f"{i}," for i in range(20))
            assert output[-1]["params"]["thread"]["turns"][0]["items"][0]["text"] == expected
            await owner.prompt([{"type": "text", "text": "hello"}])
            assert output[-1]["method"] == "turn/completed"
            assert output[-1]["params"]["turn"]["items"][1]["text"] == expected
            deltas = [event for event in output if event["method"] == "item/agentMessage/delta"]
            assert len(deltas) == 19
            assert "".join(event["params"]["delta"] for event in deltas) == expected[2:]
            assert all(event["params"]["threadId"] == "exact" for event in deltas)
            await owner.stop_event_reader()
            assert rpc.process.returncode is None
        finally:
            await owner.stop_event_reader()
            process = rpc.process
            await rpc.close()
        assert process.returncode == 0
    asyncio.run(run())
