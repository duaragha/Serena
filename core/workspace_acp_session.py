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
                history = {"id": "history", "status": "completed", "items": list(self.events.items.values())}
                await self.publish(self.events.event("workspace/history", {
                    "thread": {"id": self.session_id, "turns": [history] if history["items"] else []},
                    "acpSettings": result or {}}))
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
            if self.state != "loading" or event["method"] == "workspace/acpMetadata":
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

    async def prompt(self, content):
        async with self._lock:
            if self.state != "ready":
                raise ValueError("ACP session is not ready for input")
            if not isinstance(content, list) or not content:
                raise ValueError("ACP prompt content is required")
            supported = self.capabilities.get("promptCapabilities", {})
            for block in content:
                kind = block.get("type") if isinstance(block, dict) else None
                if kind not in {"text", "resource_link", "image", "audio", "resource"}:
                    raise ValueError("Unsupported ACP prompt block")
                capability = {"image": "image", "audio": "audio", "resource": "embeddedContext"}.get(kind)
                if capability and supported.get(capability) is not True:
                    raise ValueError("ACP server did not advertise this prompt capability")
            self.state = "running"
            start = self.events.begin(str(uuid4()))
        try:
            await self.publish(start)
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

    async def cancel(self):
        async with self._answer_lock:
            if self.state != "running":
                raise ValueError("No ACP turn is running")
            self.state = "cancelling"
            for request_id in list(self.events.questions):
                await self.rpc.answer(request_id, self.events.answer(request_id))
                await self.publish(self.events.resolved(request_id))
            await self.rpc.notify("session/cancel", {"sessionId": self.session_id})
