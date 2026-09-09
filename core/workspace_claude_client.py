"""Public TypeScript SDK client matching the current ClaudeWorkspace interface."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

from core.workspace_claude_transport import ClaudeSdkTransport


class ClaudeTypeScriptClient:
    def __init__(self, *, options, sdk_path, node_path, transport_factory=ClaudeSdkTransport,
                 on_elicitation=None):
        self.options = options
        self.on_elicitation = on_elicitation
        self.messages = asyncio.Queue()
        self.info = None
        self.transport = transport_factory(
            session_id=options.resume, cwd=options.cwd, sdk_path=sdk_path,
            cli_path=options.cli_path, node_path=node_path,
            publish=self.messages.put, request=self._request,
        )

    @property
    def owned_pid(self):
        return self.transport.owned_pid

    async def connect(self):
        self.info = await self.transport.open(env=self.options.env)

    async def disconnect(self):
        await self.transport.close()
        await self.messages.put(None)

    async def receive_messages(self):
        while True:
            message = await self.messages.get()
            if message is None:
                return
            if message.get("type") == "transport_error":
                raise RuntimeError(message["error"])
            yield message

    async def query(self, messages, session_id):
        if session_id != self.options.resume:
            raise ValueError("Claude input targets a different session")
        async for message in messages:
            await self.transport.send(message)

    async def get_server_info(self):
        return deepcopy(self.info)

    async def reload_skills(self):
        await self.transport.control("reloadSkills")
        commands = await self.transport.control("supportedCommands")
        if not isinstance(commands, list) or any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in commands):
            raise ValueError("Claude returned an invalid refreshed command catalog")
        self.info["commands"] = deepcopy(commands)

    async def fork_session(self):
        return await self.transport.control("forkSession")

    async def reload_plugins(self):
        result = await self.transport.control("reloadPlugins")
        if not isinstance(result, dict):
            raise ValueError("Claude returned an invalid plugin reload result")
        for key in ("commands", "agents", "plugins", "mcpServers"):
            if not isinstance(result.get(key), list) or any(not isinstance(item, dict) for item in result[key]):
                raise ValueError("Claude returned an invalid plugin reload result")
        if any(not isinstance(item.get("name"), str) for item in result["commands"]):
            raise ValueError("Claude returned an invalid refreshed command catalog")
        if type(result.get("error_count")) is not int or result["error_count"] < 0:
            raise ValueError("Claude returned an invalid plugin error count")
        self.info["commands"] = deepcopy(result["commands"])
        return deepcopy(result)

    async def set_model(self, model):
        return await self.transport.control("setModel", model)

    async def set_permission_mode(self, mode):
        result = await self.transport.control("setPermissionMode", mode)
        self.info["current_permission_mode"] = mode
        return result

    async def get_context_usage(self):
        return await self.transport.control("getContextUsage")

    async def stop_task(self, task_id):
        return await self.transport.control("stopTask", task_id)

    async def get_mcp_status(self):
        return {"mcpServers": await self.transport.control("mcpServerStatus")}

    async def reconnect_mcp_server(self, name):
        return await self.transport.control("reconnectMcpServer", name)

    async def toggle_mcp_server(self, name, enabled):
        return await self.transport.control("toggleMcpServer", name, enabled)

    async def interrupt(self):
        return await self.transport.control("interrupt")

    async def _request(self, method, params):
        request, options = params["request"], params.get("options") or {}
        if method == "claude/elicitation":
            if self.on_elicitation is None:
                raise RuntimeError("No interactive elicitation handler is installed")
            answer = await self.on_elicitation(request, params.get("nativeRequestId"))
            # MCP ElicitResult has optional object content, not a nullable field.
            return {key: value for key, value in answer.items() if key != "content" or value is not None}
        if method != "claude/canUseTool":
            raise ValueError("Unknown Claude interactive request")
        context = SimpleNamespace(tool_use_id=options.get("toolUseID"),
                                  title=options.get("title"), agent_id=options.get("agentID"))
        result = await self.options.can_use_tool(request["toolName"], request["input"], context)
        if result.behavior == "allow":
            if getattr(result, "updated_permissions", None):
                raise ValueError("Permission rule changes require an explicit native mapping")
            return {"behavior": "allow", "updatedInput": result.updated_input}
        if result.behavior == "deny":
            return {"behavior": "deny", "message": result.message, "interrupt": result.interrupt}
        raise ValueError("Explicit Claude permission response required")
