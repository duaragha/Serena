"""Python bridge to the public Claude SDK worker; admission/lease stay with owner."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path

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

    async def open(self, *, env=None):
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
            initialized = await self.rpc.request("open", {})
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
                if method == "claude/process":
                    pid = params.get("pid")
                    if type(pid) is not int or self.owned_pid is not None:
                        raise WorkspaceRpcError("Invalid or duplicate native process identity")
                    wrapper = self.rpc.process
                    if wrapper is None or psutil.Process(pid).ppid() != wrapper.pid:
                        raise WorkspaceRpcError("Native process does not belong to this worker")
                    self.owned_pid = pid
                    self.process_ready.set()
                elif method in {"claude/canUseTool", "claude/elicitation"}:
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
        if self.failure or self.closing or self.owned_pid is None:
            raise WorkspaceRpcError(str(self.failure or "Claude transport is not ready"))

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
