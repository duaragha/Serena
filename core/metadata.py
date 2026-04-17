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
