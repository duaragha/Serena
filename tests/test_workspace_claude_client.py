import asyncio
from types import SimpleNamespace

import pytest

from core.workspace_claude_client import ClaudeTypeScriptClient


class Transport:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.owned_pid = 42
        self.calls = []

    async def open(self, **kwargs):
        return {"models": [{"value": "model"}]}

    async def close(self):
        self.calls.append(("close",))

    async def control(self, method, *args):
        self.calls.append((method, *args))
        return []

    async def send(self, message):
        self.calls.append(("send", message))


def test_compatibility_controls_and_native_records():
    async def run():
        options = SimpleNamespace(resume="exact", cwd="/project", cli_path="claude", env={})
        client = ClaudeTypeScriptClient(options=options, sdk_path="sdk", node_path="node", transport_factory=Transport)
        await client.connect()
        assert client.owned_pid == 42
        await client.set_model("model")
        await client.set_permission_mode("plan")
        assert (await client.get_server_info())["current_permission_mode"] == "plan"
        await client.stop_task("task")
        assert await client.get_mcp_status() == {"mcpServers": []}
        await client.toggle_mcp_server("server", False)
        assert ("stopTask", "task") in client.transport.calls
        assert ("toggleMcpServer", "server", False) in client.transport.calls
        wire = {"type": "assistant", "session_id": "exact", "message": {"content": []}}
        await client.messages.put(wire)
        assert await anext(client.receive_messages()) == wire
        await client.disconnect()
    asyncio.run(run())


def test_permission_context_and_answer_conversion():
    async def run():
        seen = []

        async def permission(tool, inputs, context):
            seen.append((tool, inputs, context))
            return SimpleNamespace(behavior="allow", updated_input={"answers": {"question": "answer"}})

        options = SimpleNamespace(resume="exact", cwd="/project", cli_path="claude", env={}, can_use_tool=permission)
        client = ClaudeTypeScriptClient(options=options, sdk_path="sdk", node_path="node", transport_factory=Transport)
        response = await client._request("claude/canUseTool", {"request": {"toolName": "AskUserQuestion", "input": {}},
                                                               "options": {"toolUseID": "tool", "agentID": "child"}})
        assert response == {"behavior": "allow", "updatedInput": {"answers": {"question": "answer"}}}
        assert seen[0][2].tool_use_id == "tool" and seen[0][2].agent_id == "child"
        with pytest.raises(RuntimeError, match="No interactive elicitation"):
            await client._request("claude/elicitation", {"request": {}})
    asyncio.run(run())
