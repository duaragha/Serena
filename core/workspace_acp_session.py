"""Exact-session ACP controls over an already owned transport.

The caller owns process admission, leases and authentication. This controller
never creates a session, launches a process, authenticates, or retries a prompt.
"""

import asyncio
from contextlib import suppress
from copy import deepcopy
from uuid import uuid4

from core.workspace_acp_events import AcpEvents


class AcpSession:
    def __init__(self, *, session_id, cwd, rpc, publish):
        if not isinstance(session_id, str) or not session_id or not cwd.is_absolute() or not cwd.is_dir():
            raise ValueError("Exact ACP identity and existing absolute project are required")
        self.session_id, self.cwd = session_id, cwd
        self.rpc, self.publish = rpc, publish
        self.events = AcpEvents(session_id)
        self.state = "closed"
        self.capabilities = {}
        self._lock = asyncio.Lock()
        self._answer_lock = asyncio.Lock()
        self._reader = None
        self.last_turn_id = None
        self.config_options = []
        self.commands = []
        self.usage = None

    def start_event_reader(self):
        if self._reader is not None:
            raise ValueError("ACP event reader is already active")
        self._reader = asyncio.create_task(self._read_events())

    async def _read_events(self):
        while True:
            message = await self.rpc.events.get()
            try:
                if self.state != "unavailable":
                    await self.receive(message)
            except Exception as error:
                self.state = "unavailable"
                with suppress(Exception):
                    await self.publish(self.events.event("workspace/transportClosed", {"reason": str(error)}))
            finally:
                self.rpc.events.task_done()

    async def stop_event_reader(self):
        """Detach the controller consumer only; never stop a native process."""
        if self._reader is not None:
            self.state = "unavailable"
            self._reader.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None

    async def _drain_events(self):
        if self._reader is not None:
            await self.rpc.events.join()

    async def load(self, initialization, *, mcp_servers):
        async with self._lock:
            if self.state != "closed":
                raise ValueError("ACP session was already opened or needs recovery")
            capabilities = initialization.get("agentCapabilities", {})
            if capabilities.get("loadSession") is not True:
                raise ValueError("ACP server cannot load persisted sessions")
            self.capabilities = deepcopy(capabilities)
            self.state = "loading"
            self.events.begin("history")
            try:
                result = await self.rpc.request("session/load", {"sessionId": self.session_id,
                    "cwd": str(self.cwd), "mcpServers": deepcopy(mcp_servers)}, timeout=60)
                await self._drain_events()
                if self.state == "unavailable":
                    raise ValueError("ACP session lost verified event routing")
                if result is not None and not isinstance(result, dict):
                    raise ValueError("Invalid ACP load response")
                self.config_options = deepcopy((result or {}).get("configOptions", []))
                history = {"id": "history", "status": "completed", "items": list(self.events.items.values())}
                await self.publish(self.events.event("workspace/history", {
                    "thread": {"id": self.session_id, "turns": [history] if history["items"] else []},
                    "acpSettings": result or {}, "acpUsage": self.usage}))
                if "configOptions" in (result or {}):
                    await self.publish_model_state()
                self.events.turn = None
                self.state = "ready"
                return result
            except BaseException:
                self.state = "unavailable"
                raise

    async def receive(self, message):
        try:
            await self._receive(message)
        except BaseException:
            self.state = "unavailable"
            raise

    async def _receive(self, message):
        method, params = message.get("method"), message.get("params")
        if method == "session/update":
            event = self.events.update(params)
            if event["method"] == "workspace/acpUsage":
                self.usage = deepcopy(event["params"]["usage"])
            if params["update"]["sessionUpdate"] == "config_option_update":
                self.config_options = deepcopy(params["update"].get("configOptions", []))
                await self.publish_model_state()
            if params["update"]["sessionUpdate"] == "available_commands_update":
                commands = params["update"].get("availableCommands")
                if not isinstance(commands, list) or any(
                    not isinstance(command, dict) or not isinstance(command.get("name"), str)
                    or not command["name"] or any(c.isspace() or c == "/" for c in command["name"])
                    or not isinstance(command.get("description"), str)
                    or (command.get("input") is not None and (not isinstance(command["input"], dict)
                        or not isinstance(command["input"].get("hint"), str))) for command in commands
                ):
                    raise ValueError("Invalid ACP command catalog")
                self.commands = [{"name": command["name"], "description": command["description"],
                                  "argumentHint": (command.get("input") or {}).get("hint", ""),
                                  "kind": "command"} for command in commands]
                await self.publish(self.events.event("workspace/commands", {"data": self.commands}))
            if self.state != "loading" or event["method"] in {"workspace/acpMetadata", "workspace/acpUsage"}:
                await self.publish(event)
        elif method == "session/request_permission":
            async with self._answer_lock:
                request_id = message.get("id")
                event = self.events.permission(request_id, params)
                await self.publish(event)
                if self.state == "cancelling":
                    await self.rpc.answer(request_id, self.events.answer(request_id))
                    await self.publish(self.events.resolved(request_id))
        else:
            raise ValueError("Unsupported ACP server request or notification")

    def validate_content(self, content):
        if not isinstance(content, list) or not content:
            raise ValueError("ACP prompt content is required")
        content = deepcopy(content)
        supported = self.capabilities.get("promptCapabilities", {})
        if not isinstance(supported, dict):
            raise ValueError("Invalid ACP prompt capabilities")
        for block in content:
            kind = block.get("type") if isinstance(block, dict) else None
            fields = {"text": ("text",), "resource_link": ("uri", "name"),
                      "image": ("data", "mimeType"), "audio": ("data", "mimeType"), "resource": ()}
            if not isinstance(kind, str) or kind not in fields:
                raise ValueError("Unsupported ACP prompt block")
            if any(not isinstance(block.get(field), str) for field in fields[kind]):
                raise ValueError("Invalid ACP prompt content")
            if kind == "resource":
                resource = block.get("resource")
                if (not isinstance(resource, dict) or not isinstance(resource.get("uri"), str)
                    or not any(isinstance(resource.get(field), str) for field in ("text", "blob"))):
                    raise ValueError("Invalid ACP embedded resource")
            capability = {"image": "image", "audio": "audio", "resource": "embeddedContext"}.get(kind)
            if capability and supported.get(capability) is not True:
                raise ValueError("ACP server did not advertise this prompt capability")
        return content

    async def prompt(self, content):
        async with self._lock:
            if self.state != "ready":
                raise ValueError("ACP session is not ready for input")
            content = self.validate_content(content)
            self.state = "running"
            start = self.events.begin(str(uuid4()))
            self.last_turn_id = self.events.turn
        try:
            await self.publish(start)
            await self.publish(self.events.submitted(content))
            result = await self.rpc.request("session/prompt", {"sessionId": self.session_id,
                "prompt": deepcopy(content)}, timeout=None)
            await self._drain_events()
            if self.state == "unavailable":
                raise ValueError("ACP session lost verified event routing")
            completion = self.events.complete(result.get("stopReason") if isinstance(result, dict) else None)
            await self.publish(completion)
            self.state = "ready"
            return result
        except BaseException:
            self.state = "unavailable"
            raise

    async def answer(self, request_id, option_id=None):
        async with self._answer_lock:
            result = self.events.answer(request_id, option_id)
            await self.rpc.answer(request_id, result)
            await self.publish(self.events.resolved(request_id))

    def model_option(self):
        return self.select_option("model")

    def select_option(self, category):
        if not isinstance(self.config_options, list):
            raise ValueError("Invalid ACP configuration catalog")
        candidates = [option for option in self.config_options if isinstance(option, dict)
                      and option.get("category") == category and option.get("type") == "select"]
        if not candidates:
            raise ValueError(f"ACP {category} selection is not advertised")
        option = candidates[0]
        choices = option.get("options")
        if (not isinstance(option.get("id"), str) or not option["id"]
            or not isinstance(choices, list) or not choices
            or any(not isinstance(choice, dict) or not isinstance(choice.get("value"), str)
                   or not choice["value"] or not isinstance(choice.get("name"), str) for choice in choices)
            or len({choice["value"] for choice in choices}) != len(choices)
            or option.get("currentValue") not in {choice["value"] for choice in choices}):
            raise ValueError(f"Invalid ACP {category} options")
        return deepcopy(option)

    async def publish_model_state(self):
        try:
            option = self.model_option()
        except ValueError as error:
            result = {"data": [], "settings": {"model": None}, "reason": str(error)}
        else:
            result = {"data": [{"id": choice["value"], "model": choice["value"],
                                "displayName": choice["name"], "supportedReasoningEfforts": []}
                               for choice in option["options"]],
                      "settings": {"model": option["currentValue"]}}
        await self.publish(self.events.event("workspace/models", result))
        return result

    async def set_model(self, model):
        await self.set_selection("model", model)

    async def set_selection(self, category, value):
        if category not in {"model", "mode"}:
            raise ValueError("Unsupported ACP selection")
        async with self._lock:
            if self.state != "ready":
                raise ValueError("ACP session is not ready for configuration")
            option = self.select_option(category)
            if not isinstance(value, str) or value not in {choice["value"] for choice in option["options"]}:
                raise ValueError(f"ACP {category} was not offered")
            if value == option["currentValue"]:
                return
            self.state = "configuring"
            try:
                result = await self.rpc.request("session/set_config_option", {
                    "sessionId": self.session_id, "configId": option["id"], "value": value})
                await self._drain_events()
                if self.state == "unavailable" or not isinstance(result, dict) or "configOptions" not in result:
                    raise ValueError(f"ACP {category} change is unconfirmed")
                self.config_options = deepcopy(result["configOptions"])
                confirmed = self.select_option(category)
                if confirmed["id"] != option["id"] or confirmed["currentValue"] != value:
                    raise ValueError(f"ACP {category} change is unconfirmed")
                await self.publish_model_state()
                await self.publish(self.events.event("workspace/settings", {category: value}))
                self.state = "ready"
            except BaseException:
                self.state = "unavailable"
                raise

    async def cancel(self):
        async with self._answer_lock:
            if self.state != "running":
                raise ValueError("No ACP turn is running")
            self.state = "cancelling"
            try:
                for request_id in list(self.events.questions):
                    await self.rpc.answer(request_id, self.events.answer(request_id))
                    await self.publish(self.events.resolved(request_id))
                await self.rpc.notify("session/cancel", {"sessionId": self.session_id})
            except BaseException:
                self.state = "unavailable"
                with suppress(Exception):
                    await self.publish(self.events.event("workspace/transportClosed", {
                        "reason": "Stop delivery could not be confirmed; the provider may still be running"}))
                raise
