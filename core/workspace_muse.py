"""Explicit owner for Muse chats, backed by headless ``muse exec`` turns.

Muse offers no persistent agent server, so unlike the Codex and Claude owners
there is no long-lived child to supervise: each turn spawns one
``muse exec``, streams its JSONL answer into the workspace event protocol the
host already speaks (``turn/started``, ``item/completed``, ``turn/completed``),
and exits. Ownership is guarded per turn with the shared session lease,
because two writers appending to one native transcript is the only shared
mutable state here.

Turns run WITHOUT ``--no-session-log`` and WITH the workspace session id, so
the native transcript the catalog indexes is the one these turns wrote.
Authority matches a full pane: approval never prompts. A failed turn fails
closed with the CLI's own error; nothing is retried or reinterpreted.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import uuid
from contextlib import suppress
from pathlib import Path
from uuid import UUID

from core.billing import strip_metered_auth_env
from core.workspace_lease import SessionLease

MODEL = "muse-spark"
EFFORT = "high"
TURN_TIMEOUT_SECONDS = 1800.0


class MuseWorkspaceError(RuntimeError):
    """A Muse pane could not do what was asked."""


class MuseWorkspace:
    """One Muse chat: native session id, working directory, and turns."""

    def __init__(
        self,
        *,
        session_id: str,
        cwd: Path | str,
        publish,
        binary: str | None = None,
        lease_factory=SessionLease,
    ) -> None:
        if not session_id.startswith("new:") and str(UUID(session_id)) != session_id:
            raise ValueError("Exact Muse session ID required")
        self.session_id = session_id
        self.cwd = Path(cwd).resolve(strict=True)
        self.publish = publish
        self._binary_override = str(binary) if binary else ""
        self._lease_factory = lease_factory
        self._control = asyncio.Lock()
        self._state = "opening"
        self._turn_id: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._model = MODEL
        self._effort = EFFORT
        self.settings = {"model": self._model, "reasoningEffort": self._effort}

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def _unsupported(*args, **kwargs):
            raise MuseWorkspaceError(f"Muse workspace does not support {name} yet")

        return _unsupported

    @property
    def state(self) -> str:
        return self._state

    @property
    def active_turn(self) -> str | None:
        return self._turn_id if self._state == "running" else None

    @property
    def questions(self) -> dict:
        # Headless turns never ask; approval never prompts.
        return {}

    def _binary(self) -> str:
        if self._binary_override:
            return self._binary_override
        found = shutil.which("muse")
        if found:
            return found
        for candidate in (
            Path.home() / ".local" / "bin" / "muse",
            Path("/usr/local/bin/muse"),
            Path("/usr/bin/muse"),
        ):
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)
        raise MuseWorkspaceError("Muse CLI is not installed")

    def _argv(self, prompt: str, *, image_paths: list[str]) -> list[str]:
        argv = [self._binary(), "exec", "--json"]
        if self._model != MODEL:
            argv += ["--model", self._model]
        argv += [
            "--reasoning-effort",
            self._effort,
            "--workspace",
            str(self.cwd),
            "--session-id",
            self.session_id,
            "--approval-mode",
            "never",
        ]
        for path in image_paths:
            argv += ["--image", path]
        argv.append(prompt)
        return argv

    async def create(self, *, checkpoint) -> dict:
        """Mint the native identity and checkpoint it, like every owner."""
        async with self._control:
            if not self.session_id.startswith("new:"):
                raise MuseWorkspaceError("Muse creation was already attempted")
            self._binary()
            native_id = str(uuid.uuid4())
            self.session_id = native_id
            target = {"session_id": native_id, "provider": "muse", "cwd": str(self.cwd)}
            await checkpoint(target)
            with suppress(Exception):
                from core.metadata import set_muse_workspace

                set_muse_workspace(native_id, str(self.cwd))
            self._state = "ready"
            return {"session_id": native_id}

    async def open(self) -> dict:
        """Attach to an existing chat; the transcript stays the authority."""
        async with self._control:
            if self._state not in {"opening", "closed", "unavailable"}:
                raise MuseWorkspaceError("Muse attachment was already attempted")
            self._binary()
            self._state = "ready"
            await self._publish_history_best_effort()
            return {"session_id": self.session_id}

    async def _publish_history_best_effort(self) -> None:
        try:
            from core.muse_scanner import read_turns, transcript_path

            native = transcript_path(self.session_id)
            turns = read_turns(native) if native else []
        except Exception:
            turns = []
        items = []
        for index, turn in enumerate(turns):
            if turn["role"] == "user":
                items.append(
                    {
                        "id": f"history-{index}",
                        "type": "userMessage",
                        "text": turn["text"][:8000],
                    }
                )
            else:
                items.append(
                    {
                        "id": f"history-{index}",
                        "type": "agentMessage",
                        "text": turn["text"][:8000],
                    }
                )
        history = {"id": "history", "status": "completed", "items": items}
        with suppress(Exception):
            await self.publish(
                {
                    "method": "workspace/history",
                    "params": {
                        "thread": {
                            "id": self.session_id,
                            "turns": [history] if items else [],
                        }
                    },
                }
            )

    @staticmethod
    def _prompt_and_images(inputs: list[dict]) -> tuple[str, list[str]]:
        parts: list[str] = []
        images: list[str] = []
        for item in inputs or []:
            if not isinstance(item, dict):
                raise MuseWorkspaceError("Invalid Muse message input")
            kind = item.get("type")
            if kind == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif kind in {"image", "localImage"} and isinstance(item.get("path"), str):
                images.append(item["path"])
            else:
                raise MuseWorkspaceError(f"Unsupported Muse input: {kind}")
        prompt = "\n\n".join(part for part in parts if part.strip()).strip()
        if not prompt:
            raise MuseWorkspaceError("Muse turn requires text")
        return prompt, images

    async def submit(self, inputs: list[dict], *, options: dict | None = None) -> dict:
        from fleet.muse import MuseStream

        async with self._control:
            if options is not None and (
                not isinstance(options, dict) or options.keys() - {"model", "effort"}
            ):
                raise MuseWorkspaceError("Unsupported Muse per-turn settings")
            if self._state != "ready" or self._turn_id is not None:
                raise MuseWorkspaceError("Muse is not ready for input")
            if options:
                if "model" in options:
                    self._model = str(options["model"] or MODEL)
                if "effort" in options:
                    self._effort = str(options["effort"] or EFFORT)
                self.settings = {
                    "model": self._model,
                    "reasoningEffort": self._effort,
                }
            prompt, image_paths = self._prompt_and_images(inputs)
            lease = self._lease_factory(self.session_id)
            try:
                lease.launching()
            except Exception:
                lease.release()
                raise
            turn_id = uuid.uuid4().hex
            item_id = f"{turn_id}-answer"
            self._state = "running"
            self._turn_id = turn_id
            try:
                await self.publish({"method": "turn/started", "params": {"turn": {"id": turn_id}}})
                environment = strip_metered_auth_env(dict(os.environ))
                try:
                    self._process = await asyncio.create_subprocess_exec(
                        *self._argv(prompt, image_paths=image_paths),
                        cwd=str(self.cwd),
                        env=environment,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        start_new_session=True,
                    )
                except OSError as exc:
                    raise MuseWorkspaceError(f"Muse turn could not start: {exc}") from exc
                assert self._process.stdout is not None
                assert self._process.stderr is not None
                with suppress(Exception):
                    lease.bind(self._process.pid)
                stream = MuseStream()
                raw_lines: list[str] = []
                try:
                    async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
                        async for raw in self._process.stdout:
                            try:
                                event = json.loads(raw.decode("utf-8", "replace"))
                            except json.JSONDecodeError:
                                text = raw.decode("utf-8", "replace").strip()
                                if text and len(raw_lines) < 200:
                                    raw_lines.append(text[:4000])
                                continue
                            if isinstance(event, dict):
                                stream.accept(event)
                        stderr = await self._process.stderr.read()
                        returncode = await self._process.wait()
                except (asyncio.TimeoutError, TimeoutError) as exc:
                    raise MuseWorkspaceError("Muse turn timed out") from exc
                output = stream.output
                if not output and not stream.error and returncode == 0 and raw_lines:
                    output = "\n".join(raw_lines)[-32 * 1024 :]
                failure = stream.completion_error(returncode)
                if failure is None and not output:
                    failure = (stderr or b"").decode("utf-8", "replace").strip()[
                        -2000:
                    ] or "Muse completed without a final response"
                if failure:
                    raise MuseWorkspaceError(failure)
                await self.publish(
                    {
                        "method": "item/completed",
                        "params": {
                            "turnId": turn_id,
                            "item": {"id": item_id, "type": "agentMessage", "text": output},
                        },
                    }
                )
                await self.publish(
                    {
                        "method": "turn/completed",
                        "params": {
                            "turn": {
                                "id": turn_id,
                                "status": "completed",
                                "items": [{"id": item_id, "type": "agentMessage", "text": output}],
                            }
                        },
                    }
                )
                return {"turn": {"id": turn_id}}
            except BaseException as error:
                with suppress(Exception):
                    await self.publish(
                        {
                            "method": "turn/completed",
                            "params": {"turn": {"id": turn_id, "status": "failed", "items": []}},
                        }
                    )
                if not isinstance(error, MuseWorkspaceError):
                    raise MuseWorkspaceError(str(error)) from error
                raise
            finally:
                self._kill_process()
                self._turn_id = None
                if self._state == "running":
                    self._state = "ready"
                with suppress(Exception):
                    lease.release()

    def _kill_process(self) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGTERM)

    async def interrupt(self) -> dict:
        async with self._control:
            self._kill_process()
            if self._turn_id is not None:
                turn_id, self._turn_id = self._turn_id, None
                with suppress(Exception):
                    await self.publish(
                        {
                            "method": "turn/completed",
                            "params": {
                                "turn": {
                                    "id": turn_id,
                                    "status": "cancelled",
                                    "items": [],
                                }
                            },
                        }
                    )
            if self._state == "running":
                self._state = "ready"
            return {}

    async def list_models(self) -> dict:
        if self._state not in {"ready", "running"}:
            raise MuseWorkspaceError("Muse is not attached")
        return {
            "data": [
                {
                    "id": MODEL,
                    "model": MODEL,
                    "displayName": "Muse Spark",
                    "supportedReasoningEfforts": [],
                }
            ],
            "settings": {"model": self._model},
        }

    async def list_commands(self) -> dict:
        if self._state not in {"ready", "running"}:
            raise MuseWorkspaceError("Muse is not attached")
        return {"data": []}

    async def list_background_tasks(self) -> dict:
        return {"data": []}

    def can_retry_attachment(self) -> bool:
        return (
            self._state in {"closed", "unavailable"}
            and self._process is None
            and self._turn_id is None
        )

    async def close(self) -> None:
        """Explicit native shutdown; never a disposal callback."""
        async with self._control:
            self._kill_process()
            self._turn_id = None
            self._state = "closed"
