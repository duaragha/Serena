"""Python bridge to the public Claude SDK worker; admission/lease stay with owner."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path
from uuid import UUID

import psutil

from core.billing import METERED_AUTH_ENV_VARS, strip_metered_auth_env
from core.workspace_rpc import WorkspaceRpc, WorkspaceRpcError


class ClaudeSdkTransport:
    def __init__(self, *, session_id, cwd, sdk_path, cli_path, node_path,
                 publish, request, rpc_factory=WorkspaceRpc):
        self.session_id = session_id
        self.cwd = Path(cwd).resolve()
        self.command = [str(node_path), str(Path(__file__).with_name("workspace_claude_worker.mjs")),
                        str(sdk_path), str(cli_path), session_id, str(self.cwd)]
        self.publish, self.request = publish, request
        self.rpc = rpc_factory()
        self.owned_pid = None
        self.started = False
        self.reader = None
        self.questions = {}
        self.failure = None
        self.closing = False
        self.process_ready = asyncio.Event()
        self.transition_pending = False
        self.transition_target = None
        self.transition_committing = False

    async def open(self, *, env=None):
        return await self._open(env=env, method="open")

    async def create(self, *, env=None):
        if not isinstance(self.session_id, str) or str(UUID(self.session_id)) != self.session_id:
            raise ValueError("Native creation requires a reserved UUID")
        return await self._open(env=env, method="create")

    async def _open(self, *, env=None, method):
        if self.started or self.closing:
            raise WorkspaceRpcError("Claude transport cannot be started twice")
        self.started = True
        inherited = dict(os.environ if env is None else env)
        clean = strip_metered_auth_env(inherited)
        clean.update({key: "" for key in set(METERED_AUTH_ENV_VARS) | (inherited.keys() - clean.keys())})
        if sys.platform.startswith("linux"):
            roots = [Path(value) for value in (clean.get("APPDIR"), getattr(sys, "_MEIPASS", None)) if value]
            if roots or "LD_LIBRARY_PATH_ORIG" in clean:
                original = clean.get("LD_LIBRARY_PATH_ORIG", clean.get("LD_LIBRARY_PATH", ""))
                paths = [value for value in original.split(os.pathsep)
                         if value and not any(Path(value).is_relative_to(root) for root in roots)]
                if paths:
                    clean["LD_LIBRARY_PATH"] = os.pathsep.join(paths)
                else:
                    clean.pop("LD_LIBRARY_PATH", None)
                clean.pop("LD_LIBRARY_PATH_ORIG", None)
        if clean.get("SERENA_WORKSPACE_NODE_MODE") == "electron":
            clean["ELECTRON_RUN_AS_NODE"] = "1"
        else:
            clean.pop("ELECTRON_RUN_AS_NODE", None)
        await self.rpc.start(self.command, cwd=self.cwd, env=clean)
        self.reader = asyncio.create_task(self._read())
        try:
            initialized = await self.rpc.request(method, {})
            # The process notification precedes the open response on stdout,
            # but its consumer runs in a separate task. Drain that event first.
            await asyncio.wait_for(self.process_ready.wait(), 3)
            if self.failure:
                raise self.failure
            if self.owned_pid is None:
                raise WorkspaceRpcError("Claude did not identify its native process")
            return initialized
        except BaseException:
            await self.close()
            raise

    async def _read(self):
        try:
            while True:
                event = await self.rpc.events.get()
                method = event["method"]
                params = event.get("params") or {}
                if method == "_local_barrier" and isinstance(event.get("future"), asyncio.Future):
                    if not event["future"].done():
                        event["future"].set_result(None)
                elif method == "claude/process":
                    pid = params.get("pid")
                    if type(pid) is not int or self.owned_pid is not None:
                        raise WorkspaceRpcError("Invalid or duplicate native process identity")
                    wrapper = self.rpc.process
                    parent = psutil.Process(pid).ppid()
                    # Windows owns a gated bootstrap above the Node SDK worker.
                    # Require that exact extra hop, not any arbitrary descendant.
                    if getattr(self.rpc, "windows_gated", False):
                        parent = psutil.Process(parent).ppid()
                    if wrapper is None or parent != wrapper.pid:
                        raise WorkspaceRpcError("Native process does not belong to this worker")
                    self.owned_pid = pid
                    self.process_ready.set()
                elif method in {"claude/canUseTool", "claude/elicitation"}:
                    if self.transition_pending:
                        raise WorkspaceRpcError("Unexpected interaction during session handoff")
                    request_id = event["id"]
                    if request_id in self.questions:
                        raise WorkspaceRpcError("Duplicate interactive request")
                    task = asyncio.create_task(self._answer(event))
                    self.questions[request_id] = task
                    task.add_done_callback(lambda _, key=request_id: self.questions.pop(key, None))
                elif method == "serverRequest/resolved":
                    task = self.questions.get(params.get("requestId"))
                    if task:
                        task.cancel()
                elif method == "claude/message":
                    message = params["message"]
                    if message.get("session_id") and message["session_id"] != self.session_id:
                        raise WorkspaceRpcError("Native output crossed session identity")
                    await self.publish(message)
                elif method in {"workspace/error", "workspace/transportClosed"}:
                    raise WorkspaceRpcError(params.get("reason", "Claude worker closed"))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.failure = error
            self.process_ready.set()
            if not self.closing:
                await self.publish({"type": "transport_error", "error": str(error)})

    async def _answer(self, event):
        try:
            answer = await self.request(event["method"], event["params"])
            await self.rpc.answer(event["id"], answer)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.failure = error
            await self.publish({"type": "transport_error", "error": str(error)})

    def _ready(self):
        if self.failure or self.closing or self.owned_pid is None or self.transition_pending:
            raise WorkspaceRpcError(str(self.failure or "Claude transport is not ready"))

    async def _drain_events(self):
        # RPC replies and event delivery have separate consumers.
        barrier = asyncio.get_running_loop().create_future()
        await self.rpc.events.put({"method": "_local_barrier", "future": barrier})
        try:
            await asyncio.wait({barrier, self.reader}, return_when=asyncio.FIRST_COMPLETED)
            if self.failure or self.closing or not barrier.done():
                raise WorkspaceRpcError("Output did not drain before handoff")
        finally:
            barrier.cancel()

    async def begin_clear(self, name=None):
        self._ready()
        if self.questions:
            raise WorkspaceRpcError("Resolve pending interactions before clearing")
        if name is not None and not isinstance(name, str):
            raise WorkspaceRpcError("Conversation title must be text")
        self.transition_pending = True
        try:
            result = await self.rpc.request(
                "begin_clear", {} if name is None else {"name": name}
            )
            sid = result.get("sessionId") if isinstance(result, dict) else None
            if not isinstance(sid, str) or str(UUID(sid)) != sid or sid == self.session_id:
                raise WorkspaceRpcError("Native clear returned an invalid session identity")
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
                            or any(ord(char) < 32 or ord(char) == 127 for char in name_error)
                        )
                    )
                ):
                    raise WorkspaceRpcError("Native clear returned an invalid title receipt")
            self.transition_target = sid
            if self.failure or self.closing:
                raise WorkspaceRpcError("Transport closed during clear")
            await self._drain_events()
            return {
                "sessionId": sid,
                **(
                    {
                        "requestedName": name,
                        "nameConfirmed": result["nameConfirmed"],
                        **(
                            {"nameError": result["nameError"]}
                            if result["nameConfirmed"] is False
                            else {}
                        ),
                    }
                    if name is not None
                    else {}
                ),
            }
        except asyncio.CancelledError:
            self.failure = WorkspaceRpcError("Clear outcome is unconfirmed after cancellation")
            raise
        except Exception as error:
            self.failure = WorkspaceRpcError(f"Clear outcome is unconfirmed: {error}")
            raise self.failure from error

    async def commit_clear(self, session_id):
        if (self.failure or self.closing or self.transition_committing or not self.transition_pending
                or self.transition_target is None or session_id != self.transition_target):
            raise WorkspaceRpcError("Exact pending session handoff is required")
        # The worker emits buffered new-session events before its acknowledgement.
        # Install their identity first, but keep all input blocked until the ack.
        self.session_id = session_id
        self.transition_committing = True
        try:
            result = await self.rpc.request("commit_clear", {"sessionId": session_id})
            if not isinstance(result, dict) or result.get("sessionId") != session_id:
                raise WorkspaceRpcError("Native session handoff was not confirmed")
            if self.failure or self.closing:
                raise self.failure or WorkspaceRpcError("Transport closed during handoff")
            await self._drain_events()
            self.transition_pending = False
            self.transition_target = None
            self.transition_committing = False
            return result
        except asyncio.CancelledError:
            self.failure = WorkspaceRpcError("Session handoff is unconfirmed after cancellation")
            raise
        except Exception as error:
            self.failure = WorkspaceRpcError(f"Session handoff is unconfirmed: {error}")
            raise self.failure from error

    async def send(self, message):
        self._ready()
        if message.get("session_id") != self.session_id:
            raise WorkspaceRpcError("Input must target the exact session")
        return await self.rpc.request("send", {"message": message})

    async def control(self, method, *args):
        self._ready()
        return await self.rpc.request("control", {"method": method, "args": list(args)})

    async def close(self):
        self.closing = True
        for task in list(self.questions.values()):
            task.cancel()
        await asyncio.gather(*self.questions.values(), return_exceptions=True)
        if self.rpc.process is not None:
            with contextlib.suppress(WorkspaceRpcError, TimeoutError):
                await self.rpc.request("close", {}, timeout=6)
            # EOF is the second shutdown path. An orphan keeps the host's native
            # PID lease occupied even if the wrapper itself must be killed.
            await self.rpc.close()
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
