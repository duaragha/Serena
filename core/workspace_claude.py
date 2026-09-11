"""Persistent Claude SDK coding owner, separate from the resident brain.

Not admitted by production routes until provider-specific parity gates pass.
The SDK's process handle is inspected only for shared ownership, never replaced
with a second CLI. Missing process identity fails attachment closed.
"""

from __future__ import annotations

import asyncio
import math
import os
import shutil
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

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
        self._session_directory = self.cwd
        self.client_factory, self.lease_factory = client_factory, lease_factory
        self.session_info, self.history_reader = session_info, history
        self.state, self.active_turn = "closed", None
        self.client = None
        self.events = ClaudeEvents(session_id)
        self.questions = {}
        self.question_inputs = {}
        self.question_suggestions = {}
        self.elicitations = {}
        self.model_catalog = None
        self.permission_mode = None
        self._stop = asyncio.Event()
        self._ready = None
        self._owner_task = None
        self._control = asyncio.Lock()
        self._cleanup_complete = True
        self._lease = None
        self._clear_target = None
        self._creation_attempted = False
        self._uncertain_input = None
        self._pending_renames = {}

    async def create(self, *, checkpoint):
        async with self._control:
            if self._owner_task is not None or self._creation_attempted:
                raise RuntimeError("Claude creation was already attempted")
            if not callable(checkpoint) or not self.session_id.startswith("new:"):
                raise ValueError("Creation requires a provisional request and durable checkpoint")
            request_id = self.session_id[4:]
            if str(UUID(request_id)) != request_id:
                raise ValueError("Creation requires an exact request UUID")
            self._creation_attempted = True
            self.session_id = str(uuid4())
            self.events = ClaudeEvents(self.session_id)
            self.state = "opening"
            self._ready = asyncio.get_running_loop().create_future()
            self._owner_task = asyncio.create_task(self._lifetime(self.events.history([]), checkpoint))
            await asyncio.shield(self._ready)

    async def open(self):
        async with self._control:
            if self.session_id.startswith("new:"):
                raise ValueError("New sessions require explicit creation")
            if self._owner_task is not None:
                raise RuntimeError("Claude owner already exists")
            info = await asyncio.to_thread(
                self.session_info, self.session_id, directory=str(self.cwd)
            )
            if info is None or info.session_id != self.session_id:
                raise ValueError("Exact native Claude session is unavailable in this project")
            self._session_directory = Path(getattr(info, "cwd", None) or self.cwd).resolve()
            records = await asyncio.to_thread(
                self.history_reader, self.session_id, directory=str(self.cwd)
            )
            history = self.events.history(records)
            self.state = "opening"
            self._ready = asyncio.get_running_loop().create_future()
            self._owner_task = asyncio.create_task(self._lifetime(history))
            await asyncio.shield(self._ready)

    async def _lifetime(self, history, checkpoint=None):
        reader = None
        try:
            binary = shutil.which("claude")
            if not binary:
                raise RuntimeError("Installed Claude CLI is unavailable")
            self._lease = self.lease_factory(self.session_id)
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
            # Transcript discovery uses its original project, not the latest tool cwd.
            env["SERENA_CLAUDE_SESSION_DIRECTORY"] = str(self._session_directory)
            options = ClaudeAgentOptions(
                cli_path=binary,
                cwd=str(self.cwd),
                resume=self.session_id,
                fork_session=False,
                permission_mode="bypassPermissions",
                setting_sources=["user", "project", "local"],
                system_prompt={"type": "preset", "preset": "claude_code"},
                include_partial_messages=True,
                can_use_tool=self._permission,
                env=env,
            )
            self.client = self.client_factory(options=options)
            if callable(getattr(self.client, "observe_runtime", None)):
                self.client.observe_runtime(self._lease.bind)
            if checkpoint is not None:
                if not callable(getattr(self.client, "create", None)):
                    raise RuntimeError("This Claude client cannot create a reserved native session")
                await checkpoint({"session_id": self.session_id, "provider": "claude", "cwd": str(self.cwd)})
            if hasattr(self.client, "on_elicitation"):
                self.client.on_elicitation = self._elicitation
            self._lease.launching()
            self._cleanup_complete = False
            if checkpoint is None:
                await self.client.connect()
            else:
                await self.client.create()
            pid = getattr(self.client, "owned_pid", None)
            if pid is None:
                process = getattr(getattr(self.client, "_transport", None), "_process", None)
                pid = getattr(process, "pid", None)
            if type(pid) is not int:
                raise RuntimeError("Claude SDK did not expose a verifiable owned process")
            self._lease.bind(pid)
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
            for future, _ in list(self.elicitations.values()):
                if not future.done():
                    future.set_result({"action": "cancel", "content": None})
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
                self._cleanup_complete = True
            finally:
                if self._lease:
                    self._lease.release()

    async def _read(self):
        try:
            async for message in self.client.receive_messages():
                for event in self.events.receive(message):
                    if event["method"] == "turn/completed" and self.state not in {"clearing", "awaiting-handoff", "committing-handoff", "unavailable", "closed"}:
                        self.active_turn = self.events.turn
                        if self.state != "uncertain" or event["params"]["turn"]["id"] == self._uncertain_input:
                            self._uncertain_input = None
                            self.state = "running" if self.active_turn else "ready"
                    await self.publish(event)
                    if event["method"] == "turn/completed":
                        turn = event["params"]["turn"]
                        title = self._pending_renames.pop(turn["id"], None)
                        if title is not None and turn.get("status") == "completed":
                            await self.publish(self.events.event("workspace/renameCompleted", {"title": title}))
            raise RuntimeError("Claude output stream ended")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.state = "unavailable"
            try:
                await self.publish({"method": "workspace/error", "params": {"reason": str(error)}})
            finally:
                self._stop.set()

    async def list_models(self):
        if self.client is None:
            raise RuntimeError("Claude is not attached")
        info = await self.client.get_server_info()
        models = (info or {}).get("models", [])
        if not isinstance(models, list):
            raise ValueError("Claude returned an invalid model catalog")
        self.model_catalog = [
            deepcopy(model)
            for model in models
            if isinstance(model, dict) and isinstance(model.get("value"), str)
        ]
        result = {
            "data": [
                {
                    "id": model["value"],
                    "model": model["value"],
                    "displayName": model.get("displayName", model["value"]),
                    "supportedReasoningEfforts": [{"reasoningEffort": effort} for effort in self._model_efforts(model)],
                    "claudeCapabilities": model,
                }
                for model in self.model_catalog
            ]
        }
        await self.publish(self.events.event("workspace/models", result))
        return result

    @staticmethod
    def _model_efforts(model):
        levels = model.get("supportedEffortLevels")
        if model.get("supportsEffort") is not True or not isinstance(levels, list):
            return []
        return [level for level in levels if isinstance(level, str) and level in {"low", "medium", "high", "xhigh", "max"}]

    async def permissions(self):
        if self.client is None or self.state in {"closed", "opening", "unavailable"}:
            raise RuntimeError("Claude is not attached")
        if self.permission_mode is None:
            info = await self.client.get_server_info()
            mode = (info or {}).get("current_permission_mode")
            if isinstance(mode, str):
                self.permission_mode = mode
        return {"mode": self.permission_mode, "modes": ["default", "acceptEdits", "plan", "dontAsk", "auto", "bypassPermissions"]}

    async def set_permissions(self, mode, confirmed=False):
        async with self._control:
            if self.state != "ready" or self.questions:
                raise RuntimeError("Finish the current Claude turn before changing permission mode")
            if not isinstance(mode, str) or mode not in (await self.permissions())["modes"]:
                raise ValueError("Unsupported Claude permission mode")
            if mode == "bypassPermissions" and confirmed is not True:
                raise ValueError("Bypassing permission prompts requires explicit confirmation")
            await self.client.set_permission_mode(mode)
            self.permission_mode = mode
            await self.publish(self.events.event("workspace/settings", {"permissionMode": mode}))
            return await self.permissions()

    async def account_rate_limits(self):
        if self.state not in {"ready", "running"}:
            raise RuntimeError("Attach Claude before checking account limits")
        return await self.client.get_usage()

    async def context_usage(self):
        if self.client is None or self.state in {"closed", "opening", "unavailable"}:
            raise RuntimeError("Claude is not attached")
        result = await self.client.get_context_usage()
        if not isinstance(result, dict) or any(type(result.get(key)) is not int or result[key] < 0 for key in ("totalTokens", "maxTokens")) or result["maxTokens"] == 0:
            raise ValueError("Claude returned invalid context totals")
        percentage = result.get("percentage")
        if type(percentage) not in {int, float} or not math.isfinite(percentage) or percentage < 0:
            raise ValueError("Claude returned an invalid context percentage")
        if not isinstance(result.get("model"), str) or not isinstance(result.get("categories"), list):
            raise ValueError("Claude returned an invalid context breakdown")
        categories = []
        for category in result["categories"]:
            if not isinstance(category, dict) or not isinstance(category.get("name"), str) or type(category.get("tokens")) is not int or category["tokens"] < 0:
                raise ValueError("Claude returned an invalid context category")
            categories.append({"name": category["name"], "tokens": category["tokens"], "isDeferred": category.get("isDeferred") is True})
        return {**{key: result[key] for key in ("model", "totalTokens", "maxTokens", "percentage")}, "categories": categories}

    async def list_background_tasks(self):
        if self.client is None or self.state in {"closed", "opening", "unavailable"}:
            raise RuntimeError("Claude is not attached")
        return {"data": [
            {"processId": task["id"], "command": task.get("description") or task["id"],
             "cwd": str(self.cwd), "status": task.get("status")}
            for task in self.events.tasks.values()
            if task.get("status") in {"running", "pending", "paused"}
        ]}

    async def terminate_background_task(self, task_id):
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("An exact Claude task ID is required")
        async with self._control:
            current = await self.list_background_tasks()
            if not any(task["processId"] == task_id for task in current["data"]):
                raise ValueError("Task is not running in this Claude session")
            await self.client.stop_task(task_id)
            # The SDK acknowledgement is not completion; native lifecycle events decide.
            current = await self.list_background_tasks()
            pending = any(task["processId"] == task_id for task in current["data"])
            return {"terminated": not pending, "pending": pending}

    async def list_mcp_servers(self):
        if self.client is None or self.state in {"closed", "unavailable"}:
            raise RuntimeError("Claude is not attached")
        response = await self.client.get_mcp_status()
        servers = response.get("mcpServers") if isinstance(response, dict) else None
        if not isinstance(servers, list):
            raise ValueError("Claude returned an invalid MCP status")
        result = {"data": []}
        names = set()
        for server in servers:
            if (
                not isinstance(server, dict)
                or not isinstance(server.get("name"), str)
                or not server["name"]
                or server["name"] in names
                or server.get("status") not in {"connected", "pending", "failed", "needs-auth", "disabled"}
            ):
                raise ValueError("Claude returned an invalid MCP server")
            names.add(server["name"])
            # Configs may contain tokens, headers, or credentials. Do not journal them.
            result["data"].append({"name": server["name"], "status": server["status"]})
        return result

    async def control_mcp_server(self, name, action):
        async with self._control:
            if self.state != "ready":
                raise RuntimeError("Wait for Claude's current turn before changing MCP connections")
            if not isinstance(name, str) or action not in {"reconnect", "enable", "disable"}:
                raise ValueError("An exact MCP server and supported action are required")
            servers = (await self.list_mcp_servers())["data"]
            if name not in {server["name"] for server in servers}:
                raise ValueError("MCP server is not configured in this session")
            if action == "reconnect":
                await self.client.reconnect_mcp_server(name)
            else:
                await self.client.toggle_mcp_server(name, action == "enable")
            return await self.list_mcp_servers()

    async def fork_session(self):
        async with self._control:
            if self.state != "ready":
                raise RuntimeError("Wait for Claude's current turn before forking")
            fork = getattr(self.client, "fork_session", None)
            if fork is None:
                raise RuntimeError("This Claude runtime cannot fork sessions")
            result = await fork()
            sid = result.get("sessionId") if isinstance(result, dict) else None
            if not isinstance(sid, str) or not sid or sid == self.session_id:
                raise ValueError("Native fork did not return a new session identity")
            return {"session_id": sid, "provider": "claude", "cwd": str(self.cwd)}

    async def begin_clear(self, name=None):
        async with self._control:
            if name is not None and (
                not isinstance(name, str)
                or not name
                or name != name.strip()
                or len(name) > 1000
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
            ):
                raise ValueError(
                    "Conversation title must contain at most 1000 characters without control characters"
                )
            if (self.state != "ready" or self.active_turn or self.questions or self.elicitations
                    or any(task.get("status") not in {"completed", "failed", "stopped", "killed"}
                           for task in self.events.tasks.values())):
                raise RuntimeError("Finish active work and interactions before clearing Claude")
            begin = getattr(self.client, "begin_clear", None)
            if begin is None or self._lease is None:
                raise RuntimeError("This Claude runtime cannot transfer session ownership")
            self.state = "clearing"
            try:
                result = await (begin() if name is None else begin(name))
                sid = result.get("sessionId") if isinstance(result, dict) else None
                if not isinstance(sid, str) or str(UUID(sid)) != sid or sid == self.session_id:
                    raise ValueError("Native clear returned an invalid new identity")
                if (self.state != "clearing" or self._stop.is_set() or self.questions or self.elicitations
                        or any(task.get("status") not in {"completed", "failed", "stopped", "killed"}
                               for task in self.events.tasks.values())):
                    raise RuntimeError("Claude is no longer quiescent after draining clear output")
                self._clear_target = sid
                self.state = "awaiting-handoff"
                target = {"session_id": sid, "provider": "claude", "cwd": str(self.cwd)}
                if name is not None:
                    confirmed = result.get("nameConfirmed")
                    name_error = result.get("nameError")
                    if (
                        result.get("requestedName") != name
                        or type(confirmed) is not bool
                        or (confirmed and name_error is not None)
                        or (
                            not confirmed
                            and (
                                not isinstance(name_error, str)
                                or not name_error
                                or len(name_error) > 1000
                                or any(
                                    ord(char) < 32 or ord(char) == 127
                                    for char in name_error
                                )
                            )
                        )
                    ):
                        raise ValueError("Native clear returned an invalid title receipt")
                    target.update(requestedName=name, nameConfirmed=confirmed)
                    if not confirmed:
                        target["nameError"] = name_error
                return target
            except BaseException:
                self.state = "unavailable"
                raise

    async def commit_clear(self, session_id, *, publish):
        """Internal: host must checkpoint and reserve the target before calling."""
        async with self._control:
            if (self.state != "awaiting-handoff" or session_id != self._clear_target
                    or not callable(publish) or self._stop.is_set()):
                raise RuntimeError("Exact pending Claude handoff is required")
            self.state = "committing-handoff"
            try:
                self._lease = self._lease.transfer_after_transition(session_id)
                self.session_id = session_id
                self.events = ClaudeEvents(session_id)
                self.publish = publish
                await self.publish(self.events.history([]))
                result = await self.client.commit_clear(session_id)
                if (result.get("sessionId") != session_id or self.state != "committing-handoff"
                        or self._stop.is_set()):
                    raise RuntimeError("Claude handoff was not confirmed")
                self._clear_target = None
                self.state = "ready"
                return {"session_id": session_id, "provider": "claude", "cwd": str(self.cwd)}
            except BaseException:
                self.state = "unavailable"
                raise

    async def reload_skills(self):
        async with self._control:
            if self.state != "ready":
                raise RuntimeError("Wait for Claude's current turn before reloading skills")
            reload = getattr(self.client, "reload_skills", None)
            if reload is None:
                raise RuntimeError("This Claude runtime cannot reload skills")
            await reload()
            # The initial catalog may include skills removed from disk since attach.
            self.events.capabilities["slash_commands"] = []
            return await self.list_commands()

    async def reload_plugins(self):
        async with self._control:
            if self.state != "ready":
                raise RuntimeError("Wait for Claude's current turn before reloading plugins")
            reload = getattr(self.client, "reload_plugins", None)
            if reload is None:
                raise RuntimeError("This Claude runtime cannot reload plugins")
            result = await reload()
            self.events.capabilities["slash_commands"] = []
            await self.publish(self.events.event("workspace/plugins", deepcopy(result)))
            commands = await self.list_commands()
            return {**result, **commands}

    async def search_files(self, query):
        if self.state in {"closed", "opening", "unavailable"}:
            raise RuntimeError("Claude session is unavailable")
        from core.workspace_files import search_project_files

        return await asyncio.to_thread(search_project_files, self.cwd, query)

    async def list_commands(self):
        if self.client is None or self.state in {"closed", "unavailable"}:
            raise RuntimeError("Claude is not attached")
        info = await self.client.get_server_info()
        commands = (info or {}).get("commands")
        if not isinstance(commands, list):
            raise ValueError("Claude did not advertise a command catalog")
        result = {"data": []}
        names = {item.get("name") for item in commands if isinstance(item, dict)}
        commands = list(commands) + [
            {"name": name}
            for name in self.events.capabilities.get("slash_commands", [])
            if isinstance(name, str) and name not in names
        ]
        for command in commands:
            if not isinstance(command, dict) or not isinstance(command.get("name"), str):
                raise ValueError("Claude returned an invalid command catalog")
            item = deepcopy(command)
            if item["name"] in {"clear", "reset", "new", "fork"}:
                item["workspaceAction"] = "fork" if item["name"] == "fork" else "clear"
            elif item["name"] == "resume":
                item["workspaceAction"] = "resume"
            elif item["name"] in {"reload-plugins", "reload-skills"}:
                item["workspaceAction"] = item["name"]
            elif item["name"] == "color":
                item["workspaceAction"] = "color"
            elif (item["name"] in self.events.capabilities.get("terminal_slash_commands", [])
                  and item["name"] not in self.events.capabilities.get("skills", [])):
                item["unavailableReason"] = "Claude reports this command requires a terminal"
            result["data"].append(item)
        await self.publish(self.events.event("workspace/commands", result))
        return result

    async def diagnostics(self):
        from core.workspace_diagnostics import claude_doctor

        async with self._control:
            if self.state != "ready" or self.client is None:
                raise RuntimeError("Wait for Claude's current turn before checking installation health")
            return await claude_doctor(self.client.options.cli_path, self.cwd, self.client.options.env)

    async def submit(self, inputs, *, options=None):
        return await self._submit(inputs, options=options)

    async def queue_input(self, inputs, *, expected_turn_id):
        if not isinstance(expected_turn_id, str) or not expected_turn_id:
            raise ValueError("Queued input requires the displayed active turn")
        return await self._submit(inputs, expected_turn_id=expected_turn_id)

    async def _submit(self, inputs, *, options=None, expected_turn_id=None):
        async with self._control:
            if expected_turn_id is not None:
                if self.state != "running" or self.active_turn != expected_turn_id:
                    raise RuntimeError("Claude active turn changed; queued input was not sent")
            elif self.state != "ready":
                raise RuntimeError("Claude is not ready for a new turn")
            options = options or {}
            if not isinstance(options, dict) or options.keys() - {"model", "effort"}:
                raise ValueError("Unsupported Claude per-turn settings")
            if not isinstance(inputs, list) or not inputs:
                raise ValueError("A message or attachment is required")
            text = "".join(part.get("text", "") for part in inputs if part.get("type") == "text")
            if text.strip().split(maxsplit=1)[:1] in [
                ["/clear"],
                ["/reset"],
                ["/new"],
                ["/resume"],
                ["/fork"],
            ]:
                raise ValueError("Session switching is not implemented in this pane")
            if "model" in options or "effort" in options:
                if self.model_catalog is None:
                    await self.list_models()
                selected = next((model for model in self.model_catalog if model["value"] == options.get("model")), None)
                if selected is None:
                    raise ValueError("Claude did not advertise this model")
                if "effort" in options and options["effort"] not in self._model_efforts(selected):
                    raise ValueError("Claude did not advertise this effort for the selected model")
                await self.client.set_model(options["model"])
                await self.publish(
                    self.events.event("workspace/settings", {"model": options["model"]})
                )
                if "effort" in options:
                    await self.client.set_effort(options["effort"])
                    await self.publish(self.events.event("workspace/settings", {"reasoningEffort": options["effort"]}))
            turn_id = str(uuid4())
            command = text.strip().split(maxsplit=1)
            if len(command) == 2 and command[0] == "/rename" and all(part.get("type") == "text" for part in inputs):
                title = command[1].strip()
                if title and len(title) <= 1000 and not any(ord(c) < 32 for c in title):
                    self._pending_renames[turn_id] = title
            self.events.begin_input(turn_id)
            self.active_turn = self.events.turn
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
                if self.state == "submitting":
                    self.state = "running" if self.active_turn else "ready"
                return {"turn": {"id": turn_id}}
            except BaseException:
                self._uncertain_input = turn_id
                self.state = "uncertain"
                raise

    async def interrupt(self):
        if not self.active_turn:
            raise RuntimeError("Claude has no active turn")
        await self.client.interrupt()
        for future in list(self.questions.values()):
            if not future.done():
                future.set_result(PermissionResultDeny(message="Cancelled by the user", interrupt=True))
        for future, _ in list(self.elicitations.values()):
            if not future.done():
                future.set_result({"action": "cancel"})
        await asyncio.sleep(0)
        return {}

    async def _permission(self, tool, inputs, context):
        request_id = context.tool_use_id or str(uuid4())
        if request_id in self.questions:
            raise RuntimeError("Duplicate Claude permission request")
        future = asyncio.get_running_loop().create_future()
        self.questions[request_id] = future
        self.question_inputs[request_id] = (tool, deepcopy(inputs))
        suggestions = deepcopy(getattr(context, "suggestions", None) or [])
        self.question_suggestions[request_id] = suggestions
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
                        "suggestions": [update.to_dict() for update in suggestions],
                    },
                }
            )
            return await future
        finally:
            self.questions.pop(request_id, None)
            self.question_inputs.pop(request_id, None)
            self.question_suggestions.pop(request_id, None)
            await self.publish(
                self.events.event("serverRequest/resolved", {"requestId": request_id})
            )

    async def _elicitation(self, request, native_request_id):
        if not isinstance(request, dict):
            raise ValueError("Invalid Claude MCP request")
        request_id = "claude-mcp:" + (str(native_request_id) if native_request_id else str(uuid4()))
        if request_id in self.elicitations:
            raise ValueError("Duplicate Claude MCP request")
        params = deepcopy(request)
        params.setdefault("mode", "form")
        params["threadId"] = self.session_id
        future = asyncio.get_running_loop().create_future()
        self.elicitations[request_id] = (future, params)
        try:
            await self.publish({"id": request_id, "method": "mcpServer/elicitation/request", "params": params})
            return await future
        finally:
            self.elicitations.pop(request_id, None)
            await self.publish(self.events.event("serverRequest/resolved", {"requestId": request_id}))

    async def answer(self, request_id, answer):
        if request_id in self.elicitations:
            from core.workspace_elicitation import validate_reply

            future, params = self.elicitations[request_id]
            if future.done():
                raise ValueError("Claude MCP request is no longer pending")
            validate_reply(params, answer)
            future.set_result(deepcopy(answer))
            return
        future = self.questions.get(request_id)
        if future is None or future.done():
            raise ValueError("Claude request is no longer pending")
        tool, inputs = self.question_inputs[request_id]
        if tool == "AskUserQuestion" and isinstance(answer, dict) and set(answer) == {"answers"}:
            answers = answer["answers"]
            questions = inputs.get("questions", [])
            expected = {q.get("question") for q in questions}
            if (
                not expected
                or None in expected
                or len(expected) != len(questions)
                or not isinstance(answers, dict)
                or set(answers) != expected
                or any(
                    not isinstance(value, str) or not value.strip() for value in answers.values()
                )
            ):
                raise ValueError("Answer every pending Claude question")
            future.set_result(
                PermissionResultAllow(updated_input={**inputs, "answers": deepcopy(answers)})
            )
            return
        if (
            not isinstance(answer, dict)
            or set(answer) not in ({"decision"}, {"decision", "suggestions"})
            or answer["decision"] not in {"allow", "deny"}
        ):
            raise ValueError("An explicit allow or deny decision is required")
        if tool == "AskUserQuestion" and answer["decision"] == "allow":
            raise ValueError("Claude questions require answers, not approval")
        selected = answer.get("suggestions", [])
        suggestions = self.question_suggestions.get(request_id, [])
        if (
            not isinstance(selected, list)
            or any(type(index) is not int or index < 0 or index >= len(suggestions) for index in selected)
            or len(set(selected)) != len(selected)
            or ("suggestions" in answer and (answer["decision"] != "allow" or not selected))
        ):
            raise ValueError("Select only pending permission suggestions with an explicit allow decision")
        result = (
            PermissionResultAllow(updated_input=inputs, updated_permissions=[deepcopy(suggestions[index]) for index in selected] or None)
            if answer["decision"] == "allow"
            else PermissionResultDeny(message="Denied by user")
        )
        future.set_result(result)

    def can_retry_attachment(self):
        return (self.state in {"closed", "unavailable"} and self._cleanup_complete
                and (self._owner_task is None or self._owner_task.done()))

    async def close(self):
        self._stop.set()
        if self._owner_task:
            await asyncio.shield(self._owner_task)
        self.state = "closed"
        self.active_turn = None
