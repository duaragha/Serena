"""Persistent Claude SDK coding owner, separate from the resident brain.

Not admitted by production routes until provider-specific parity gates pass.
The SDK's process handle is inspected only for shared ownership, never replaced
with a second CLI. Missing process identity fails attachment closed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    get_session_info,
    get_session_messages,
)

from core.billing import METERED_AUTH_ENV_VARS, strip_metered_auth_env
from core.workspace_claude_events import ClaudeEvents
from core.workspace_lease import SessionLease


class ClaudeWorkspace:
    def __init__(
        self,
        *,
        session_id,
        cwd,
        publish,
        client_factory=ClaudeSDKClient,
        lease_factory=SessionLease,
        session_info=get_session_info,
        history=get_session_messages,
    ):
        self.session_id, self.cwd, self.publish = session_id, Path(cwd).resolve(), publish
        self.client_factory, self.lease_factory = client_factory, lease_factory
        self.session_info, self.history_reader = session_info, history
        self.state, self.active_turn = "closed", None
        self.client = None
        self.events = ClaudeEvents(session_id)
        self.questions = {}
        self._stop = asyncio.Event()
        self._ready = None
        self._owner_task = None
        self._control = asyncio.Lock()

    async def open(self):
        async with self._control:
            if self._owner_task is not None:
                raise RuntimeError("Claude owner already exists")
            info = await asyncio.to_thread(
                self.session_info, self.session_id, directory=str(self.cwd)
            )
            if info is None or info.session_id != self.session_id:
                raise ValueError("Exact native Claude session is unavailable in this project")
            records = await asyncio.to_thread(
                self.history_reader, self.session_id, directory=str(self.cwd)
            )
            history = self.events.history(records)
            self.state = "opening"
            self._ready = asyncio.get_running_loop().create_future()
            self._owner_task = asyncio.create_task(self._lifetime(history))
            await asyncio.shield(self._ready)

    async def _lifetime(self, history):
        lease, reader = None, None
        try:
            binary = shutil.which("claude")
            if not binary:
                raise RuntimeError("Installed Claude CLI is unavailable")
            lease = self.lease_factory(self.session_id)
            inherited = dict(os.environ)
            clean = strip_metered_auth_env(inherited)
            # SDK overlays its env onto os.environ. Empty blocked values so
            # omission cannot accidentally restore an inherited billing route.
            env = {
                **clean,
                **{
                    key: ""
                    for key in set(METERED_AUTH_ENV_VARS) | (inherited.keys() - clean.keys())
                },
            }
            options = ClaudeAgentOptions(
                cli_path=binary,
                cwd=str(self.cwd),
                resume=self.session_id,
                fork_session=False,
                setting_sources=["user", "project", "local"],
                system_prompt={"type": "preset", "preset": "claude_code"},
                include_partial_messages=True,
                can_use_tool=self._permission,
                env=env,
            )
            self.client = self.client_factory(options=options)
            lease.launching()
            await self.client.connect()
            process = getattr(getattr(self.client, "_transport", None), "_process", None)
            if process is None or not isinstance(process.pid, int):
                raise RuntimeError("Claude SDK did not expose a verifiable owned process")
            lease.bind(process.pid)
            await self.publish(history)
            self.state = "ready"
            reader = asyncio.create_task(self._read())
            self._ready.set_result(None)
            await self._stop.wait()
        except BaseException as error:
            self.state = "unavailable"
            if not self._ready.done():
                self._ready.set_exception(error)
            elif not isinstance(error, asyncio.CancelledError):
                await self.publish({"method": "workspace/error", "params": {"reason": str(error)}})
        finally:
            for future in list(self.questions.values()):
                if not future.done():
                    future.set_result(
                        PermissionResultDeny(message="Session owner closed", interrupt=True)
                    )
            if reader:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            try:
                if self.client:
                    await self.client.disconnect()
            finally:
                if lease:
                    lease.release()

    async def _read(self):
        try:
            async for message in self.client.receive_messages():
                for event in self.events.receive(message):
                    if event["method"] == "turn/completed":
                        self.active_turn = None
                        self.state = "ready"
                    await self.publish(event)
            raise RuntimeError("Claude output stream ended")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.state = "unavailable"
            try:
                await self.publish({"method": "workspace/error", "params": {"reason": str(error)}})
            finally:
                self._stop.set()

    async def submit(self, inputs, *, options=None):
        async with self._control:
            if self.state != "ready":
                raise RuntimeError("Claude is not ready for a new turn")
            if options:
                raise ValueError("Claude per-turn settings are not implemented yet")
            if not isinstance(inputs, list) or not inputs:
                raise ValueError("A message or attachment is required")
            turn_id = str(uuid4())
            self.active_turn = self.events.turn = turn_id
            self.state = "submitting"

            async def message():
                yield {
                    "type": "user",
                    "uuid": turn_id,
                    "session_id": self.session_id,
                    "message": {"role": "user", "content": deepcopy(inputs)},
                    "parent_tool_use_id": None,
                }

            try:
                await self.publish(
                    self.events.event(
                        "turn/started", {"turn": {"id": turn_id, "status": "inProgress"}}
                    )
                )
                await self.publish(
                    self.events.event(
                        "item/started",
                        {
                            "turnId": turn_id,
                            "item": {
                                "id": turn_id,
                                "type": "userMessage",
                                "content": deepcopy(inputs),
                                "status": "sending",
                            },
                        },
                    )
                )
                await self.client.query(message(), session_id=self.session_id)
                await self.publish(
                    self.events.event(
                        "item/completed",
                        {
                            "turnId": turn_id,
                            "item": {
                                "id": turn_id,
                                "type": "userMessage",
                                "content": deepcopy(inputs),
                                "status": "sent",
                            },
                        },
                    )
                )
                if self.active_turn == turn_id:
                    self.state = "running"
                return {"turn": {"id": turn_id}}
            except BaseException:
                self.state = "uncertain"
                raise

    async def interrupt(self):
        if not self.active_turn:
            raise RuntimeError("Claude has no active turn")
        await self.client.interrupt()
        return {}

    async def _permission(self, tool, inputs, context):
        request_id = context.tool_use_id or str(uuid4())
        if request_id in self.questions:
            raise RuntimeError("Duplicate Claude permission request")
        future = asyncio.get_running_loop().create_future()
        self.questions[request_id] = future
        try:
            await self.publish(
                {
                    "id": request_id,
                    "method": "workspace/claudeApproval",
                    "params": {
                        "threadId": self.session_id,
                        "tool": tool,
                        "input": deepcopy(inputs),
                        "title": getattr(context, "title", None),
                        "agentId": getattr(context, "agent_id", None),
                    },
                }
            )
            return await future
        finally:
            self.questions.pop(request_id, None)
            await self.publish(
                self.events.event("serverRequest/resolved", {"requestId": request_id})
            )

    async def answer(self, request_id, answer):
        future = self.questions.get(request_id)
        if future is None or future.done():
            raise ValueError("Claude request is no longer pending")
        if (
            not isinstance(answer, dict)
            or set(answer) != {"decision"}
            or answer["decision"] not in {"allow", "deny"}
        ):
            raise ValueError("An explicit allow or deny decision is required")
        result = (
            PermissionResultAllow()
            if answer["decision"] == "allow"
            else PermissionResultDeny(message="Denied by user")
        )
        future.set_result(result)

    async def close(self):
        self._stop.set()
        if self._owner_task:
            await asyncio.shield(self._owner_task)
        self.state = "closed"
        self.active_turn = None
