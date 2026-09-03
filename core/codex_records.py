"""Read the conversation out of a Codex rollout, whichever format wrote it.

Codex changed how it records a turn. Up to 0.146 every turn produced an
``event_msg`` record whose payload type was ``user_message`` or
``agent_message``, and Serena read only those. From 0.150 those events are gone.
The conversation is still on disk, as ``response_item`` records carrying
``{"type": "message", "role": ..., "content": [...]}``, which is the shape the
model API itself uses.

Nothing in Serena knew that, so every chat from the new Codex indexed as zero
messages, opened as an empty transcript, and the ask-codex bridge waited forever
for a ``user_message`` that was never coming.

Two details make this more than a rename:

Older rollouts carry BOTH shapes for the same turn, so accepting both would
double every historical chat. Where a file has the events, the events win and
the items are ignored; the items are the fallback for files that have none.

The new shape also exposes turns the old one hid. Codex injects AGENTS.md,
environment context and attached-file listings as ``user`` messages, and marks
its own scaffolding ``developer``. None of that was ever visible as a
``user_message`` event, and surfacing it now would put three kilobytes of
boilerplate at the top of every chat and title them all "AGENTS.md
instructions". Those are filtered, which restores the behaviour the old format
gave for free.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

# Roles that belong to the conversation. "developer" is Codex's own scaffolding
# (skills, permissions, multi-agent mode, aborted-turn notices) and is not a
# turn anyone had.
CONVERSATION_ROLES = ("user", "assistant")

_EVENT_ROLES = {
    "user_message": "user",
    "agent_message": "assistant",
    "assistant_message": "assistant",
}

# Context Codex injects into the user slot. Matched against the start of the
# text, which is where every one of these declares itself.
_INJECTED_PREFIXES = (
    "# AGENTS.md instructions",
    "# Files mentioned by the user:",
)

# Injected context is otherwise wrapped in an XML-ish tag on the first line:
# <environment_context>, <in-app-browser-context ...>, <memory-context>, and so
# on. A real message can start with "<" but not with a lone tag on its own.
_TAG_BLOCK = re.compile(r"^<[a-z][a-z0-9_.-]*(\s[^>]*)?>")


def is_injected_context(role: str, text: str) -> bool:
    """True when this "user" turn is something Codex inserted, not something typed."""
    if role != "user":
        return False
    head = text.lstrip()
    if head.startswith(_INJECTED_PREFIXES):
        return True
    return bool(_TAG_BLOCK.match(head))


def _content_text(payload: dict) -> str:
    """Join a response_item's content parts; they split input and output text."""
    parts = []
    for part in payload.get("content") or []:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts)


def message_of(record: dict) -> tuple[str, str, str] | None:
    """One record as ``(shape, role, text)``, or None when it is not a turn.

    ``shape`` is "event" for the old envelope and "item" for the new one, so a
    caller reading a whole file can prefer one and avoid counting turns twice.
    """
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    kind = record.get("type")

    if kind == "event_msg":
        role = _EVENT_ROLES.get(payload.get("type"))
        if not role:
            return None
        text = payload.get("message") or payload.get("text") or ""
        if not isinstance(text, str) or not text.strip():
            return None
        return "event", role, text

    if kind == "response_item" and payload.get("type") == "message":
        role = payload.get("role")
        if role not in CONVERSATION_ROLES:
            return None
        text = _content_text(payload)
        if not text.strip():
            return None
        if is_injected_context(role, text):
            return None
        return "item", role, text

    return None


def iter_records(path: Path) -> Iterator[dict]:
    """Every parseable record in a rollout. Unreadable lines are skipped."""
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def read_messages(path: Path) -> list[tuple[str, str, str]]:
    """Conversation turns as ``(role, text, timestamp)``, oldest first.

    The events win when the file has any, because a rollout that carries both
    shapes would otherwise report every turn twice.
    """
    events: list[tuple[str, str, str]] = []
    items: list[tuple[str, str, str]] = []
    for record in iter_records(path):
        found = message_of(record)
        if found is None:
            continue
        shape, role, text = found
        target = events if shape == "event" else items
        target.append((role, text, str(record.get("timestamp") or "")))
    return events or items


def first_typed_message(messages: list[tuple[str, str, str]]) -> str:
    """The first thing the person actually said, for titles and previews."""
    for role, text, _ts in messages:
        if role == "user" and text.strip():
            return text.strip()
    return ""
