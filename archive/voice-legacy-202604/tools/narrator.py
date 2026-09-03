"""Narration engine for Claude Code sessions.

Summarizes Claude Code's progress as it works, speaking updates via TTS
so the user hears what's happening without needing to watch the screen.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from serena.brain.claude import ClaudeBrain
    from serena.ipc.server import IPCServer
    from serena.voice.tts import TextToSpeech

logger = logging.getLogger(__name__)

# Minimum seconds between spoken narrations to avoid spamming.
_NARRATION_INTERVAL = 5.0

# Tool calls that are too boring to narrate.
_SKIP_TOOLS = frozenset({"Read", "Glob", "Grep", "ToolSearch", "ListMcpResourcesTool"})

# Tools that indicate a file edit.
_EDIT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})

# Tools that run shell commands.
_BASH_TOOLS = frozenset({"Bash"})


class CodeNarrator:
    """Consumes Claude Code stream events and narrates progress via TTS.

    Batches events into ~5-second windows, generates a one-sentence spoken
    summary for each batch via the brain, and speaks it. Also broadcasts
    raw events to the IPC overlay.
    """

    def __init__(
        self,
        brain: ClaudeBrain,
        tts: TextToSpeech,
        ipc: IPCServer | None = None,
    ) -> None:
        self._brain = brain
        self._tts = tts
        self._ipc = ipc

        # Batching state
        self._pending_actions: list[str] = []
        self._last_narration_time: float = 0.0

        # Session-level tracking
        self._files_edited: set[str] = set()
        self._tools_called: list[str] = []
        self._errors: list[str] = []
        self._bash_outputs: list[str] = []

    async def handle_event(self, event: dict) -> None:
        """Process a single stream-json event and narrate if due.

        Call this for every event yielded by CodeSession.run().
        """
        action = self._extract_action(event)
        if action:
            self._pending_actions.append(action)

        # Check if it's time to narrate
        now = time.monotonic()
        if (
            self._pending_actions
            and (now - self._last_narration_time) >= _NARRATION_INTERVAL
        ):
            await self._flush_narration()

    async def finalize(
        self,
        files_edited: list[str],
        tools_called: list[str],
        errors: list[str],
    ) -> None:
        """Speak a final completion summary after the session ends."""
        # Flush any remaining pending actions first
        if self._pending_actions:
            await self._flush_narration()

        summary = self._build_final_summary(files_edited, tools_called, errors)
        logger.info("Final narration: %s", summary)
        try:
            await self._tts.speak(summary)
        except Exception:
            logger.exception("Failed to speak final narration")

    def _extract_action(self, event: dict) -> str | None:
        """Extract a narration-worthy action description from an event.

        Returns None for events that shouldn't be narrated (e.g. file reads).
        """
        event_type = event.get("type")

        if event_type == "assistant":
            message = event.get("message", {})
            for block in message.get("content", []):
                block_type = block.get("type")

                if block_type == "tool_use":
                    tool_name = block.get("name", "unknown")
                    tool_input = block.get("input", {})
                    self._tools_called.append(tool_name)

                    # Skip boring tools
                    if tool_name in _SKIP_TOOLS:
                        return None

                    if tool_name in _EDIT_TOOLS:
                        file_path = tool_input.get("file_path", "")
                        short_name = Path(file_path).name if file_path else "a file"
                        self._files_edited.add(file_path)
                        return f"editing {short_name}"

                    if tool_name in _BASH_TOOLS:
                        cmd = tool_input.get("command", "")
                        # Detect test commands
                        if any(kw in cmd for kw in ("test", "pytest", "jest", "npm test", "cargo test")):
                            return f"running tests: {cmd[:80]}"
                        return f"running command: {cmd[:80]}"

                    return f"using {tool_name}"

        elif event_type == "result":
            if event.get("is_error"):
                error = event.get("error", "unknown error")
                self._errors.append(error)
                return f"error: {error[:80]}"
            return "finished"

        return None

    async def _flush_narration(self) -> None:
        """Generate and speak a summary of pending actions."""
        if not self._pending_actions:
            return

        actions = self._pending_actions.copy()
        self._pending_actions.clear()
        self._last_narration_time = time.monotonic()

        summary = await self._summarize(actions)
        if summary:
            logger.info("Narrating: %s", summary)
            try:
                await self._tts.speak(summary)
            except Exception:
                logger.exception("Failed to speak narration")

    async def _summarize(self, actions: list[str]) -> str:
        """Generate a one-sentence spoken summary of a batch of actions."""
        if len(actions) == 1:
            # Simple actions don't need the brain
            return actions[0]

        # Use the brain for multi-action batches
        actions_text = "; ".join(actions)
        prompt = (
            "Summarize this coding progress in one short, natural sentence "
            "for spoken narration (no markdown, no quotes, max 15 words): "
            f"{actions_text}"
        )

        try:
            summary = await self._brain.think(prompt)
            # Clean up — the brain adds to context, which we don't want for
            # narration meta-prompts. Reset the last two messages (our prompt
            # + its response) to avoid polluting conversation history.
            if self._brain.context_length >= 2:
                # Remove the narration exchange from context
                self._brain._messages = self._brain._messages[:-2]
            return summary.strip()
        except Exception:
            logger.exception("Failed to generate narration summary")
            return actions[-1]  # Fall back to last action

    @staticmethod
    def _build_final_summary(
        files_edited: list[str],
        tools_called: list[str],
        errors: list[str],
    ) -> str:
        """Build the spoken completion summary."""
        parts: list[str] = ["done"]

        if files_edited:
            count = len(files_edited)
            names = ", ".join(Path(f).name for f in files_edited[:3])
            if count <= 3:
                parts.append(f"edited {names}")
            else:
                parts.append(f"edited {count} files including {names}")

        # Check if tests were run by looking at tool calls
        test_tools = [t for t in tools_called if t == "Bash"]
        if test_tools:
            # We can't know test results here, but mention they ran
            pass

        if errors:
            parts.append(f"{len(errors)} error{'s' if len(errors) > 1 else ''} encountered")

        return ". ".join(parts) + "."
