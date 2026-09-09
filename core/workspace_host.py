"""Persistent structured-session owners independent of Flask requests and panes.

The app supplies an authoritative resolver which rejects existing PTYs/external
writers before opening. Provider adapters additionally acquire the shared lease.
Nothing launches on construction, reads, polling, or renderer disconnection.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

from core.workspace_codex import CodexWorkspace
from core.workspace_journal import WorkspaceJournal
from core.workspace_uploads import WorkspaceUploads


class WorkspaceHost:
    def __init__(self, *, journal: WorkspaceJournal, resolve: Callable, factories=None):
        self.journal = journal
        self.uploads = WorkspaceUploads(journal.path.parent / "workspace-uploads")
        self.resolve = resolve
        self.factories = factories if factories is not None else {"codex": CodexWorkspace}
        self._guard = threading.Lock()
        self._loop = None
        self._thread = None
        self._stopped = False
        self._sessions = {}
        self._locks = {}
        self._operations = set()

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
                await asyncio.to_thread(self.journal.append, sid, event)

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

    def command(self, sid: str, request_id: str, action: str, payload: dict, *, timeout=35):
        self._validate_session(sid)
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError("A stable request ID is required")
        if action not in {"submit", "steer", "interrupt", "answer", "models"} or not isinstance(
            payload, dict
        ):
            raise ValueError("Unsupported workspace control")
        return self._dispatch(self._command(sid, request_id, action, deepcopy(payload)), timeout)

    async def _command(self, sid, request_id, action, payload):
        async with self._locks.setdefault(sid, asyncio.Lock()):
            if sid not in self._sessions:
                raise ValueError("Explicitly attach this session before sending controls")
            claimed, result = await asyncio.to_thread(
                self.journal.claim_command, sid, request_id, {"action": action, "payload": payload}
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
                if action == "models":
                    if payload or provider != "codex":
                        raise ValueError("Model discovery is unavailable for this request")
                    result = await owner.list_models()
                elif action == "submit":
                    if payload.keys() - {"inputs", "options"}:
                        raise ValueError("Unsupported submit fields")
                    if provider != "codex":
                        raise ValueError("Provider input mapping is not implemented")
                    inputs = await asyncio.to_thread(
                        self.uploads.codex_inputs, sid, payload["inputs"]
                    )
                    result = await owner.submit(inputs, options=payload.get("options"))
                elif action == "steer":
                    if set(payload) != {"inputs"}:
                        raise ValueError("Steering requires inputs only")
                    if provider != "codex":
                        raise ValueError("Provider input mapping is not implemented")
                    inputs = await asyncio.to_thread(
                        self.uploads.codex_inputs, sid, payload["inputs"]
                    )
                    result = await owner.steer(inputs)
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
