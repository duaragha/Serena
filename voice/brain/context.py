"""Conversation persistence — save/load context between daemon restarts."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path.home() / ".config" / "serena" / "conversation.json"
_STALE_SECONDS = 24 * 60 * 60  # 24 hours


class ConversationStore:
    """Persist conversation context to disk as JSON.

    Saves a timestamped snapshot of the message list so context survives
    daemon restarts.  Conversations older than 24 hours are treated as
    stale and discarded on load.
    """

    def __init__(self, path: str | Path = _DEFAULT_PATH) -> None:
        self._path = Path(path).expanduser()

    @property
    def path(self) -> Path:
        return self._path

    def save(self, messages: list[dict[str, Any]]) -> None:
        """Write the current conversation context to disk.

        Creates parent directories if they don't exist.  Writes atomically
        via a temp file + rename so a crash mid-write won't corrupt the file.
        """
        payload = {
            "saved_at": time.time(),
            "messages": messages,
        }

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)

            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)

            logger.debug("Saved %d messages to %s", len(messages), self._path)
        except OSError:
            logger.exception("Failed to save conversation context")

    def load(self) -> list[dict[str, Any]]:
        """Load saved conversation context.

        Returns an empty list if the file is missing, corrupt, or stale
        (older than 24 hours).
        """
        if not self._path.exists():
            logger.debug("No saved conversation at %s", self._path)
            return []

        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            logger.warning("Corrupt conversation file at %s — starting fresh", self._path)
            return []

        if not isinstance(data, dict) or "messages" not in data:
            logger.warning("Unexpected conversation format — starting fresh")
            return []

        # Check staleness
        saved_at = data.get("saved_at", 0)
        age = time.time() - saved_at
        if age > _STALE_SECONDS:
            logger.info(
                "Saved conversation is %.1f hours old — discarding",
                age / 3600,
            )
            return []

        messages = data["messages"]
        if not isinstance(messages, list):
            logger.warning("Messages field is not a list — starting fresh")
            return []

        logger.info(
            "Restored %d messages from %s (%.1f min old)",
            len(messages),
            self._path,
            age / 60,
        )
        return messages

    def prune(self, max_messages: int) -> None:
        """Load, trim to max_messages, and re-save.

        Removes the oldest messages first.  If the file doesn't exist or
        has fewer messages than the limit, this is a no-op.
        """
        messages = self.load()
        if not messages or len(messages) <= max_messages:
            return

        trimmed = messages[-max_messages:]
        logger.info(
            "Pruned conversation from %d to %d messages",
            len(messages),
            len(trimmed),
        )
        self.save(trimmed)
