"""Codex rich-client control, separate from the restricted resident brain.

This adapter holds a session lease shared with the PTY registry.
This module has no UI/socket-disconnect lifecycle and never falls back to a new
thread when resumption fails. Raw protocol events are retained for the renderer.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections import deque
from collections.abc import Awaitable, Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

from core.billing import strip_metered_auth_env
from core.workspace_lease import SessionLease
from core.workspace_rpc import WorkspaceRpc, WorkspaceRpcError


class CodexWorkspace:
    def __init__(
        self,
        *,
        session_id: str,
        cwd: Path,
        publish: Callable[[dict], Awaitable[None]],
        rpc: WorkspaceRpc | None = None,
        lease_factory: Callable[[str], SessionLease] = SessionLease,
    ) -> None:
        if not session_id or not cwd.is_dir():
            raise ValueError("A persisted session ID and existing project directory are required")
        self.session_id = session_id
        self.cwd = cwd.resolve()
        self.rpc = rpc or WorkspaceRpc()
        self.publish = publish
        self._lease_factory = lease_factory
        self._lease: SessionLease | None = None
        self.thread: dict | None = None
        self.settings: dict = {}
        self.model_catalog: list[dict] | None = None
        self.active_turn: str | None = None
        self.state = "closed"
        self.questions: dict[int | str, dict] = {}
        self._completed: deque[str] = deque(maxlen=64)
        self._control_lock = asyncio.Lock()
        self._events_task: asyncio.Task | None = None
        self._history_ready = asyncio.Event()

    async def open(self, *, binary: str | None = None, env: dict[str, str] | None = None) -> dict:
        async with self._control_lock:
            if self.state != "closed":
                raise WorkspaceRpcError("Session already opened or awaiting recovery")
            executable = binary or shutil.which("codex")
            if not executable:
                raise WorkspaceRpcError("Codex executable is unavailable")
            self.state = "opening"
            self._history_ready.clear()
            try:
                self._lease = self._lease_factory(self.session_id)
                self._lease.launching()
                await self.rpc.start(
                    [executable, "app-server", "--stdio"],
                    cwd=self.cwd,
                    env=strip_metered_auth_env(dict(os.environ if env is None else env)),
                )
                self._lease.bind(self.rpc.process.pid)
                await self.rpc.request(
                    "initialize",
                    {
                        "clientInfo": {"name": "serena-workspace", "version": "1"},
                        "capabilities": {"experimentalApi": True},
                    },
                )
                await self.rpc.notify("initialized", {})
                self._events_task = asyncio.create_task(self._events())
                # Do not set baseInstructions, disable coding tools, or override
                # the session's configured model and permission policy here.
                result = await self.rpc.request("thread/resume", {"threadId": self.session_id})
                thread = result.get("thread") if isinstance(result, dict) else None
                if not isinstance(thread, dict) or thread.get("id") != self.session_id:
                    raise WorkspaceRpcError(
                        "Codex returned a different session; refusing attachment"
                    )
                self.thread = deepcopy(thread)
                self.settings = {
                    key: deepcopy(result[key])
                    for key in ("model", "reasoningEffort", "serviceTier")
                    if key in result
                }
                for turn in thread.get("turns", []):
                    if turn.get("status") == "inProgress":
                        self.active_turn = turn["id"]
                if self.state == "opening":
                    self.state = "running" if self.active_turn else "ready"
                await self.publish({"method": "workspace/history", "params": deepcopy(result)})
                self._history_ready.set()
                return deepcopy(result)
            except BaseException:
                await self._close()
                raise

    async def permissions(self):
        if self.state in {"closed", "opening", "unavailable"}:
            raise WorkspaceRpcError("Session is not connected")
        profiles, seen, cursors, cursor = [], set(), set(), None
        while True:
            params = {"cwd": str(self.cwd), "limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = await self.rpc.request("permissionProfile/list", params)
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise WorkspaceRpcError("Codex returned an invalid permission catalog")
            for profile in page["data"]:
                if not isinstance(profile, dict) or not isinstance(profile.get("id"), str) or not profile["id"] or type(profile.get("allowed")) is not bool or profile["id"] in seen:
                    raise WorkspaceRpcError("Codex returned an invalid permission profile")
                seen.add(profile["id"])
                profiles.append({"id": profile["id"], "allowed": profile["allowed"], "description": profile.get("description") or ""})
            cursor = page.get("nextCursor")
            if not cursor:
                return {"mode": self.settings.get("permissionProfile"), "modes": [p["id"] for p in profiles], "profiles": profiles}
            if not isinstance(cursor, str) or cursor in cursors or len(cursors) >= 100:
                raise WorkspaceRpcError("Permission catalog pagination did not advance")
            cursors.add(cursor)

    async def set_permissions(self, mode, confirmed=False):
        async with self._control_lock:
            if self.state != "ready" or self.questions:
                raise WorkspaceRpcError("Finish the current Codex turn before changing permissions")
            if confirmed is not True:
                raise ValueError("Changing a permission profile requires explicit confirmation")
            profiles = (await self.permissions())["profiles"]
            if not isinstance(mode, str) or not any(p["id"] == mode and p["allowed"] for p in profiles):
                raise ValueError("Permission profile is unavailable or blocked by policy")
            if self.state != "ready":
                raise WorkspaceRpcError("Session changed during permission lookup")
            result = await self.rpc.request("thread/settings/update", {"threadId": self.session_id, "permissions": mode})
            if not isinstance(result, dict):
                raise WorkspaceRpcError("Permission profile change was not confirmed")
            self.settings["permissionProfile"] = mode
            await self.publish({"method": "workspace/settings", "params": deepcopy(self.settings)})
            return {"mode": mode, "modes": [p["id"] for p in profiles], "profiles": profiles}

    async def list_commands(self) -> dict:
        if self.state in {"closed", "opening", "unavailable"}:
            raise WorkspaceRpcError("Session is not connected")
        result = await self.rpc.request("skills/list", {"cwds": [str(self.cwd)], "forceReload": True})
        pages = result.get("data") if isinstance(result, dict) else None
        if not isinstance(pages, list) or len(pages) != 1 or not isinstance(pages[0], dict) or pages[0].get("cwd") != str(self.cwd):
            raise WorkspaceRpcError("Codex returned skills for a different project")
        page = pages[0]
        if page.get("errors"):
            raise WorkspaceRpcError("Codex could not load every skill; fix the skill errors before selecting")
        if not isinstance(page.get("skills"), list):
            raise WorkspaceRpcError("Codex returned an invalid skill catalog")
        commands, paths = [], set()
        for skill in page["skills"]:
            if not isinstance(skill, dict) or any(not isinstance(skill.get(key), str) or not skill[key] for key in ("name", "path")) or not isinstance(skill.get("enabled"), bool):
                raise WorkspaceRpcError("Codex returned an invalid skill")
            if skill["path"] in paths:
                raise WorkspaceRpcError("Codex returned duplicate skill paths")
            paths.add(skill["path"])
            commands.append({"name": skill["name"], "path": skill["path"], "kind": "skill",
                             "description": skill.get("description", ""),
                             "unavailableReason": "Skill is disabled" if not skill["enabled"] else ""})
        return {"data": commands}

    async def list_mcp_servers(self) -> dict:
        if self.state in {"closed", "opening", "unavailable"}:
            raise WorkspaceRpcError("Session is not connected")
        data, cursors, names, cursor = [], set(), set(), None
        while True:
            params = {"threadId": self.session_id, "limit": 100, "detail": "toolsAndAuthOnly"}
            if cursor:
                params["cursor"] = cursor
            page = await self.rpc.request("mcpServerStatus/list", params)
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise WorkspaceRpcError("Codex returned an invalid MCP inventory")
            for server in page["data"]:
                if (
                    not isinstance(server, dict)
                    or not isinstance(server.get("name"), str)
                    or not server["name"]
                    or server["name"] in names
                    or not isinstance(server.get("tools"), dict)
                ):
                    raise WorkspaceRpcError("Codex returned an invalid MCP server")
                names.add(server["name"])
                status = server.get("runtimeStatus")
                if status not in {None, "notStarted", "starting", "connected", "authenticationRequired", "failed", "cancelled", "disabled"}:
                    raise WorkspaceRpcError("Codex returned an unknown MCP connection state")
                # Auth state or a nonempty tool catalog does not prove a live connection.
                data.append({"name": server["name"], "status": status or "unknown",
                             "authStatus": server.get("authStatus", "unknown"),
                             "toolCount": len(server["tools"])})
            cursor = page.get("nextCursor")
            if not cursor:
                return {"data": data}
            if not isinstance(cursor, str) or cursor in cursors or len(cursors) >= 100:
                raise WorkspaceRpcError("MCP inventory pagination did not advance")
            cursors.add(cursor)

    async def list_background_tasks(self) -> dict:
        if self.state in {"closed", "opening", "unavailable"}:
            raise WorkspaceRpcError("Session is not connected")
        data, cursors, cursor = [], set(), None
        while True:
            params = {"threadId": self.session_id, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = await self.rpc.request("thread/backgroundTerminals/list", params)
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise WorkspaceRpcError("Provider returned an invalid background task list")
            for task in page["data"]:
                if not isinstance(task, dict) or any(
                    not isinstance(task.get(key), str)
                    for key in ("processId", "itemId", "command", "cwd")
                ):
                    raise WorkspaceRpcError("Provider returned an invalid background task")
            data.extend(deepcopy(page["data"]))
            cursor = page.get("nextCursor")
            if not cursor:
                return {"data": data}
            if not isinstance(cursor, str) or cursor in cursors or len(cursors) >= 100:
                raise WorkspaceRpcError("Background task pagination did not advance")
            cursors.add(cursor)

    async def terminate_background_task(self, process_id: str) -> dict:
        if not isinstance(process_id, str) or not process_id or len(process_id) > 256:
            raise ValueError("An exact background process ID is required")
        async with self._control_lock:
            current = await self.list_background_tasks()
            if not any(task["processId"] == process_id for task in current["data"]):
                raise ValueError("Background task is no longer running in this session")
            result = await self.rpc.request(
                "thread/backgroundTerminals/terminate",
                {"threadId": self.session_id, "processId": process_id},
            )
            if not isinstance(result, dict) or not isinstance(result.get("terminated"), bool):
                raise WorkspaceRpcError("Background task termination is unconfirmed")
            return result

    async def list_models(self) -> dict:
        if self.state in {"closed", "opening", "unavailable"}:
            raise WorkspaceRpcError("Session is not connected")
        models, seen, cursor = [], set(), None
        while True:
            params = {"limit": 100, "includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            page = await self.rpc.request("model/list", params)
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise WorkspaceRpcError("Provider returned an invalid model catalog")
            models.extend(deepcopy(page["data"]))
            cursor = page.get("nextCursor")
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in seen or len(seen) >= 100:
                raise WorkspaceRpcError("Provider model pagination did not advance")
            seen.add(cursor)
        self.model_catalog = models
        result = {"data": models, "settings": deepcopy(self.settings)}
        await self.publish({"method": "workspace/models", "params": result})
        return deepcopy(result)

    async def _validate_model_options(self, options):
        if not {"model", "effort", "serviceTier"}.intersection(options):
            return
        if self.model_catalog is None:
            await self.list_models()
        model_id = options.get("model") or self.settings.get("model")
        model = next((m for m in self.model_catalog if m.get("model") == model_id), None)
        if model is None:
            raise ValueError("Selected model is not advertised by this provider")
        if (
            "model" in options
            and options["model"] != self.settings.get("model")
            and "effort" not in options
        ):
            default = model.get("defaultReasoningEffort")
            if not isinstance(default, str):
                raise ValueError("Provider did not advertise a default effort for this model")
            options["effort"] = default
        effort = options.get("effort")
        if effort is not None and effort not in {
            e.get("reasoningEffort") for e in model.get("supportedReasoningEfforts", [])
        }:
            raise ValueError("Selected effort is not supported by this model")
        tier = options.get("serviceTier")
        if tier is not None and tier not in {t.get("id") for t in model.get("serviceTiers", [])}:
            raise ValueError("Selected speed tier is not supported by this model")

    async def _skill_inputs(self, skills):
        if not isinstance(skills, list):
            raise ValueError("Selected skills must be a list")
        if not skills:
            return []
        if len(skills) > 20 or any(not isinstance(path, str) for path in skills) or len(set(skills)) != len(skills):
            raise ValueError("Select distinct skills from the current project")
        catalog = {item["path"]: item for item in (await self.list_commands())["data"] if not item["unavailableReason"]}
        if any(path not in catalog for path in skills):
            raise ValueError("Selected skill is no longer enabled in this project")
        return [{"type": "skill", "name": catalog[path]["name"], "path": path} for path in skills]

    async def submit(self, inputs: list[dict], *, options: dict | None = None) -> dict:
        async with self._control_lock:
            if self.state != "ready":
                raise WorkspaceRpcError("Session is not ready for a new turn")
            if not inputs:
                raise ValueError("A message or attachment is required")
            params = deepcopy(options or {})
            selected = await self._skill_inputs(params.pop("skills", []))
            allowed = {
                "model",
                "effort",
                "serviceTier",
                "approvalPolicy",
                "sandboxPolicy",
                "collaborationMode",
            }
            if params.keys() - allowed:
                raise ValueError("Unsupported turn option")
            await self._validate_model_options(params)
            params.update(threadId=self.session_id, input=deepcopy(inputs) + selected)
            self.state = "submitting"
            try:
                result = await self.rpc.request("turn/start", params)
                turn = result.get("turn") if isinstance(result, dict) else None
                if not isinstance(turn, dict) or not turn.get("id"):
                    raise WorkspaceRpcError("Codex returned no turn identity")
                turn_id = turn["id"]
                if turn_id not in self._completed:
                    self.active_turn = turn_id
                    self.state = "running"
                else:
                    self.active_turn = None
                    self.state = "ready"
                for source, target in (
                    ("model", "model"),
                    ("effort", "reasoningEffort"),
                    ("serviceTier", "serviceTier"),
                ):
                    if source in params:
                        self.settings[target] = params[source]
                await self.publish(
                    {"method": "workspace/settings", "params": deepcopy(self.settings)}
                )
                return result
            except BaseException:
                # A timeout is not proof that Codex rejected the message. Do not
                # allow a retry to create a second turn until state is reconciled.
                if self.state == "submitting":
                    self.state = "uncertain"
                raise

    async def steer(self, inputs: list[dict], *, expected_turn_id: str | None = None, skills=None) -> Any:
        turn_id = self.active_turn
        if not self.active_turn or self.state != "running":
            raise WorkspaceRpcError("No running turn to steer")
        if expected_turn_id is not None and expected_turn_id != self.active_turn:
            raise WorkspaceRpcError("The running turn changed; steering was not sent")
        selected = await self._skill_inputs([] if skills is None else skills)
        if self.active_turn != turn_id or self.state != "running":
            raise WorkspaceRpcError("The running turn changed; steering was not sent")
        return await self.rpc.request(
            "turn/steer",
            {
                "threadId": self.session_id,
                "expectedTurnId": self.active_turn,
                "input": deepcopy(inputs) + selected,
            },
        )

    async def review(self, target: dict) -> dict:
        fields = {
            "uncommittedChanges": set(),
            "baseBranch": {"branch"},
            "commit": {"sha"},
            "custom": {"instructions"},
        }
        if not isinstance(target, dict) or target.get("type") not in fields:
            raise ValueError("Unsupported review target")
        required = fields[target["type"]]
        if set(target) != {"type"} | required or any(
            not isinstance(target.get(key), str) or not target[key].strip() for key in required
        ):
            raise ValueError("Review target fields are incomplete")
        async with self._control_lock:
            if self.state != "ready":
                raise WorkspaceRpcError("Session is not ready for review")
            self.state = "submitting"
            try:
                result = await self.rpc.request(
                    "review/start",
                    {"threadId": self.session_id, "delivery": "inline", "target": deepcopy(target)},
                )
                if result.get("reviewThreadId") != self.session_id or not result.get(
                    "turn", {}
                ).get("id"):
                    raise WorkspaceRpcError("Review did not confirm this exact session and turn")
                turn_id = result["turn"]["id"]
                if turn_id not in self._completed:
                    self.active_turn, self.state = turn_id, "running"
                else:
                    self.active_turn, self.state = None, "ready"
                return result
            except BaseException:
                if self.state == "submitting":
                    self.state = "uncertain"
                raise

    async def compact(self) -> dict:
        async with self._control_lock:
            if self.state != "ready":
                raise WorkspaceRpcError("Session is not ready for compaction")
            self.state = "submitting"
            try:
                await self.publish(
                    {
                        "method": "workspace/activity",
                        "params": {"threadId": self.session_id, "status": "compacting"},
                    }
                )
                return await self.rpc.request("thread/compact/start", {"threadId": self.session_id})
            except BaseException:
                if self.state == "submitting":
                    self.state = "uncertain"
                raise

    async def interrupt(self) -> Any:
        if not self.active_turn:
            raise WorkspaceRpcError("No running turn to interrupt")
        return await self.rpc.request(
            "turn/interrupt",
            {
                "threadId": self.session_id,
                "turnId": self.active_turn,
            },
        )

    async def answer(self, request_id: int | str, answer: dict) -> None:
        question = self.questions.get(request_id)
        if question is None:
            raise WorkspaceRpcError("Question is no longer pending in this session")
        method = question["method"]
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            decisions = {"accept", "acceptForSession", "decline", "cancel"}
            if (
                set(answer) != {"decision"}
                or not isinstance(answer["decision"], str)
                or answer["decision"] not in decisions
            ):
                raise ValueError("Invalid approval decision")
        elif method == "mcpServer/elicitation/request":
            from core.workspace_elicitation import validate_reply

            validate_reply(question["params"], answer)
        elif method == "item/permissions/requestApproval":
            requested = question["params"].get("permissions")
            granted = answer.get("permissions")
            if (
                set(answer) != {"permissions", "scope"}
                or answer["scope"] not in {"turn", "session"}
                or not isinstance(requested, dict)
                or not isinstance(granted, dict)
                or granted.keys() - {"network", "fileSystem"}
            ):
                raise ValueError("Invalid permission grant")
            # Grant only exact requested groups. In particular, never remove a
            # deny entry from a filesystem profile and accidentally widen it.
            for key, value in granted.items():
                if key not in requested or json.dumps(value, sort_keys=True) != json.dumps(
                    requested[key], sort_keys=True
                ):
                    raise ValueError("Permission grant exceeds the requested access")
        elif method == "item/tool/requestUserInput":
            expected = {q["id"] for q in question["params"].get("questions", [])}
            answers = answer.get("answers")
            if (
                set(answer) != {"answers"}
                or not isinstance(answers, dict)
                or set(answers) != expected
            ):
                raise ValueError("Answer must address the pending questions exactly")
            for value in answers.values():
                if (
                    not isinstance(value, dict)
                    or set(value) != {"answers"}
                    or not isinstance(value["answers"], list)
                    or not all(isinstance(a, str) for a in value["answers"])
                ):
                    raise ValueError("Invalid question answer")
        else:
            raise WorkspaceRpcError(f"Response schema not yet implemented for {method}")
        await self.rpc.answer(request_id, answer)
        self.questions.pop(request_id, None)

    async def _events(self) -> None:
        try:
            await self._history_ready.wait()
            while True:
                event = await self.rpc.events.get()
                method, params = event.get("method"), event.get("params") or {}
                thread_id = params.get("threadId")
                if thread_id is not None and thread_id != self.session_id:
                    raise WorkspaceRpcError("Received an event for a different coding session")
                if "id" in event:
                    self.questions[event["id"]] = deepcopy(event)
                if method == "turn/started":
                    self.active_turn = params["turn"]["id"]
                    self.state = "running"
                elif method == "turn/completed":
                    turn_id = params["turn"]["id"]
                    self._completed.append(turn_id)
                    if self.active_turn == turn_id or (
                        self.active_turn is None and self.state not in {"submitting", "uncertain"}
                    ):
                        self.active_turn = None
                        self.state = "ready"
                elif method == "serverRequest/resolved":
                    self.questions.pop(params.get("requestId"), None)
                elif method == "workspace/transportClosed":
                    self.state = "unavailable"
                    self.questions.clear()
                await self.publish(deepcopy(event))
                if method == "workspace/transportClosed":
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.state = "unavailable"
            await self.publish({"method": "workspace/error", "params": {"reason": str(error)}})

    async def _close(self) -> None:
        if self._events_task:
            self._events_task.cancel()
            await asyncio.gather(self._events_task, return_exceptions=True)
            self._events_task = None
        try:
            await self.rpc.close()
        finally:
            if self._lease:
                self._lease.release()
                self._lease = None
        self.state = "closed"
        self.active_turn = None
        self.thread = None
        self.settings = {}
        self.model_catalog = None
        self.questions.clear()
        self._completed.clear()

    async def close(self) -> None:
        """Owner shutdown, not view hide or browser disconnect."""
        async with self._control_lock:
            await self._close()
