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

from core.workspace_codex import CodexWorkspace
from core.workspace_journal import WorkspaceJournal
from core.workspace_uploads import WorkspaceUploads


def _claude_owner(**kwargs):
    # Keep the optional SDK dependency out of ordinary Codex-only startup.
    from core.workspace_claude import ClaudeWorkspace

    return ClaudeWorkspace(**kwargs)


class WorkspaceHost:
    def __init__(self, *, journal: WorkspaceJournal, resolve: Callable, factories=None):
        self.journal = journal
        self.uploads = WorkspaceUploads(journal.path.parent / "workspace-uploads")
        self.resolve = resolve
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

    async def _attach(self, sid):
        async with self._locks.setdefault(sid, asyncio.Lock()):
            if sid in self._sessions:
                return self._status(sid)
            target = await asyncio.to_thread(self.resolve, sid)
            if target.get("session_id") != sid:
                raise ValueError("Resolver returned a different session")
            factory = self.factories.get(target.get("provider"))
            if factory is None:
                raise ValueError("This provider has no verified structured adapter yet")

            async def publish(event):
                decorated = await asyncio.to_thread(self.uploads.decorate_event, sid, event)
                await asyncio.to_thread(self.journal.append, sid, decorated)

            owner = factory(session_id=sid, cwd=Path(target["cwd"]), publish=publish)
            # Reserve before the first awaited provider operation. Repeated
            # requests reuse this owner even if attachment fails ambiguously.
            self._sessions[sid] = (owner, target["provider"])
            try:
                await owner.open()
            except Exception as error:
                await publish({"method": "workspace/error", "params": {"reason": str(error)}})
                return {"ok": False, "session_id": sid, "error": str(error), "state": "unavailable"}
            return self._status(sid)

    def _status(self, sid):
        owner, provider = self._sessions[sid]
        return {
            "ok": owner.state not in {"closed", "unavailable"},
            "session_id": sid,
            "provider": provider,
            "state": owner.state,
            "turn_id": owner.active_turn,
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
            "interrupt",
            "answer",
            "models",
            "review",
            "compact",
            "background_tasks",
            "commands",
            "terminate_background_task",
            "cancel_queued_bridge",
        } or not isinstance(payload, dict):
            raise ValueError("Unsupported workspace control")
        return self._dispatch(self._command(sid, request_id, action, deepcopy(payload)), timeout)

    async def _command(self, sid, request_id, action, payload):
        async with self._locks.setdefault(sid, asyncio.Lock()):
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
            try:
                if action == "cancel_queued_bridge":
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
                elif action == "commands":
                    if provider != "claude" or payload:
                        raise ValueError(
                            "Command discovery requires a Claude session and no payload"
                        )
                    result = await owner.list_commands()
                elif action == "background_tasks":
                    if provider != "codex" or payload:
                        raise ValueError("Background tasks require a Codex session and no payload")
                    result = await owner.list_background_tasks()
                elif action == "terminate_background_task":
                    if provider != "codex" or set(payload) != {"processId"}:
                        raise ValueError("An exact Codex background process is required")
                    result = await owner.terminate_background_task(payload["processId"])
                elif action == "compact":
                    if provider != "codex" or payload:
                        raise ValueError("Compaction requires a Codex session and no payload")
                    result = await owner.compact()
                elif action == "review":
                    if provider != "codex" or set(payload) != {"target"}:
                        raise ValueError("Review requires a Codex target")
                    result = await owner.review(payload["target"])
                elif action == "models":
                    if payload or provider not in {"codex", "claude"}:
                        raise ValueError("Model discovery is unavailable for this request")
                    result = await owner.list_models()
                elif action == "submit":
                    if payload.keys() - {"inputs", "options"}:
                        raise ValueError("Unsupported submit fields")
                    mapper = {
                        "codex": self.uploads.codex_inputs,
                        "claude": self.uploads.claude_inputs,
                    }.get(provider)
                    if mapper is None:
                        raise ValueError("Provider input mapping is not implemented")
                    inputs = await asyncio.to_thread(mapper, sid, payload["inputs"])
                    result = await owner.submit(inputs, options=payload.get("options"))
                elif action == "steer":
                    if (
                        set(payload) != {"inputs", "expectedTurnId"}
                        or not isinstance(payload["expectedTurnId"], str)
                        or not payload["expectedTurnId"]
                    ):
                        raise ValueError("Steering requires inputs and the expected running turn")
                    if provider != "codex":
                        raise ValueError("Provider input mapping is not implemented")
                    inputs = await asyncio.to_thread(
                        self.uploads.codex_inputs, sid, payload["inputs"]
                    )
                    result = await owner.steer(inputs, expected_turn_id=payload["expectedTurnId"])
                elif action == "interrupt":
                    if payload:
                        raise ValueError("Interrupt takes no payload")
                    result = await owner.interrupt()
                else:
                    if set(payload) != {"request_id", "answer"}:
                        raise ValueError("A pending question and answer are required")
                    result = await owner.answer(payload["request_id"], payload["answer"])
                receipt = {"ok": True, "result": result}
            except Exception as error:
                receipt = {"ok": False, "error": str(error)}
            await asyncio.to_thread(self.journal.finish_command, sid, request_id, receipt)
            return receipt

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
