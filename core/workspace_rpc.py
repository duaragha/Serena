"""Bidirectional JSONL transport for the interactive workspace's owned agent process.

This layer never opens a thread, starts a turn, or decides an approval. A
disconnected renderer is not an instruction to close the process.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from collections import deque
from pathlib import Path
from typing import Any


class WorkspaceRpcError(RuntimeError):
    pass


class WorkspaceRpc:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.stderr: deque[str] = deque(maxlen=40)
        self._pending: dict[int, asyncio.Future] = {}
        self._questions: set[int | str] = set()
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._failure: str | None = None
        self._windows_job = None
        self.suspended = False

    async def pause_idle(self) -> bool:
        """Pause a caller-verified idle owner, never a pending RPC.

        The host must additionally exclude active turns, background tasks,
        drafts and reservations. Windows uses the owned, non-breakaway job tree.
        """
        async with self._lifecycle_lock, self._write_lock:
            process = self.process
            if (process is None or process.returncode is not None
                    or self._failure or self._pending or self._questions or not self.events.empty()):
                return False
            if self.suspended:
                return True
            if os.name == "nt":
                if self._windows_job is None:
                    return False
                try:
                    return self._windows_job.suspend()
                finally:
                    self.suspended = self._windows_job.suspended
            # start() creates this dedicated session. Never signal our own group.
            try:
                if os.getpgid(process.pid) != process.pid or process.pid == os.getpgrp():
                    raise WorkspaceRpcError("Provider does not own an isolated process group")
                os.killpg(process.pid, signal.SIGSTOP)
            except ProcessLookupError:
                return False
            self.suspended = True
            return True

    def wake(self) -> None:
        """Wake the same owned process before writes or explicit shutdown."""
        if not self.suspended:
            return
        process = self.process
        if os.name == "nt" and self._windows_job is not None:
            self._windows_job.resume()
        elif process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                if os.getpgid(process.pid) != process.pid or process.pid == os.getpgrp():
                    raise WorkspaceRpcError("Provider process group changed while suspended")
                os.killpg(process.pid, signal.SIGCONT)
        self.suspended = False

    async def start(self, command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
        async with self._lifecycle_lock:
            if self.process is not None:
                raise WorkspaceRpcError(
                    "Transport already owns a process; close it before starting another"
                )
            if not cwd.is_dir():
                raise WorkspaceRpcError("Project directory is unavailable")
            self._failure = None
            self.events = asyncio.Queue()
            self.stderr.clear()
            launch = command
            if os.name == "nt":
                from core.workspace_windows_job import WindowsJob

                self._windows_job = WindowsJob()
                if getattr(sys, "frozen", False):
                    launch = [sys.executable, "--workspace-child"]
                else:
                    launch = [getattr(sys, "_base_executable", sys.executable), "-I", "-S",
                              str(Path(__file__).with_name("workspace_windows_bootstrap.py"))]
            try:
                self.process = await asyncio.create_subprocess_exec(
                    *launch,
                    cwd=str(cwd),
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=os.name != "nt",
                    limit=16 * 1024 * 1024,
                )
                if self._windows_job is not None:
                    self._windows_job.assign(self.process.pid)
                    self.process.stdin.write((json.dumps(command) + "\n").encode())
                    await self.process.stdin.drain()
            except BaseException:
                if self._windows_job is not None:
                    self._windows_job.close()
                    self._windows_job = None
                if self.process is not None:
                    with contextlib.suppress(ProcessLookupError):
                        self.process.kill()
                    await self.process.wait()
                    self.process = None
                raise
            self._tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._stderr())]

    @property
    def windows_gated(self):
        return self._windows_job is not None

    async def request(self, method: str, params: dict, *, timeout: float = 30) -> Any:
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"id": request_id, "method": method, "params": params})
            response = await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)
        if "error" in response:
            raise WorkspaceRpcError(str(response["error"]))
        return response.get("result")

    async def notify(self, method: str, params: dict) -> None:
        await self._send({"method": method, "params": params})

    async def answer(self, request_id: int | str, result: Any) -> None:
        # Validation and consumption share the write lock so two windows cannot
        # answer the same approval. The session layer validates decision schemas.
        async with self._write_lock:
            if request_id not in self._questions:
                raise WorkspaceRpcError("Request is no longer awaiting a response")
            self._questions.remove(request_id)
            await self._write({"id": request_id, "result": result})

    async def _send(self, message: dict) -> None:
        async with self._write_lock:
            await self._write(message)

    async def _write(self, message: dict) -> None:
        process = self.process
        if (
            self._failure
            or process is None
            or process.returncode is not None
            or process.stdin is None
        ):
            raise WorkspaceRpcError(self._failure or "Agent transport is closed")
        try:
            self.wake()
            process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as error:
            raise WorkspaceRpcError("Agent input pipe closed") from error

    async def _read(self) -> None:
        assert self.process and self.process.stdout
        failure = "Agent output pipe closed"
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("Expected a JSON-RPC object")
                if "method" in message:
                    if "id" in message:
                        request_id = message["id"]
                        if type(request_id) not in (int, str) or request_id in self._questions:
                            raise ValueError("Invalid or duplicate server request ID")
                        self._questions.add(request_id)
                    elif message["method"] == "serverRequest/resolved":
                        self._questions.discard((message.get("params") or {}).get("requestId"))
                    await self.events.put(message)
                elif "id" in message:
                    future = self._pending.get(message["id"])
                    if future is not None and not future.done():
                        future.set_result(message)
                else:
                    raise ValueError("Missing JSON-RPC method or ID")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = f"Invalid agent protocol: {type(error).__name__}"
        finally:
            self._failure = failure
            self._questions.clear()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(WorkspaceRpcError(failure))
            await self.events.put(
                {"method": "workspace/transportClosed", "params": {"reason": failure}}
            )

    async def _stderr(self) -> None:
        assert self.process and self.process.stderr
        # Read chunks instead of lines: a noisy child must never deadlock stderr.
        while chunk := await self.process.stderr.read(4096):
            self.stderr.append(chunk.decode(errors="replace"))

    async def close(self) -> None:
        """Explicit owner shutdown, never called for a browser disconnect."""
        async with self._lifecycle_lock:
            process = self.process
            if process is None:
                return
            try:
                self.wake()
            except OSError:
                if self._windows_job is None:
                    raise
                # Explicit owner shutdown must reap children even if wake failed.
                self._windows_job.terminate()
                self.suspended = False
            if process.stdin:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGTERM)
                    elif self._windows_job is not None:
                        self._windows_job.terminate()
                    else:
                        process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        if os.name != "nt":
                            os.killpg(process.pid, signal.SIGKILL)
                        elif self._windows_job is not None:
                            self._windows_job.terminate()
                        else:
                            process.kill()
                    await process.wait()
            # The leader may exit normally while shell/MCP children survive.
            # Only explicit owner close reaches here, never renderer disposal.
            if os.name != "nt":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            if self._windows_job is not None:
                self._windows_job.terminate()
                deadline = asyncio.get_running_loop().time() + 5
                while self._windows_job.active_processes():
                    if asyncio.get_running_loop().time() >= deadline:
                        raise WorkspaceRpcError("Windows worker descendants have not exited")
                    await asyncio.sleep(.01)
                self._windows_job.close()
                self._windows_job = None
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
            self.process = None
