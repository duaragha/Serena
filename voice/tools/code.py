"""Claude Code integration tool for Serena.

Spawns Claude Code CLI as a subprocess and streams its output, allowing
Serena to act as a voice-first coding interface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from pathlib import Path
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

PROJECTS_ROOT = Path.home() / "Documents" / "Projects"


class CodeSession:
    """Manages a running Claude Code CLI subprocess.

    Spawns ``claude --print --output-format stream-json`` in a given project
    directory and streams parsed JSON events from stdout.
    """

    def __init__(self, project_dir: str) -> None:
        self._project_dir = project_dir
        self._process: asyncio.subprocess.Process | None = None

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def run(self, prompt: str) -> AsyncIterator[dict]:
        """Spawn Claude Code and yield parsed JSON events from stdout.

        Each yielded dict corresponds to one line of stream-json output.
        Key event shapes:
            {"type": "assistant", "message": {...}}  -- response with content blocks
            {"type": "result", ...}                  -- final result
        """
        cmd = [
            "claude",
            "--print",
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            "-p", prompt,
        ]

        logger.info(
            "Starting Claude Code in %s: %s",
            self._project_dir,
            " ".join(cmd),
        )

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._project_dir,
        )

        assert self._process.stdout is not None

        try:
            async for raw_line in self._process.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                    yield event
                except json.JSONDecodeError:
                    logger.debug("Non-JSON line from Claude Code: %s", line[:200])
        finally:
            await self._cleanup()

    async def _cleanup(self) -> None:
        """Ensure the subprocess is fully terminated."""
        if self._process is None:
            return

        if self._process.returncode is None:
            try:
                self._process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self._process.kill()
                    await self._process.wait()
            except ProcessLookupError:
                pass

        rc = self._process.returncode
        logger.info("Claude Code process exited with code %s", rc)
        self._process = None

    def cancel(self) -> None:
        """Kill the subprocess immediately."""
        if self._process is not None and self._process.returncode is None:
            logger.info("Cancelling Claude Code subprocess")
            try:
                self._process.kill()
            except ProcessLookupError:
                pass


class CodeTool:
    """Tool protocol implementation that routes coding requests to Claude Code CLI.

    Registered in Serena's tool registry so the brain can invoke it when the
    user asks for any programming task.
    """

    def __init__(self) -> None:
        self.active_project: str | None = None
        self._session: CodeSession | None = None

        # Set after construction via set_narration() once brain/tts/ipc exist.
        self._brain: Any = None
        self._tts: Any = None
        self._ipc: Any = None

    # --- Tool protocol ---

    @property
    def name(self) -> str:
        return "code"

    @property
    def description(self) -> str:
        return (
            "Run Claude Code to write, fix, or refactor code in a project. "
            "Use when the user asks for any programming task."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "What to ask Claude Code to do (e.g. 'add a health check endpoint').",
                },
                "project": {
                    "type": "string",
                    "description": (
                        "Project directory name under ~/Documents/Projects/. "
                        "Omit to use the currently active project."
                    ),
                },
            },
            "required": ["prompt"],
        }

    def set_narration(self, brain: Any, tts: Any, ipc: Any) -> None:
        """Wire up narration dependencies after construction.

        Args:
            brain: ClaudeBrain instance (for generating narration summaries).
            tts: TextToSpeech instance (for speaking narration).
            ipc: IPCServer instance (for broadcasting events to the overlay).
        """
        self._brain = brain
        self._tts = tts
        self._ipc = ipc

    def _resolve_project_dir(self, project: str | None) -> str:
        """Resolve a project name to an absolute directory path.

        Raises:
            ValueError: If no project is specified and none is active,
                        or the resolved path doesn't exist.
        """
        name = project or self.active_project
        if not name:
            raise ValueError(
                "No project specified and no active project set. "
                "Tell me which project to work on first."
            )

        project_dir = PROJECTS_ROOT / name
        if not project_dir.is_dir():
            raise ValueError(
                f"Project directory not found: {project_dir}. "
                f"Available projects: {', '.join(self._list_projects())}"
            )

        return str(project_dir)

    @staticmethod
    def _list_projects() -> list[str]:
        """List project directory names under ~/Documents/Projects/."""
        if not PROJECTS_ROOT.is_dir():
            return []
        return sorted(
            d.name for d in PROJECTS_ROOT.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )

    async def execute(self, prompt: str, project: str | None = None, **_: Any) -> str:
        """Run Claude Code on the given prompt and return a summary.

        If narration is configured, events are narrated via TTS and broadcast
        over IPC as they arrive.
        """
        try:
            project_dir = self._resolve_project_dir(project)
        except ValueError as exc:
            return str(exc)

        session = CodeSession(project_dir)
        self._session = session

        events: list[dict] = []
        files_edited: list[str] = []
        tools_called: list[str] = []
        text_blocks: list[str] = []
        errors: list[str] = []

        # Optionally narrate in parallel
        narrator = None
        if self._brain and self._tts:
            from serena.tools.narrator import CodeNarrator
            narrator = CodeNarrator(self._brain, self._tts, self._ipc)

        try:
            async for event in session.run(prompt):
                events.append(event)

                # Broadcast to overlay
                if self._ipc:
                    try:
                        await self._ipc.broadcast({"type": "code_event", "event": event})
                    except Exception:
                        logger.debug("Failed to broadcast code event", exc_info=True)

                # Extract useful info from events
                self._process_event(event, files_edited, tools_called, text_blocks, errors)

                # Feed to narrator
                if narrator:
                    await narrator.handle_event(event)

            # Final narration
            if narrator:
                await narrator.finalize(files_edited, tools_called, errors)

        except asyncio.CancelledError:
            session.cancel()
            return "Coding session cancelled."
        except Exception:
            logger.exception("Claude Code session failed")
            session.cancel()
            return "Claude Code encountered an error. Check logs for details."
        finally:
            self._session = None

        return self._build_summary(text_blocks, files_edited, tools_called, errors)

    def cancel(self) -> None:
        """Cancel the active coding session."""
        if self._session:
            self._session.cancel()

    @staticmethod
    def _process_event(
        event: dict,
        files_edited: list[str],
        tools_called: list[str],
        text_blocks: list[str],
        errors: list[str],
    ) -> None:
        """Extract structured information from a stream-json event."""
        event_type = event.get("type")

        if event_type == "assistant":
            message = event.get("message", {})
            for block in message.get("content", []):
                block_type = block.get("type")
                if block_type == "text":
                    text_blocks.append(block.get("text", ""))
                elif block_type == "tool_use":
                    tool_name = block.get("name", "unknown")
                    tools_called.append(tool_name)
                    tool_input = block.get("input", {})
                    # Track file edits
                    if tool_name in ("Edit", "Write", "NotebookEdit"):
                        file_path = tool_input.get("file_path", "")
                        if file_path and file_path not in files_edited:
                            files_edited.append(file_path)

        elif event_type == "result":
            # Final result — may contain error info
            if event.get("is_error"):
                errors.append(event.get("error", "unknown error"))

    @staticmethod
    def _build_summary(
        text_blocks: list[str],
        files_edited: list[str],
        tools_called: list[str],
        errors: list[str],
    ) -> str:
        """Build a human-readable summary of the coding session."""
        parts: list[str] = []

        if text_blocks:
            # Use the last text block as the primary result
            parts.append(text_blocks[-1].strip())

        if files_edited:
            parts.append(f"Files edited: {', '.join(Path(f).name for f in files_edited)}")

        if errors:
            parts.append(f"Errors: {'; '.join(errors)}")

        if not parts:
            tool_summary = f"Ran {len(tools_called)} tool calls." if tools_called else ""
            return f"Claude Code session completed. {tool_summary}".strip()

        return "\n\n".join(parts)
