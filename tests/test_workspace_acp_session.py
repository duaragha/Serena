import asyncio

import pytest

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
