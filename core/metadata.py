"""Synced metadata storage for stars, tags, and custom titles.

Stored as a JSON file in ~/.claude/projects/.chats-meta.json so Syncthing
syncs it across devices. This file is small and append-style updates are
safe — each device only adds/modifies entries for its own sessions, and
since entries are keyed by session_id (UUID), there are no conflicts.
"""

import json
from pathlib import Path

from core.config import METADATA_PATH


def _load() -> dict:
    """Load the metadata file. Returns {session_id: {starred, tags, custom_title}}."""
    if not METADATA_PATH.exists():
        return {}
    try:
        return json.loads(METADATA_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    """Save the metadata file."""
    METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    METADATA_PATH.write_text(
        json.dumps(data, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def get_meta(session_id: str) -> dict:
    """Get metadata for a session."""
    data = _load()
    return data.get(session_id, {})


def set_starred(session_id: str, starred: bool) -> None:
    data = _load()
    entry = data.setdefault(session_id, {})
    entry["starred"] = starred
    _save(data)


def set_custom_title(session_id: str, title: str) -> None:
    data = _load()
    entry = data.setdefault(session_id, {})
    entry["custom_title"] = title
    _save(data)


def set_model(session_id: str, model: str | None) -> None:
    """Pin the model for a session (overrides the global /model setting on spawn)."""
    data = _load()
    entry = data.setdefault(session_id, {})
    if model:
        entry["model"] = model
    else:
        entry.pop("model", None)
    _save(data)


def set_effort(session_id: str, effort: str | None) -> None:
    """Pin the effort level for a session (low / medium / high / xhigh / max)."""
    data = _load()
    entry = data.setdefault(session_id, {})
    if effort:
        entry["effort"] = effort
    else:
        entry.pop("effort", None)
    _save(data)


def set_done(session_id: str, done: bool, done_at: str | None = None) -> None:
    """Mark or unmark a session as 'done'. done_at is the ISO timestamp when marked."""
    data = _load()
    entry = data.setdefault(session_id, {})
    if done:
        entry["done"] = True
        if done_at:
            entry["done_at"] = done_at
    else:
        entry.pop("done", None)
        entry.pop("done_at", None)
    _save(data)


def add_tag_meta(session_id: str, tag: str) -> None:
    data = _load()
    entry = data.setdefault(session_id, {})
    tags = set(entry.get("tags", []))
    tags.add(tag)
    entry["tags"] = sorted(tags)
    _save(data)


def remove_tag_meta(session_id: str, tag: str) -> None:
    data = _load()
    entry = data.get(session_id, {})
    tags = set(entry.get("tags", []))
    tags.discard(tag)
    entry["tags"] = sorted(tags)
    data[session_id] = entry
    _save(data)


def delete_meta(session_id: str) -> None:
    data = _load()
    data.pop(session_id, None)
    _save(data)


def get_all_meta() -> dict:
    """Get all metadata. Used during index rebuild to apply synced state."""
    return _load()
