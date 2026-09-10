"""Persistent structured-session owners independent of Flask requests and panes.

The app supplies an authoritative resolver which rejects existing PTYs/external
writers before opening. Provider adapters additionally acquire the shared lease.
Nothing launches on construction, reads, polling, or renderer disconnection.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import threading
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from uuid import UUID

from core.workspace_codex import CodexWorkspace
from core.workspace_journal import WorkspaceJournal
from core.workspace_uploads import WorkspaceUploads


def _claude_owner(**kwargs):
    # Keep the optional SDK dependency out of ordinary Codex-only startup.
    from core.workspace_claude import ClaudeWorkspace
    from core.workspace_claude_runtime import client_factory

    return ClaudeWorkspace(client_factory=client_factory(), **kwargs)


class WorkspaceHost:
    def __init__(self, *, journal: WorkspaceJournal, resolve: Callable, factories=None, register_fork=None):
        self.journal = journal
        self.uploads = WorkspaceUploads(journal.path.parent / "workspace-uploads")
        self.resolve = resolve
        self.register_fork = register_fork
        self.factories = (
            factories
            if factories is not None
            else {
                "codex": CodexWorkspace,
                "claude": _claude_owner,
            }
        )
        self._guard = threading.Lock()
        self._loop = None
        self._thread = None
        self._stopped = False
        self._sessions = {}
        self._locks = {}
        self._operations = set()
        self._bridge_queues = {}
        self._bridge_messages = {}
        self._bridge_cancelled = set()

    async def _run(self, coroutine):
        task = asyncio.current_task()
        self._operations.add(task)
        try:
            return await coroutine
        finally:
            self._operations.discard(task)

    def _dispatch(self, coroutine, timeout):
        with self._guard:
            if self._stopped:
                coroutine.close()
                raise RuntimeError("Workspace host is stopped")
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._loop.run_forever, daemon=True, name="workspace-owners"
                )
                self._thread.start()
            future = asyncio.run_coroutine_threadsafe(self._run(coroutine), self._loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            # The caller stopped observing, not the provider. Never cancel the
            # operation or free its session ownership on an HTTP timeout.
            return {"ok": False, "pending": True, "error": "Operation is still pending"}

    def attach(self, session_id: str, *, timeout=35):
        self._validate_session(session_id)
        return self._dispatch(self._attach(session_id), timeout)

    def observe(self, session_id: str):
        """Inspect an existing owner without starting the owner loop or a provider."""
        self._validate_session(session_id)
        with self._guard:
            if self._stopped or self._loop is None:
                return {"observing": False, "session_id": session_id}
            future = asyncio.run_coroutine_threadsafe(self._observe(session_id), self._loop)
        return future.result(timeout=5)

    async def _observe(self, sid):
        entry = self._sessions.get(sid)
        if entry is None or entry[0].state not in {"ready", "running", "completed", "failed", "interrupted"}:
            return {"observing": False, "session_id": sid}
        return {**self._status(sid), "observing": True}

    def decorate_runtime_sessions(self, sessions):
        """Expose owner state for sidebar rows independently of mounted views."""
        with self._guard:
            if self._stopped or self._loop is None:
                return sessions
            future = asyncio.run_coroutine_threadsafe(self._runtime_snapshot(), self._loop)
        states = future.result(timeout=5)
        return [{**row, "workspace_runtime": states[row["session_id"]]}
                if row["session_id"] in states else row for row in sessions]

    async def _runtime_snapshot(self):
        return {sid: self._status(sid) for sid in self._sessions if not sid.startswith("new:")}

    def create(self, request_id: str, provider: str, cwd: str, *, confirmed=False, seed="", timeout=35):
        if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
            raise ValueError("Creation requires an exact request UUID")
        if confirmed is not True or provider not in {"codex", "claude"} or provider not in self.factories:
            raise ValueError("Explicit supported-provider creation is required")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise ValueError("An existing absolute project directory is required")
        if not isinstance(seed, str) or "\0" in seed or len(seed.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Initial context must be text of at most 1 MiB without NUL")
        return self._dispatch(self._create(request_id, provider, str(Path(cwd).resolve()), seed), timeout)

    async def _create(self, request_id, provider, cwd, seed=""):
        reservation = "new:" + request_id
        payload = {"action": "create_session", "payload": {"provider": provider, "cwd": cwd, "confirmed": True}}
        if seed:
            payload["payload"]["seed"] = seed
        async with self._locks.setdefault(reservation, asyncio.Lock()):
            claimed, receipt = await asyncio.to_thread(self.journal.claim_command, reservation, request_id, payload)
            if not claimed:
                return receipt or {"ok": False, "pending": True, "error": "Creation is unconfirmed; it will not be repeated"}
            owner = None
            try:
                async def publish(event):
                    await self._publish(owner.session_id, event)
                owner = self.factories[provider](session_id=reservation, cwd=Path(cwd), publish=publish)
                self._sessions[reservation] = (owner, provider)
                async def checkpoint(target):
                    if target["session_id"] in self._sessions:
                        raise ValueError("Native creation returned an already owned identity")
                    await asyncio.to_thread(self.journal.prepare_creation, request_id, target)
                    self._sessions[target["session_id"]] = (owner, provider)
                    self._sessions.pop(reservation, None)
                await owner.create(checkpoint=checkpoint)
                initial = None
                if seed:
                    # Native completion may arrive during submit. Admit its
                    # transcript for indexing before delivering the first turn.
                    await asyncio.to_thread(self.journal.mark_creation_ready, request_id)
                    initial = await self._command(owner.session_id, "creation-seed:" + request_id, "submit",
                                                  {"inputs": [{"type": "text", "text": seed}]})
                receipt = await asyncio.to_thread(self.journal.complete_creation, request_id, initial)
                self._sessions.pop(reservation, None)
                return receipt
            except Exception as error:
                # The durable claim is deliberately retained even if native
                # creation or its acknowledgement was lost. Never auto-replay it.
                return {"ok": False, "pending": True, "error": str(error)}

    async def _attach(self, sid):
        async with self._locks.setdefault(sid, asyncio.Lock()):
            if sid in self._sessions:
                owner = self._sessions[sid][0]
                retry = getattr(owner, "can_retry_attachment", None)
                if owner.state not in {"closed", "unavailable"} or retry is None or not retry():
                    return self._status(sid)
            if await asyncio.to_thread(self.journal.has_pending_clear, sid):
                raise ValueError("A native clear handoff is unconfirmed; this source cannot be resumed automatically")
            target = await asyncio.to_thread(self.resolve, sid)
            if target.get("session_id") != sid:
                raise ValueError("Resolver returned a different session")
            factory = self.factories.get(target.get("provider"))
            if factory is None:
                raise ValueError("This provider has no verified structured adapter yet")

            async def publish(event):
                await self._publish(sid, event)

            owner = factory(session_id=sid, cwd=Path(target["cwd"]), publish=publish)
            # Reserve before the first awaited provider operation. Repeated
            # requests reuse this owner even if attachment fails ambiguously.
            self._sessions[sid] = (owner, target["provider"])
            try:
                queued = await asyncio.to_thread(self.journal.recoverable_bridge_queue, sid, target["provider"])
                await owner.open()
                await self._restore_bridge_queue(sid, owner, queued)
            except Exception as error:
                await publish({"method": "workspace/error", "params": {"reason": str(error)}})
                return {"ok": False, "session_id": sid, "error": str(error), "state": "unavailable"}
            return self._status(sid)

    async def _restore_bridge_queue(self, sid, owner, requests):
        if self._bridge_queues.get(sid):
            return
        if not requests:
            return
        queue = ["bridge:" + item["id"] for item in requests]
        self._bridge_queues[sid] = queue
        for key, item in zip(queue, requests, strict=True):
            self._bridge_messages[(sid, key)] = item["prompt"]
        await self._publish_bridge_queue(sid)
        for key, item in zip(list(queue), requests, strict=True):
            asyncio.create_task(self._run(self._deliver_bridge(sid, owner, item["prompt"], key)))

    def _status(self, sid):
        owner, provider = self._sessions[sid]
        return {
            "ok": owner.state not in {"closed", "unavailable"},
            "session_id": sid,
            "provider": provider,
            "state": owner.state,
            "turn_id": owner.active_turn,
            **({"error": "Session runtime is unavailable; retry is refused until its cleanup and ownership are confirmed"}
               if owner.state in {"closed", "unavailable"} else {}),
        }

    def events(self, session_id: str, *, after=0):
        self._validate_session(session_id)
        # Reading a journal must never resume a process or create the loop.
        return self.journal.read(session_id, after=after)

    def bridge(self, sid, provider, prompt, request_id, *, timeout=300):
        """Use an already attached owner; None alone permits legacy fallback."""
        self._validate_session(sid)
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("A bridge prompt is required")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
            raise ValueError("A stable bridge request ID is required")
        found, receipt = self.journal.command_receipt(
            sid, f"bridge:{request_id}", {"provider": provider, "prompt": prompt}
        )
        if found:
            return receipt or {
                "ok": False,
                "pending": True,
                "response": "",
                "message": "Prior bridge delivery is unconfirmed; do not resend as a new request",
            }
        if self._loop is None:
            return None
        result = self._dispatch(self._bridge(sid, provider, prompt, request_id), timeout)
        if result is not None and result.get("pending"):
            result.update(
                response="",
                message=result.get(
                    "message", "Bridge is still pending; do not resend as a new request"
                ),
            )
        return result

    async def _bridge(self, sid, provider, prompt, request_id):
        if sid not in self._sessions:
            return None
        key = f"bridge:{request_id}"
        async with self._locks.setdefault(sid, asyncio.Lock()):
            if sid not in self._sessions:
                return None
            owner, actual_provider = self._sessions[sid]
            if provider != actual_provider:
                return {
                    "ok": False,
                    "response": "",
                    "message": "Target belongs to another provider",
                }
            claimed, prior = await asyncio.to_thread(
                self.journal.claim_command, sid, key, {"provider": provider, "prompt": prompt}
            )
            if not claimed:
                return prior or {"ok": False, "pending": True}
            queue = self._bridge_queues.setdefault(sid, [])
            queue.append(key)
            self._bridge_messages[(sid, key)] = prompt
            queued = owner.state in {"running", "submitting"} or len(queue) > 1
            await self._publish_bridge_queue(sid)
        delivery = self._deliver_bridge(sid, owner, prompt, key)
        if queued:
            # A sibling may itself be waiting inside a tool call. Return the
            # queue acknowledgement now, not a mutual wait until both time out.
            asyncio.create_task(self._run(delivery))
            return {
                "ok": True,
                "pending": True,
                "queued": True,
                "message": "Queued behind the current turn; reuse request_id to collect the reply",
            }
        return await delivery

    async def _publish_bridge_queue(self, sid):
        requests = [
            {"id": key.removeprefix("bridge:"), "prompt": self._bridge_messages[(sid, key)]}
            for key in self._bridge_queues.get(sid, [])
        ]
        await asyncio.to_thread(
            self.journal.append,
            sid,
            {
                "method": "workspace/bridgeQueue",
                "params": {"threadId": sid, "count": len(requests), "requests": requests},
            },
        )

    async def _deliver_bridge(self, sid, owner, prompt, key):
        queue = self._bridge_queues[sid]
        try:
            while True:
                async with self._locks[sid]:
                    if (sid, key) in self._bridge_cancelled:
                        raise RuntimeError("Queued bridge cancelled before submission")
                    if self._stopped:
                        raise RuntimeError("Host stopped; queued bridge was not submitted")
                    if owner.state in {"closed", "unavailable", "uncertain", "opening"}:
                        raise RuntimeError("Session unavailable; queued bridge was not submitted")
                    if queue[0] == key and owner.state == "ready":
                        prompt = self._bridge_messages[(sid, key)]
                        queue.remove(key)
                        self._bridge_messages.pop((sid, key), None)
                        await self._publish_bridge_queue(sid)
                        cursor = await asyncio.to_thread(self.journal.latest_sequence, sid)
                        result = await owner.submit([{"type": "text", "text": prompt}])
                        turn_id = result["turn"]["id"]
                        break
                await asyncio.sleep(0.1)
        except Exception as error:
            receipt = {"ok": False, "response": "", "message": str(error)}
            await asyncio.to_thread(self.journal.finish_command, sid, key, receipt)
            return receipt
        finally:
            async with self._locks[sid]:
                if key in queue:
                    queue.remove(key)
                self._bridge_messages.pop((sid, key), None)
                self._bridge_cancelled.discard((sid, key))
                await self._publish_bridge_queue(sid)
        texts = {}
        receipt = None
        while receipt is None:
            if self._stopped:
                receipt = {"ok": False, "message": "Host stopped before bridge completion"}
                break
            page = await asyncio.to_thread(self.journal.read, sid, after=cursor)
            for envelope in page["events"]:
                event = envelope["event"]
                method, params = event["method"], event.get("params", {})
                if method in {"workspace/error", "workspace/transportClosed"}:
                    receipt = {"ok": False, "message": params.get("reason", "Session unavailable")}
                    break
                if params.get("turnId") == turn_id:
                    if method == "item/agentMessage/delta":
                        item_id = params["itemId"]
                        texts[item_id] = texts.get(item_id, "") + params.get("delta", "")
                    elif method == "item/completed" and params.get("item", {}).get("type") in {
                        "agentMessage",
                        "commandOutput",
                    }:
                        item = params["item"]
                        texts[item["id"]] = item.get("text", "")
                turn = params.get("turn") or {}
                if method == "turn/completed" and turn.get("id") == turn_id:
                    for item in turn.get("items", []):
                        if item.get("type") in {"agentMessage", "commandOutput"}:
                            texts[item["id"]] = item.get("text", "")
                    receipt = {
                        "ok": turn.get("status") == "completed",
                        "message": f"finished ({turn.get('status', 'unknown')})",
                    }
                    break
            cursor = page["cursor"]
            if receipt is None and not page["has_more"]:
                await asyncio.sleep(0.1)
        receipt.update(response="\n\n".join(texts.values()), session_id=sid, turn_id=turn_id)
        await asyncio.to_thread(self.journal.finish_command, sid, key, receipt)
        return receipt

    def command(self, sid: str, request_id: str, action: str, payload: dict, *, timeout=35):
        self._validate_session(sid)
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError("A stable request ID is required")
        if action not in {
            "submit",
            "steer",
            "queue_input",
            "interrupt",
            "answer",
            "models",
            "session_modes",
            "set_session_mode",
            "review",
            "compact",
            "background_tasks",
            "commands",
            "reload_skills",
            "set_skill_enabled",
            "reload_plugins",
            "diagnostics",
            "account_status",
            "account_login",
            "account_login_cancel",
            "search_files",
            "load_earlier",
            "shell_command",
            "fork_session",
            "clear_session",
            "disconnect_session",
            "register_fork",
            "context_usage",
            "permissions",
            "set_permissions",
            "mcp_servers",
            "mcp_server_control",
            "mcp_login",
            "mcp_reload",
            "set_mcp_enabled",
            "terminate_background_task",
            "cancel_queued_bridge",
            "edit_queued_bridge",
        } or not isinstance(payload, dict):
            raise ValueError("Unsupported workspace control")
        return self._dispatch(self._command(sid, request_id, action, deepcopy(payload)), timeout)

    async def _command(self, sid, request_id, action, payload):
        async with self._locks.setdefault(sid, asyncio.Lock()):
            if action == "clear_session":
                found, prior = await asyncio.to_thread(self.journal.command_receipt, sid, request_id,
                                                       {"action": action, "payload": payload})
                if found:
                    return prior or {"ok": False, "uncertain": True,
                                     "error": "Clear outcome is unconfirmed; it will not be repeated"}
            if sid not in self._sessions:
                raise ValueError("Explicitly attach this session before sending controls")
            recorded_payload = payload
            if action == "answer":
                recorded_payload = {
                    "sha256": hashlib.sha256(
                        json.dumps(payload, sort_keys=True, allow_nan=False).encode()
                    ).hexdigest()
                }
            claimed, result = await asyncio.to_thread(
                self.journal.claim_command,
                sid,
                request_id,
                {"action": action, "payload": recorded_payload},
            )
            if not claimed:
                if result is None and action == "fork_session" and not payload and self._sessions[sid][1] in {"claude", "codex"}:
                    target = await asyncio.to_thread(self.journal.fork_checkpoint, sid, request_id)
                    if target is not None:
                        receipt = {"ok": True, "result": await self._register_created_fork(target)}
                        await asyncio.to_thread(self.journal.finish_command, sid, request_id, receipt)
                        return receipt
                return (
                    result
                    if result is not None
                    else {
                        "ok": False,
                        "uncertain": True,
                        "error": "Prior delivery has no confirmed result; it will not be repeated",
                    }
                )
            owner, provider = self._sessions[sid]
            retryable = False
            try:
                if action == "edit_queued_bridge":
                    if set(payload) != {"request_id", "prompt", "expected_prompt"} or any(not isinstance(payload[field], str) for field in payload) or not payload["prompt"].strip():
                        raise ValueError("An exact queued message, original text and non-empty replacement are required")
                    key = f"bridge:{payload['request_id']}"
                    if key not in self._bridge_queues.get(sid, []):
                        raise ValueError("Message is no longer queued; running turns are not changed")
                    if self._bridge_messages[(sid, key)] != payload["expected_prompt"]:
                        raise ValueError("Queued message changed; reopen it before editing")
                    self._bridge_messages[(sid, key)] = payload["prompt"]
                    try:
                        await self._publish_bridge_queue(sid)
                    except BaseException:
                        self._bridge_messages[(sid, key)] = payload["expected_prompt"]
                        raise
                    result = {"edited": True}
                elif action == "cancel_queued_bridge":
                    if set(payload) != {"request_id"} or not isinstance(payload["request_id"], str):
                        raise ValueError("An exact queued bridge ID is required")
                    key = f"bridge:{payload['request_id']}"
                    queue = self._bridge_queues.get(sid, [])
                    if key not in queue:
                        raise ValueError(
                            "Message is no longer queued; running turns are not cancelled"
                        )
                    self._bridge_cancelled.add((sid, key))
                    queue.remove(key)
                    self._bridge_messages.pop((sid, key), None)
                    await self._publish_bridge_queue(sid)
                    result = {"cancelled": True}
                elif action == "permissions":
                    if provider not in {"codex", "claude"} or payload:
                        raise ValueError("Permission modes require a supported session and no payload")
                    result = await owner.permissions()
                elif action == "set_permissions":
                    if provider not in {"codex", "claude"} or set(payload) != {"mode", "confirmed"} or type(payload["confirmed"]) is not bool:
                        raise ValueError("An explicit native permission mode is required")
                    result = await owner.set_permissions(payload["mode"], payload["confirmed"])
                elif action == "context_usage":
                    if provider != "claude" or payload:
                        raise ValueError("Context breakdown requires a Claude session and no payload")
                    result = await owner.context_usage()
                elif action == "mcp_servers":
                    if provider not in {"codex", "claude"} or payload:
                        raise ValueError("MCP discovery requires a supported session and no payload")
                    result = await owner.list_mcp_servers()
                elif action == "set_skill_enabled":
                    if provider != "codex" or set(payload) != {"path", "enabled"}:
                        raise ValueError("An exact Codex skill path and enabled state are required")
                    if owner.state != "ready":
                        retryable = True
                        raise ValueError("Finish the current Codex turn before changing skills")
                    result = await owner.set_skill_enabled(payload["path"], payload["enabled"])
                elif action == "set_mcp_enabled":
                    if provider != "codex" or set(payload) != {"name", "enabled"} or type(payload["enabled"]) is not bool:
                        raise ValueError("An exact Codex MCP server and boolean enabled state are required")
                    if owner.state != "ready":
                        retryable = True
                        raise ValueError("Finish the current Codex turn before changing MCP settings")
                    result = await owner.set_mcp_enabled(payload["name"], payload["enabled"])
                elif action == "mcp_server_control":
                    if provider != "claude" or set(payload) != {"name", "action"}:
                        raise ValueError("An exact Claude MCP server and action are required")
                    result = await owner.control_mcp_server(payload["name"], payload["action"])
                elif action in {"mcp_login", "mcp_reload"}:
                    if provider != "codex" or set(payload) != ({"name"} if action == "mcp_login" else set()):
                        raise ValueError("An exact Codex MCP action is required")
                    if owner.state != "ready":
                        retryable = True
                        raise ValueError("Finish the current Codex turn before changing MCP connections")
                    result = await owner.login_mcp(payload["name"]) if action == "mcp_login" else await owner.reload_mcp()
                elif action == "register_fork":
                    if provider not in {"claude", "codex"} or set(payload) != {"fork_request_id"} or not isinstance(payload["fork_request_id"], str) or self.register_fork is None:
                        raise ValueError("An exact fork creation receipt is required")
                    found, prior = await asyncio.to_thread(self.journal.command_receipt, sid,
                                                          payload["fork_request_id"], {"action": "fork_session", "payload": {}})
                    if not found or not prior or not prior.get("ok"):
                        raise ValueError("Fork creation is not confirmed for this session")
                    target = prior["result"]
                    result = {key: target[key] for key in ("session_id", "provider", "cwd")}
                    retryable = True  # Registration is idempotent and cannot create a native fork.
                    await asyncio.to_thread(self.register_fork, result)
                    result["indexed"] = True
                elif action == "disconnect_session":
                    if payload != {"confirmed": True} or type(payload.get("confirmed")) is not bool:
                        raise ValueError("Explicit session disconnect confirmation is required")
                    if provider not in {"claude", "codex"}:
                        raise ValueError("Safe disconnect is unavailable: this provider cannot confirm background task completion. Closing the view keeps the session running.")
                    retryable = True
                    if owner.state != "ready" or owner.active_turn or self._bridge_queues.get(sid):
                        raise ValueError("Finish active and queued work before disconnecting")
                    tasks = await owner.list_background_tasks()
                    if not isinstance(tasks, dict) or tasks.get("data") != []:
                        raise ValueError("Stop background tasks before disconnecting")
                    native_tasks = getattr(getattr(owner, "events", None), "tasks", {})
                    if any(task.get("status") not in {"completed", "failed", "stopped"} for task in native_tasks.values()):
                        raise ValueError("Background task completion is unconfirmed")
                    if owner.state != "ready" or owner.active_turn or getattr(owner, "questions", {}) or getattr(owner, "elicitations", {}):
                        raise ValueError("Session became active; disconnect was not performed")
                    retryable = False
                    await owner.close()
                    if not owner.can_retry_attachment():
                        raise ValueError("Runtime cleanup is unconfirmed")
                    await self._publish(sid, {"method": "workspace/transportClosed", "params": {"reason": "Session disconnected"}})
                    result = {"disconnected": True, "session_id": sid}
                elif action == "clear_session":
                    if provider != "claude" or payload != {"confirmed": True} or type(payload.get("confirmed")) is not bool:
                        raise ValueError("Explicit confirmation for a Claude session clear is required")
                    if owner.state != "ready" or self._bridge_queues.get(sid):
                        retryable = True
                        raise ValueError("Finish active and queued work before clearing")
                    return await self._clear_session(sid, request_id, owner)
                elif action == "fork_session":
                    if provider not in {"claude", "codex"} or payload or self.register_fork is None:
                        raise ValueError("Native fork requires a supported session, no payload and an available catalog")
                    if owner.state != "ready":
                        retryable = True
                        raise ValueError("Wait for the current turn before forking")
                    result = await owner.fork_session()
                    await asyncio.to_thread(self.journal.append, sid, {
                        "method": "workspace/sessionForked", "params": {"threadId": sid, "requestId": request_id, "fork": result}
                    })
                    result = await self._register_created_fork(result)
                elif action == "shell_command":
                    if provider != "codex" or set(payload) != {"command", "confirmed"}:
                        raise ValueError("An explicit Codex shell command is required")
                    result = await owner.shell_command(payload["command"], payload["confirmed"])
                elif action == "load_earlier":
                    if provider != "codex" or set(payload) != {"cursor"} or not isinstance(payload["cursor"], str):
                        raise ValueError("An exact Codex history cursor is required")
                    # Reading cannot start work. The owner revalidates its cursor
                    # before publishing, so a confirmed failure can be retried.
                    retryable = True
                    result = await owner.load_earlier(payload["cursor"])
                elif action == "search_files":
                    if provider not in {"claude", "codex"} or set(payload) != {"query"}:
                        raise ValueError("File search requires a supported session and query")
                    retryable = True
                    result = await owner.search_files(payload["query"])
                elif action == "reload_plugins":
                    if provider != "claude" or payload:
                        raise ValueError("Plugin reload requires a Claude session and no payload")
                    if owner.state != "ready":
                        retryable = True
                        raise ValueError("Wait for Claude's current turn before reloading plugins")
                    result = await owner.reload_plugins()
                elif action in {"account_login", "account_login_cancel"}:
                    expected = {"loginId"} if action == "account_login_cancel" else set()
                    if provider != "codex" or set(payload) != expected:
                        raise ValueError("Browser login requires a Codex session and exact payload")
                    result = (await owner.login_account() if action == "account_login"
                              else await owner.cancel_account_login(payload["loginId"]))
                elif action == "account_status":
                    if provider != "codex" or payload:
                        raise ValueError("Account status requires a Codex session and no payload")
                    retryable = True
                    result = await owner.account_status()
                elif action == "diagnostics":
                    if provider != "claude" or payload:
                        raise ValueError("Installation diagnostics requires a Claude session and no payload")
                    retryable = True
                    result = await owner.diagnostics()
                elif action == "reload_skills":
                    if provider != "claude" or payload:
                        raise ValueError("Skill reload requires a Claude session and no payload")
                    result = await owner.reload_skills()
                elif action == "commands":
                    if provider not in {"claude", "codex", "gemini"} or payload:
                        raise ValueError(
                            "Command discovery requires a supported session and no payload"
                        )
                    result = await owner.list_commands()
                elif action == "background_tasks":
                    if provider not in {"codex", "claude"} or payload:
                        raise ValueError("Background tasks require a supported session and no payload")
                    result = await owner.list_background_tasks()
                elif action == "terminate_background_task":
                    if provider not in {"codex", "claude"} or set(payload) != {"processId"}:
                        raise ValueError("An exact native background task is required")
                    result = await owner.terminate_background_task(payload["processId"])
                elif action == "compact":
                    if provider != "codex" or payload:
                        raise ValueError("Compaction requires a Codex session and no payload")
                    result = await owner.compact()
                elif action == "review":
                    if provider != "codex" or set(payload) != {"target"}:
                        raise ValueError("Review requires a Codex target")
                    result = await owner.review(payload["target"])
                elif action == "session_modes":
                    if provider != "gemini" or payload:
                        raise ValueError("Native session modes are unavailable")
                    result = await owner.list_session_modes()
                elif action == "set_session_mode":
                    if provider != "gemini" or set(payload) != {"mode"} or not isinstance(payload["mode"], str):
                        raise ValueError("Invalid native session mode")
                    result = await owner.set_session_mode(payload["mode"])
                elif action == "models":
                    if payload or provider not in {"codex", "claude", "gemini"}:
                        raise ValueError("Model discovery is unavailable for this request")
                    result = await owner.list_models()
                elif action == "submit":
                    if payload.keys() - {"inputs", "options"}:
                        raise ValueError("Unsupported submit fields")
                    mapper = {
                        "codex": self.uploads.codex_inputs,
                        "claude": self.uploads.claude_inputs,
                        "gemini": self.uploads.acp_inputs,
                    }.get(provider)
                    if mapper is None:
                        raise ValueError("Provider input mapping is not implemented")
                    inputs = await asyncio.to_thread(mapper, sid, payload["inputs"])
                    result = await owner.submit(inputs, options=payload.get("options"))
                elif action == "queue_input":
                    retryable = True
                    if (provider != "claude" or set(payload) != {"inputs", "expectedTurnId"}
                            or not isinstance(payload["expectedTurnId"], str) or not payload["expectedTurnId"]):
                        raise ValueError("Claude queued input requires inputs and the expected active turn")
                    inputs = await asyncio.to_thread(self.uploads.claude_inputs, sid, payload["inputs"])
                    if owner.state != "running" or owner.active_turn != payload["expectedTurnId"]:
                        raise ValueError("Claude active turn changed; queued input was not sent")
                    retryable = False
                    result = await owner.queue_input(inputs, expected_turn_id=payload["expectedTurnId"])
                elif action == "steer":
                    if (
                        set(payload) - {"inputs", "expectedTurnId", "skills"}
                        or not {"inputs", "expectedTurnId"} <= payload.keys()
                        or not isinstance(payload["expectedTurnId"], str)
                        or not payload["expectedTurnId"]
                    ):
                        raise ValueError("Steering requires inputs and the expected running turn")
                    if provider != "codex":
                        raise ValueError("Provider input mapping is not implemented")
                    inputs = await asyncio.to_thread(
                        self.uploads.codex_inputs, sid, payload["inputs"]
                    )
                    kwargs = {"expected_turn_id": payload["expectedTurnId"]}
                    if "skills" in payload:
                        kwargs["skills"] = payload["skills"]
                    result = await owner.steer(inputs, **kwargs)
                elif action == "interrupt":
                    if payload and (set(payload) != {"expectedTurnId"} or not isinstance(payload["expectedTurnId"], str) or not payload["expectedTurnId"]):
                        raise ValueError("Interrupt requires an exact expected turn ID")
                    if payload and payload["expectedTurnId"] != owner.active_turn:
                        raise ValueError("Displayed turn is no longer active; current turn was not interrupted")
                    result = await owner.interrupt()
                else:
                    if set(payload) != {"request_id", "answer"}:
                        raise ValueError("A pending question and answer are required")
                    result = await owner.answer(payload["request_id"], payload["answer"])
                receipt = {"ok": True, "result": result}
            except Exception as error:
                receipt = {"ok": False, "error": str(error)}
                if retryable:
                    receipt["retryable"] = True
            await asyncio.to_thread(self.journal.finish_command, sid, request_id, receipt)
            return receipt

    async def _publish(self, sid, event):
        decorated = await asyncio.to_thread(self.uploads.decorate_event, sid, event)
        await asyncio.to_thread(self.journal.append, sid, decorated)
        if event.get("method") != "turn/completed" or self.register_fork is None:
            return
        target = await asyncio.to_thread(self.journal.pending_target, sid)
        if not target or not target["committed"]:
            return
        # Clear creates an identity before a transcript. Only later native
        # completion may materialize it; indexing failure must not lose output.
        registration = {key: target[key] for key in ("session_id", "provider", "cwd")}
        prompt_id = event.get("params", {}).get("turn", {}).get("providerOriginal", {}).get("user_message_uuid")
        if prompt_id:
            registration["prompt_id"] = prompt_id
        for attempt in range(5):
            result = await self._register_created_fork(registration)
            if not result.get("retryable"):
                break
            if attempt < 4:
                await asyncio.sleep(0.05 * (2 ** attempt))
        if result["indexed"]:
            await asyncio.to_thread(self.journal.mark_target_cataloged, sid)
        await asyncio.to_thread(self.journal.append, sid, {"method": "workspace/catalog", "params": result})

    async def _register_created_fork(self, target):
        try:
            if self.register_fork is None:
                raise RuntimeError("Fork catalog is unavailable")
            await asyncio.to_thread(self.register_fork, target)
            return {**target, "indexed": True}
        except Exception as error:
            from core.workspace_catalog import NativeTranscriptPending

            # Creation already happened, including after an interrupted receipt.
            return {**target, "indexed": False, "error": str(error),
                    **({"retryable": True} if isinstance(error, NativeTranscriptPending) else {})}

    async def _clear_session(self, sid, request_id, owner):
        transitioned = False
        try:
            target = await owner.begin_clear()
            transitioned = True
            await asyncio.to_thread(self.journal.prepare_clear, sid, request_id, target)
            new_sid = target["session_id"]
            if new_sid in self._sessions:
                raise ValueError("Clear target already has a workspace owner")
            async with self._locks.setdefault(new_sid, asyncio.Lock()):
                if new_sid in self._sessions:
                    raise ValueError("Clear target already has a workspace owner")

                async def publish(event):
                    await self._publish(new_sid, event)

                # Reserve before acknowledgement; never retain an old-ID alias
                # that could send source-chat input into the new conversation.
                self._sessions[new_sid] = (owner, "claude")
                del self._sessions[sid]
                await owner.commit_clear(new_sid, publish=publish)
                receipt = await asyncio.to_thread(self.journal.complete_clear, sid, request_id)
                return receipt
        except BaseException:
            if transitioned or owner.state != "ready":
                owner.state = "unavailable"
            raise

    def describe_pending_session(self, sid):
        """Read-only catalog fallback; never claims a transcript exists yet."""
        target = self.journal.pending_target(sid)
        if target is None:
            return None
        return {"session_id": sid, "agent": target["provider"], "cwd": target["cwd"],
                "title": f"New {target['provider'].title()} conversation", "native_persistence_pending": True}

    def delete_pending_session(self, sid, *, source):
        """Explicit deletion only; retain journal history and a recovery manifest."""
        from uuid import uuid4

        from core import indexer, metadata
        from core.workspace_catalog import NativeTranscriptPending
        from core.workspace_lease import SessionLease

        target = self.journal.pending_target(sid)
        if not target or not target["committed"]:
            return None
        lease = SessionLease(sid)
        try:
            target = self.journal.pending_target(sid)
            if not target or not target["committed"]:
                return None
            if self.register_fork is None:
                raise ValueError("Native session catalog is unavailable")
            native = {key: target[key] for key in ("session_id", "provider", "cwd")}
            try:
                self.register_fork(native)
            except NativeTranscriptPending:
                recovery = self.journal.path.parent / "deleted-workspaces" / f"{sid}-{uuid4()}"
                recovery.mkdir(parents=True, mode=0o700)
                (recovery / "recovery.json").write_text(json.dumps({
                    "session_id": sid, "target": native, "deleted_via": source,
                    "metadata": metadata.get_meta(sid), "journal": str(self.journal.path),
                    "native_transcript_present": False,
                }, indent=2) + "\n", encoding="utf-8")
                result = str(recovery)
            else:
                with indexer._index_update_lock():
                    indexed = indexer.get_session(sid)
                    if indexed is None:
                        raise ValueError("Native registration did not produce the exact session")
                    result = indexer._delete_unowned_session(indexed, source=source)
            self.journal.mark_target_cataloged(sid)
            metadata.delete_meta(sid)
            return result
        finally:
            lease.release()

    def include_pending_sessions(self, sessions, *, projects=()):
        from core.config import claude_project_dir_for

        result = list(sessions)
        seen = {session["session_id"] for session in sessions}
        for target in reversed(self.journal.uncataloged_targets()):
            sid = target["session_id"]
            if sid in seen:
                self.journal.mark_target_cataloged(sid)
                continue
            project = claude_project_dir_for(target["cwd"])
            if projects and not any(value in project for value in projects):
                continue
            title = f"New {target['provider'].title()} conversation"
            result.insert(0, {"session_id": sid, "agent": target["provider"], "cwd": target["cwd"],
                              "project_dir": project, "display_title": title,
                              "title": title, "created_at": target["created_at"],
                              "last_timestamp": target["created_at"], "native_persistence_pending": True})
        return result

    @staticmethod
    def _validate_session(sid):
        if not isinstance(sid, str) or not sid or len(sid) > 200:
            raise ValueError("An exact session ID is required")

    def shutdown(self):
        """Explicit host shutdown only. Never call from a view close handler."""
        with self._guard:
            self._stopped = True
            loop, thread = self._loop, self._thread
        if loop is None or loop.is_closed():
            return

        async def close_owners():
            # An attach that was already admitted must finish before taking the
            # owner snapshot, otherwise it could launch after shutdown's sweep.
            await asyncio.gather(*list(self._operations), return_exceptions=True)
            await asyncio.gather(*(owner.close() for owner, _ in self._sessions.values()))
            await loop.shutdown_default_executor()

        asyncio.run_coroutine_threadsafe(close_owners(), loop).result(15)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        if thread.is_alive():
            raise RuntimeError("Workspace loop did not stop")
        loop.close()
