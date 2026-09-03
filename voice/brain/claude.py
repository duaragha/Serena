"""Claude API integration for Serena's brain."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import anthropic

from serena.brain.context import ConversationStore
from serena.brain.tools import ToolRegistry
from serena.config import LLMConfig

logger = logging.getLogger(__name__)

PERSONA_PATH = Path.home() / "Documents" / "Projects" / "chats" / "Persona.md"

VOICE_MODE_ADDENDUM = (
    "\n\n## Voice Mode\n"
    "You are speaking out loud via TTS. Keep responses concise and "
    "conversational — 1-3 sentences for simple queries, more for complex ones. "
    "No markdown formatting, no bullet lists, no code blocks unless explicitly "
    "asked. Use natural spoken language. Numbers should be spoken naturally "
    '(say "twenty three" not "23"). Avoid parenthetical asides and footnotes.'
)


def _load_system_prompt() -> str:
    """Load Serena's persona and append voice-mode instructions."""
    if not PERSONA_PATH.exists():
        logger.warning("Persona file not found at %s, using fallback", PERSONA_PATH)
        return (
            "You are Serena, a personal AI assistant. "
            "Be conversational, concise, and helpful." + VOICE_MODE_ADDENDUM
        )

    persona = PERSONA_PATH.read_text(encoding="utf-8").strip()
    return persona + VOICE_MODE_ADDENDUM


_MAX_TOOL_ROUNDS = 10  # Guard against infinite tool-use loops


class ClaudeBrain:
    """Claude-powered conversational brain for Serena.

    Maintains a rolling conversation context and sends messages to the
    Claude API via the Anthropic SDK.  Supports tool use: when Claude
    requests a tool call, the brain executes it via the ToolRegistry and
    feeds the result back until Claude produces a final text response.
    """

    def __init__(
        self,
        config: LLMConfig,
        *,
        tool_registry: ToolRegistry | None = None,
        conversation_store: ConversationStore | None = None,
    ) -> None:
        self._config = config
        self._system_prompt = _load_system_prompt()
        self._messages: list[dict[str, Any]] = []
        self._tool_registry = tool_registry or ToolRegistry()
        self._store = conversation_store

        # Anthropic client with built-in retry (default max_retries=2)
        self._client = anthropic.AsyncAnthropic(max_retries=3)

        # Restore persisted context (discards if >24h old)
        if self._store:
            self._messages = self._store.load()

        logger.info(
            "ClaudeBrain initialized — model=%s, max_context_turns=%d, tools=%d, restored=%d msgs",
            config.default_model,
            config.max_context_turns,
            self._tool_registry.tool_count,
            len(self._messages),
        )

    @property
    def tool_registry(self) -> ToolRegistry:
        """Expose the registry so callers can add tools after construction."""
        return self._tool_registry

    @property
    def context_length(self) -> int:
        """Number of messages currently in the conversation context."""
        return len(self._messages)

    def reset_context(self) -> None:
        """Clear all conversation history."""
        self._messages.clear()
        logger.info("Conversation context cleared")

    def _trim_context(self) -> None:
        """Trim context to stay within the configured turn limit.

        Each turn is a user + assistant message pair, so we keep
        max_context_turns * 2 messages.  We never trim mid-tool-use:
        if the trim point would land inside a tool_use/tool_result
        exchange, we keep the full exchange.  Trims from the front
        to preserve the most recent exchanges.
        """
        max_messages = self._config.max_context_turns * 2
        if len(self._messages) <= max_messages:
            return

        cut = len(self._messages) - max_messages

        # Don't slice into a tool_result (user message containing tool results)
        # — always keep the preceding assistant tool_use message with it.
        while cut > 0 and cut < len(self._messages):
            msg = self._messages[cut]
            content = msg.get("content")
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                # This is a tool_result — back up one to include the tool_use
                cut -= 1
            else:
                break

        if cut > 0:
            self._messages = self._messages[cut:]
            logger.debug("Trimmed %d oldest messages from context", cut)

    async def think(self, user_message: str) -> str:
        """Send a message to Claude and return the response.

        Handles tool use transparently: if Claude requests tools, the brain
        executes them and feeds results back in a loop until Claude produces
        a final text response (or the max-rounds guard is hit).

        Args:
            user_message: What the user said.

        Returns:
            Claude's response text.

        Raises:
            anthropic.APIError: On unrecoverable API failures (after retries).
        """
        self._messages.append({"role": "user", "content": user_message})
        self._trim_context()

        # Build the API kwargs — only include tools when we have some
        tool_defs = self._tool_registry.get_tool_definitions()
        api_kwargs: dict[str, Any] = {
            "model": self._config.default_model,
            "max_tokens": 1024,
            "system": self._system_prompt,
            "messages": self._messages,
        }
        if tool_defs:
            api_kwargs["tools"] = tool_defs

        for round_num in range(_MAX_TOOL_ROUNDS):
            try:
                response = await self._client.messages.create(**api_kwargs)
            except anthropic.APIError:
                # Roll back: remove every message we added this turn
                self._rollback_to_last_user_message()
                logger.exception("Claude API request failed")
                return "I'm having trouble connecting to my brain right now. Give me a moment and try again."

            logger.info(
                "Claude response (round %d, stop=%s, %d input / %d output tokens)",
                round_num + 1,
                response.stop_reason,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )

            # If Claude didn't ask for any tools, we're done
            if response.stop_reason != "tool_use":
                text = self._extract_text(response.content)
                self._messages.append({"role": "assistant", "content": text})
                self._auto_save()
                return text

            # Claude wants to use tools — execute them all and send results back
            tool_results = await self._handle_tool_use(response.content)

            # Append the assistant's response (with tool_use blocks) as-is
            self._messages.append({
                "role": "assistant",
                "content": self._serialize_content(response.content),
            })
            # Append tool results as a user message
            self._messages.append({
                "role": "user",
                "content": tool_results,
            })
            # Update messages in the API kwargs for the next round
            api_kwargs["messages"] = self._messages

        # Exhausted rounds — return whatever text we have
        logger.warning("Hit max tool-use rounds (%d)", _MAX_TOOL_ROUNDS)
        text = self._extract_text(response.content)
        fallback = text or "(max tool rounds reached)"
        self._messages.append({"role": "assistant", "content": fallback})
        self._auto_save()
        return text or "I ran into a limit processing that request."

    def _auto_save(self) -> None:
        """Persist context to disk after each successful exchange."""
        if self._store:
            self._store.save(self._messages)

    def _rollback_to_last_user_message(self) -> None:
        """Remove messages back to (and including) the last user text message.

        Used on API failure so context stays consistent.
        """
        while self._messages:
            msg = self._messages[-1]
            self._messages.pop()
            # Stop after removing the original user text message
            if msg["role"] == "user" and isinstance(msg["content"], str):
                break

    @staticmethod
    def _extract_text(content_blocks: list[Any]) -> str:
        """Pull plain text out of Anthropic response content blocks."""
        return "".join(
            block.text for block in content_blocks if block.type == "text"
        )

    @staticmethod
    def _serialize_content(content_blocks: list[Any]) -> list[dict[str, Any]]:
        """Convert Anthropic content block objects to plain dicts for context.

        The messages list needs JSON-serializable dicts, not SDK objects.
        """
        serialized: list[dict[str, Any]] = []
        for block in content_blocks:
            if block.type == "text":
                serialized.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                serialized.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
        return serialized

    async def _handle_tool_use(
        self, content_blocks: list[Any]
    ) -> list[dict[str, Any]]:
        """Execute all tool_use blocks and build tool_result messages."""
        results: list[dict[str, Any]] = []
        for block in content_blocks:
            if block.type != "tool_use":
                continue

            logger.info(
                "Executing tool '%s' (id=%s) with args: %s",
                block.name, block.id, block.input,
            )
            result_text = await self._tool_registry.execute_tool(
                block.name, block.input
            )
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_text,
            })

        return results
