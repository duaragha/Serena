"""A Muse fallback brain for Serena's resident daemon.

Same seat as the Codex fallback: when the Claude session cannot serve a turn,
the daemon asks here instead of going dark. Unlike Codex's persistent
app-server thread this is one headless ``muse exec`` per turn, because that is
the whole non-interactive surface the CLI offers. Cross-turn memory comes from
the daemon's own journal, which records every fallback turn exactly the way it
records Codex ones.

Authority matches the Codex fallback's read-only posture as closely as the CLI
allows: approval never prompts, non-shell writes are disabled, and the managed
sandbox stays on. Billing is subscription-shaped like the other brains: no API
keys are configured or passed, and metered auth is stripped from the child
environment.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import signal
import tempfile
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from core.billing import strip_metered_auth_env
from core.brain_provider import BrainProviderUsageLimit, is_usage_limit_error

MODEL = "muse-spark"
DEFAULT_EFFORT = "high"
TURN_TIMEOUT_SECONDS = 300.0
START_TIMEOUT_SECONDS = 30.0

# `muse exec --help`: Meta reasoning effort, the full accepted set.
MUSE_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})

_IMAGE_SUFFIX = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


class MuseBrainError(RuntimeError):
    """The Muse fallback brain could not serve this turn."""


class MuseBrainClient:
    """One headless Muse turn at a time, behind the Codex-brain interface."""

    def __init__(
        self,
        *,
        cwd: str | Path,
        developer_instructions: str = "",
        binary: str | None = None,
        model: str = MODEL,
        effort: str = DEFAULT_EFFORT,
    ) -> None:
        self.cwd = Path(cwd).expanduser()
        self.instructions = str(developer_instructions or "")
        self._binary_override = str(binary) if binary else ""
        self.model = str(model or MODEL)
        self.effort = str(effort or DEFAULT_EFFORT)
        self.started = False
        self._process: asyncio.subprocess.Process | None = None
        self._turn_lock = asyncio.Lock()

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
        raise MuseBrainError("Muse CLI is not installed")

    def command(self, prompt: str, *, image_paths: list[str] | None = None) -> list[str]:
        """The exact headless argv for one turn, exposed for tests."""

        argv = [self._binary(), "exec", "--json", "--no-session-log"]
        if self.model != MODEL:
            argv += ["--model", self.model]
        argv += [
            "--reasoning-effort",
            self.effort,
            "--workspace",
            str(self.cwd),
            "--approval-mode",
            "never",
            "--disable-write",
        ]
        for path in image_paths or []:
            argv += ["--image", path]
        argv.append(prompt)
        return argv

    def set_route(self, model: object, effort: object) -> None:
        model_name = str(model or "").strip()
        effort_name = str(effort or "").strip()
        if not model_name:
            raise MuseBrainError("Muse brain route requires a model")
        if effort_name not in MUSE_EFFORTS:
            raise MuseBrainError(f"unsupported Muse effort: {effort!r}")
        self.model = model_name
        self.effort = effort_name

    async def start(self) -> None:
        # Fail fast on a missing binary so selection can fall through to the
        # next provider instead of dying mid-turn.
        self._binary()
        if not self.cwd.is_dir():
            raise MuseBrainError(f"Muse brain working directory is missing: {self.cwd}")
        self.started = True

    def _prompt(self, message: str) -> str:
        if self.instructions.strip():
            return f"{self.instructions.strip()}\n\n{message}"
        return message

    def _stage_images(self, images: list[dict[str, str]] | None, directory: str) -> list[str]:
        if not images:
            return []
        paths: list[str] = []
        for index, image in enumerate(images):
            raw = str(image.get("data") or "")
            if not raw:
                raise MuseBrainError("Muse brain image has no data")
            try:
                decoded = base64.b64decode(raw, validate=True)
            except (ValueError, base64.binascii.Error) as exc:
                raise MuseBrainError("Muse brain image is not valid base64") from exc
            suffix = _IMAGE_SUFFIX.get(str(image.get("media_type") or ""), ".png")
            path = str(Path(directory) / f"brain-image-{index}{suffix}")
            Path(path).write_bytes(decoded)
            paths.append(path)
        return paths

    async def turn(
        self,
        message: str,
        *,
        images: list[dict[str, str]] | None = None,
        on_delta=None,
    ) -> dict[str, Any]:
        from fleet.muse import MuseStream

        await self.start()
        if not str(message or "").strip():
            raise MuseBrainError("Muse brain turn requires a message")
        async with self._turn_lock:
            turn_id = uuid.uuid4().hex
            thread_id = str(uuid.uuid4())
            with tempfile.TemporaryDirectory(prefix="muse-brain-") as staged:
                image_paths = self._stage_images(images, staged)
                argv = self.command(self._prompt(message), image_paths=image_paths)
                environment = strip_metered_auth_env(dict(os.environ))
                try:
                    self._process = await asyncio.create_subprocess_exec(
                        *argv,
                        cwd=str(self.cwd),
                        env=environment,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        start_new_session=True,
                    )
                except OSError as exc:
                    raise MuseBrainError(f"Muse brain could not start: {exc}") from exc
                assert self._process.stdout is not None
                assert self._process.stderr is not None
                stream = MuseStream()
                raw_lines: list[str] = []
                try:
                    async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
                        assert self._process.stdout is not None
                        async for raw in self._process.stdout:
                            try:
                                event = json.loads(raw.decode("utf-8", "replace"))
                            except json.JSONDecodeError:
                                text = raw.decode("utf-8", "replace").strip()
                                if text and len(raw_lines) < 200:
                                    raw_lines.append(text[:4000])
                                continue
                            stream.accept(event)
                        stderr = await self._process.stderr.read()
                        returncode = await self._process.wait()
                except (asyncio.TimeoutError, TimeoutError) as exc:
                    self._kill()
                    raise MuseBrainError("Muse brain turn timed out") from exc
                finally:
                    self._process = None
                error_text = (stderr or b"").decode("utf-8", "replace").strip()
                output = stream.output
                if not output and not stream.error and returncode == 0 and raw_lines:
                    output = "\n".join(raw_lines)[-32 * 1024 :]
                failure = stream.completion_error(returncode)
                if failure is None and error_text and not output:
                    failure = error_text[-2000:]
                detail = " ".join(part for part in (output, error_text) if part)
                if is_usage_limit_error(detail) or is_usage_limit_error(failure or ""):
                    raise BrainProviderUsageLimit("muse", detail or failure or "muse")
                if failure:
                    raise MuseBrainError(failure)
                if on_delta is not None:
                    # The CLI answers in batch; there are no token deltas to
                    # stream, so the whole reply arrives as one emission.
                    result = on_delta(output)
                    if asyncio.iscoroutine(result):
                        await result
                return {
                    "text": output,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "tool_calls": [],
                }

    def _kill(self) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGTERM)
        time.sleep(0.2)
        if process.returncode is None:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)

    async def interrupt(self) -> None:
        self._kill()

    async def close(self) -> None:
        self._kill()
        self.started = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "running": self.started and self._process is not None,
            "model": self.model,
            "effort": self.effort,
        }
