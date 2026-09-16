"""Muse's native MSP session host, translated into Serena workspace events.

Use session/resume, never exec --session-id: an identity is not a history
restore. Wire shapes are verified against Muse 1.3.0's exported schema.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import secrets
import shutil
import time
from contextlib import suppress
from pathlib import Path
from uuid import UUID

from core.billing import strip_metered_auth_env
from core.workspace_lease import SessionLease
from core.workspace_rpc import WorkspaceRpc, WorkspaceRpcError

MODEL = "muse-spark"
EFFORT = "high"
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def command_id() -> str:
    """MSP requires UUIDv7, including on Python versions without uuid.uuid7."""
    return str(UUID(int=(int(time.time() * 1000) << 80) | (7 << 76)
                    | (secrets.randbits(12) << 64) | (2 << 62) | secrets.randbits(62)))


class MuseWorkspaceError(RuntimeError):
    pass


class MuseRpc(WorkspaceRpc):
    async def _write(self, message):
        await super()._write({**message, "jsonrpc": "2.0"})


class MuseWorkspace:
    def __init__(self, *, session_id, cwd, publish, binary=None,
                 lease_factory=SessionLease, rpc_factory=MuseRpc):
        if not session_id.startswith("new:") and str(UUID(session_id)) != session_id:
            raise ValueError("Exact Muse session ID required")
        self.session_id = session_id
        self.cwd = Path(cwd).resolve(strict=True)
        self.publish = publish
        self._binary_override = binary
        self._lease_factory = lease_factory
        self._lease = None
        self.rpc = rpc_factory()
        self._control = asyncio.Lock()
        self._reader = None
        self._poller = None
        self._cursor = None
        self._paged = False
        self._finished_turns = set()
        self._state = "opening"
        self._turn_id = None
        self._items = {}
        self._questions = {}
        self.settings = {"model": MODEL}

    @property
    def state(self):
        return self._state

    @property
    def active_turn(self):
        return self._turn_id

    @property
    def questions(self):
        return self._questions

    def _binary(self):
        found = self._binary_override or shutil.which("muse")
        if found:
            return str(found)
        candidate = Path.home() / ".local/bin/muse"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise MuseWorkspaceError("Muse CLI is not installed")

    async def _request(self, method, **params):
        return await self.rpc.request(method, {"sessionId": self.session_id,
                                              "commandId": command_id(), **params})

    async def create(self, *, checkpoint):
        return await self._open(checkpoint=checkpoint)

    async def open(self):
        return await self._open()

    async def _open(self, checkpoint=None):
        async with self._control:
            if self._state not in {"opening", "closed", "unavailable"}:
                raise MuseWorkspaceError("Muse attachment was already attempted")
            creating = self.session_id.startswith("new:")
            if creating != (checkpoint is not None):
                raise MuseWorkspaceError("Explicit Muse creation required")
            try:
                binary = self._binary()
                if not creating:
                    from core.muse_scanner import transcript_path

                    path = await asyncio.to_thread(transcript_path, self.session_id)
                    if path:
                        await self._paint_messages(path)
                        await self.publish({"method": "workspace/activity", "params": {"status": "connecting"}})
                self._lease = self._lease_factory(self.session_id)
                self._lease.launching()
                await self.rpc.start([binary, "serve"], cwd=self.cwd,
                                     env=strip_metered_auth_env(dict(os.environ)))
                self._lease.bind(self.rpc.process.pid)
                hello = await self.rpc.request("initialize", {
                    "clientInfo": {"name": "serena_workspace", "version": "1"},
                    "capabilities": {"userInputDialogs": True},
                })
                if (hello.get("schema") or {}).get("version") != 1:
                    raise MuseWorkspaceError("Unsupported Muse session protocol version")
                await self.rpc.notify("initialized", {})
                if creating:
                    result = await self.rpc.request("session/start", {
                        "commandId": command_id(), "workspaceRoot": str(self.cwd),
                        "approvalMode": "onRequest",
                    })
                else:
                    try:
                        result = await self._request("session/resume", history="auto")
                    except WorkspaceRpcError as error:
                        if "classify turns: session fork rejected: MalformedJsonl" not in str(error):
                            raise
                        raise MuseWorkspaceError(
                            "Muse could not restore this saved conversation: its native history validator "
                            "rejected a stored turn (MalformedJsonl). Saved messages remain readable, "
                            "but sending is disabled. The original history has not been changed. "
                            "Restarting Serena will not repair this native-history error."
                        ) from error
                session = result["session"]
                sid = session["sessionId"]
                if str(UUID(sid)) != sid or (not creating and sid != self.session_id):
                    raise MuseWorkspaceError("Muse returned a different session")
                if creating:
                    self._lease = self._lease.transfer_after_transition(sid)
                    self.session_id = sid
                    await checkpoint({"session_id": sid, "provider": "muse", "cwd": str(self.cwd)})
                self.settings["model"] = session.get("modelId") or MODEL
                self._turn_id = session.get("activeTurnId")
                self._state = "running" if self._turn_id else "ready"
                await self._history(result)
                self._reader = asyncio.create_task(self._read_events())
                if self._paged:
                    self._poller = asyncio.create_task(self._poll_events())
                return {"session_id": sid}
            except BaseException:
                await self._shutdown()
                self._state = "unavailable"
                raise

    @staticmethod
    def _item(native):
        item = {**native, "id": native["itemId"], "type": native["kind"]}
        kind = native["kind"]
        if kind == "userMessage":
            item["content"] = [{"type": "text", "text": native.get("displayText", native.get("text", ""))}]
        elif kind == "toolCall":
            item["type"] = "acpToolCall"
            try:
                item["input"] = json.loads(native.get("args") or "{}")
            except (ValueError, TypeError):
                item["input"] = {"raw": native.get("args")}
            item["output"] = native.get("visibleOutput", native.get("failureReason"))
        elif kind == "userShell":
            item.update(type="commandExecution", command=native.get("commandText", ""),
                        aggregatedOutput=native.get("visibleOutput", ""))
        elif kind == "compaction":
            item["type"] = "contextCompaction"
        elif kind == "reasoning":
            item["content"] = [native.get("text", "")]
        return item

    async def _history(self, result):
        history = result.get("history") or {}
        snapshot = (history.get("snapshot") or {}).get("state") or {}
        items = history.get("items") or snapshot.get("items") or []
        if history.get("mode") == "none":
            # Older CLI transcripts can load without a materialized live view.
            # MSP explicitly supports point-in-time view/page in that state.
            # First paint the durable messages; then recover the full tool view.
            path = result["session"].get("path")
            if path:
                await self._paint_messages(path)
            recovered = {}
            self._cursor = None
            while True:
                page = await self._page()
                for event in page["events"]:
                    p = event.get("params") or {}
                    if event["method"] in {"item/started", "item/updated", "item/completed"}:
                        recovered[p["item"]["itemId"]] = p["item"]
                if not page.get("nextCursor"):
                    break
            items = list(recovered.values())
            self._paged = True
        else:
            self._cursor = result.get("viewCursor")
        turns = {}
        self._items.clear()
        for native in items:
            self._items[native["itemId"]] = dict(native)
            tid = native.get("turnId") or "history"
            turn = turns.setdefault(tid, {"id": tid, "status": "inProgress" if tid == self._turn_id else "completed", "items": []})
            turn["items"].append(self._item(native))
        if self._turn_id:
            turns.setdefault(self._turn_id, {"id": self._turn_id, "status": "inProgress", "items": []})
        if snapshot.get("reasoningEffort"):
            self.settings["reasoningEffort"] = snapshot["reasoningEffort"]
        await self.publish({"method": "workspace/history", "params": {
            "thread": {"id": self.session_id, "turns": list(turns.values())}, **self.settings,
        }})

    async def _paint_messages(self, path):
        from core.muse_scanner import read_turns

        turns = await asyncio.to_thread(read_turns, path)
        await self.publish({"method": "workspace/history", "params": {
            "thread": {"id": self.session_id, "turns": [{"id": "loading-history", "status": "completed", "items": [
                {"id": f"loading-{n}", "type": "userMessage" if t["role"] == "user" else "agentMessage",
                 "text": t["text"], "content": [{"type": "text", "text": t["text"]}]}
                for n, t in enumerate(turns)]}]}, **self.settings}})

    async def _page(self):
        params = {"sessionId": self.session_id, "limit": 1000}
        if self._cursor:
            params["cursor"] = self._cursor
        page = await self.rpc.request("view/page", params)
        events = page.get("events") or []
        if events:
            cursor = events[-1]["params"]["viewCursor"]
            if cursor == self._cursor:
                raise MuseWorkspaceError("Muse history cursor did not advance")
            self._cursor = cursor
        elif page.get("nextCursor"):
            raise MuseWorkspaceError("Muse returned an empty unfinished history page")
        return page

    async def _poll_events(self):
        try:
            while True:
                await asyncio.sleep(0.5 if self._state == "running" else 2)
                while True:
                    page = await self._page()
                    for event in page["events"]:
                        await self._event(event)
                    if not page.get("nextCursor"):
                        break
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._state = "unavailable"
            await self.publish({"method": "workspace/error", "params": {
                "reason": f"Muse monitoring interrupted: {error}", "activityUnconfirmed": bool(self._turn_id)}})

    async def _read_events(self):
        try:
            while True:
                event = await self.rpc.events.get()
                await self._event(event)
                if event.get("method") == "workspace/transportClosed":
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._state = "unavailable"
            await self.publish({"method": "workspace/error", "params": {"reason": str(error)}})

    async def _event(self, event):
        method = event.get("method")
        p = event.get("params") or {}
        if p.get("sessionId", self.session_id) != self.session_id:
            return
        if method in {"turn/started", "turn/completed"}:
            tid = p["turnId"]
            started = method == "turn/started"
            self._turn_id = tid if started else None
            if not started:
                self._finished_turns.add(tid)
            self._state = "running" if started else "ready"
            await self.publish({"method": method, "params": {"turn": {
                "id": tid, "status": "inProgress" if started else p["terminal"],
                **({"error": p["error"]} if p.get("error") else {}),
            }}})
        elif method in {"item/started", "item/updated", "item/completed"}:
            native = p["item"]
            previous = self._items.get(native["itemId"])
            if previous and previous.get("revision", 0) >= native["revision"]:
                return
            self._items[native["itemId"]] = dict(native)
            await self.publish({"method": "item/started" if native["status"] == "inProgress" else "item/completed",
                                "params": {"turnId": native.get("turnId") or "history", "item": self._item(native)}})
        elif method == "item/delta":
            native = self._items.get(p["itemId"])
            if not native:
                return
            field = p.get("field", "text")
            if field.startswith("summary."):
                index = int(field.split(".")[1])
                if not 0 <= index <= 10000:
                    raise MuseWorkspaceError("Invalid Muse summary index")
                values = native.setdefault("summary", [])
                while len(values) <= index:
                    values.append("")
                values[index] += p["delta"]
            else:
                key = "visibleOutput" if field == "output" else field
                if key not in {"text", "visibleOutput"}:
                    return
                native[key] = native.get(key, "") + p["delta"]
            await self.publish({"method": "item/started", "params": {
                "turnId": native.get("turnId") or "history", "item": self._item(native)}})
        elif method == "session/modelChanged":
            self.settings["model"] = (p.get("model") or {}).get("modelId", self.settings["model"])
            await self.publish({"method": "workspace/settings", "params": self.settings})
        elif method == "workspace/transportClosed":
            self._state = "unavailable"
            await self.publish(event)
            if self._poller:
                self._poller.cancel()
                with suppress(asyncio.CancelledError):
                    await self._poller
                self._poller = None
            await self.rpc.close()
            if self._lease:
                self._lease.release()
                self._lease = None
        elif method == "view/gap":
            result = await self.rpc.request("session/read", {"sessionId": self.session_id, "excludeItems": False})
            await self._history(result)
        elif method in {"approval/request", "approval/requested", "approval/updated"}:
            qid = "approval:" + p["approvalId"]
            self._questions[qid] = p
            await self.publish({"id": qid, "method": "session/request_permission", "params": {
                "toolCall": {"title": p["toolName"], "rawInput": p["subject"]},
                "options": [{"optionId": c["choiceId"], "name": c["label"] +
                             (f" ({c['scope']})" if c.get("scope") else "")} for c in p["availableChoices"]],
            }})
            if "id" in event:
                await self.rpc.respond(event["id"], {})
        elif method in {"userInput/request", "userInput/requested"}:
            qid = "input:" + p["userInputId"]
            self._questions[qid] = p
            await self.publish({"id": qid, "method": "item/tool/requestUserInput", "params": {"questions": p["questions"]}})
            if "id" in event:
                await self.rpc.respond(event["id"], {})
        elif method in {"approval/resolved", "userInput/settled"}:
            qid = "approval:" + p["approvalId"] if method == "approval/resolved" else "input:" + p["userInputId"]
            self._questions.pop(qid, None)
            await self.publish({"method": "serverRequest/resolved", "params": {"requestId": qid}})
        else:
            await self.publish(event)

    async def submit(self, inputs, *, options=None):
        options = options or {}
        if not isinstance(options, dict) or options.keys() - {"model", "effort"}:
            raise MuseWorkspaceError("Unsupported Muse per-turn settings")
        if options.get("effort", EFFORT) not in EFFORTS:
            raise MuseWorkspaceError("Invalid Muse reasoning effort")
        parts = []
        for item in inputs:
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append({"type": "text", "text": item["text"]})
            elif item.get("type") in {"image", "localImage"} and isinstance(item.get("path"), str):
                path = Path(item["path"])
                parts.append({"type": "image", "mediaType": mimetypes.guess_type(path)[0] or "image/png",
                              "base64Data": base64.b64encode(await asyncio.to_thread(path.read_bytes)).decode()})
            else:
                raise MuseWorkspaceError("Unsupported Muse message input")
        if not parts or not any(p.get("text", "").strip() or p["type"] == "image" for p in parts):
            raise MuseWorkspaceError("Muse turn requires input")
        async with self._control:
            if self._state != "ready" or self._turn_id:
                raise MuseWorkspaceError("Muse is not ready for input")
            model = options.get("model")
            if model and model != self.settings["model"]:
                await self._request("session/setModel", model={"modelId": model})
                self.settings["model"] = model
            effort = options.get("effort", self.settings.get("reasoningEffort"))
            self._state = "running"
            try:
                result = await self._request("turn/start", input=parts, **({"reasoningEffort": effort} if effort else {}))
            except Exception:
                self._state = "unavailable"
                raise
            if effort:
                self.settings["reasoningEffort"] = effort
            if result["turnId"] not in self._finished_turns:
                self._turn_id = result["turnId"]
            return {"turn": {"id": result["turnId"]}}

    async def answer(self, request_id, answer):
        question = self._questions.get(request_id)
        if not question or not isinstance(answer, dict):
            raise MuseWorkspaceError("Muse question is no longer pending")
        if request_id.startswith("approval:"):
            outcome = answer.get("outcome") or {}
            if outcome.get("outcome") == "cancelled":
                return await self.interrupt()
            choice = outcome.get("optionId")
            if choice not in {c["choiceId"] for c in question["availableChoices"]}:
                raise MuseWorkspaceError("Invalid Muse approval choice")
            return await self._request("approval/decide", approvalId=question["approvalId"],
                                       choiceId=choice, requirementId=question["currentRequirementId"])
        values = answer.get("answers") or {}
        answers = []
        for q in question["questions"]:
            text = "\n".join((values.get(q["id"]) or {}).get("answers", []))
            if not text.strip() or len(text) > 500:
                raise MuseWorkspaceError("Answer every Muse question (at most 500 characters)")
            answers.append({"questionId": q["id"], "freeText": text})
        return await self._request("userInput/answer", userInputId=question["userInputId"], answers=answers)

    async def interrupt(self):
        if self._turn_id:
            return await self._request("turn/interrupt", turnId=self._turn_id)
        return {}

    async def list_models(self):
        result = await self.rpc.request("model/list", {"sessionId": self.session_id})
        return {"data": [{"id": m["modelId"], "model": m["modelId"], "displayName": m["displayLabel"],
                          "supportedReasoningEfforts": [{"reasoningEffort": e} for e in EFFORTS]}
                         for m in result["models"]], "settings": dict(self.settings)}

    async def list_commands(self):
        return {"data": []}

    async def list_background_tasks(self):
        return {"data": []}

    def can_retry_attachment(self):
        return self._state in {"closed", "unavailable"} and self.rpc.process is None

    async def _shutdown(self):
        for task in (self._reader, self._poller):
            if not task:
                continue
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._reader = self._poller = None
        await self.rpc.close()
        if self._lease:
            self._lease.release()
            self._lease = None

    async def close(self):
        async with self._control:
            await self._shutdown()
            self._turn_id = None
            self._state = "closed"
