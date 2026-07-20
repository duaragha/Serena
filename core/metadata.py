"""Synced metadata storage for stars, tags, custom titles, groups, etc.

PER-SESSION layout: each chat's metadata lives in its own file at
~/.claude/projects/.chats-meta/<session_id>.json. Syncthing syncs the
directory; because each chat is a separate file, two devices renaming
DIFFERENT chats touch different files and merge cleanly. Only renaming the
SAME chat on both devices at once can conflict (Syncthing makes a visible
conflict file, which is recoverable — vs the old single-file store that
silently clobbered cross-device edits).

A one-time migration splits the legacy ~/.claude/projects/.chats-meta.json
single file into per-session files on first access.
"""

import json
import threading
from pathlib import Path

from core.config import METADATA_DIR, METADATA_PATH

_MIGRATION_LOCK = threading.Lock()
_migrated = False


def _ensure_migrated() -> None:
    """Split the legacy single-file store into per-session files. Idempotent;
    runs at most once per process."""
    global _migrated
    if _migrated:
        return
    with _MIGRATION_LOCK:
        if _migrated:
            return
        try:
            METADATA_DIR.mkdir(parents=True, exist_ok=True)
            if METADATA_PATH.exists():
                try:
                    legacy = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    legacy = {}
                for sid, entry in legacy.items():
                    if not isinstance(entry, dict):
                        continue
                    dst = METADATA_DIR / f"{sid}.json"
                    # Don't overwrite a per-session file that's already been
                    # written (it's newer / authoritative).
                    if dst.exists():
                        continue
                    try:
                        dst.write_text(json.dumps(entry, indent=2, sort_keys=True), encoding="utf-8")
                    except OSError:
                        pass
                # Rename the legacy file so it stops being the source of truth
                # (keep a backup rather than deleting).
                try:
                    METADATA_PATH.rename(METADATA_PATH.with_suffix(".json.migrated"))
                except OSError:
                    pass
        finally:
            _migrated = True


def _session_path(session_id: str) -> Path:
    return METADATA_DIR / f"{session_id}.json"


def _load_one(session_id: str) -> dict:
    _ensure_migrated()
    p = _session_path(session_id)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_one(session_id: str, entry: dict) -> None:
    _ensure_migrated()
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    p = _session_path(session_id)
    if not entry:
        # Empty entry → remove the file rather than leaving an empty husk
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        return
    p.write_text(json.dumps(entry, indent=2, sort_keys=True), encoding="utf-8")


def _load_all() -> dict:
    """Read every per-session file into {sid: entry}. Used by group scans +
    index rebuild. O(n files) but n is small and reads are cheap."""
    _ensure_migrated()
    out: dict = {}
    if not METADATA_DIR.exists():
        return out
    for p in METADATA_DIR.glob("*.json"):
        sid = p.stem
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                out[sid] = d
        except (json.JSONDecodeError, OSError):
            continue
    return out


def get_meta(session_id: str) -> dict:
    """Get metadata for a session."""
    return _load_one(session_id)


def set_starred(session_id: str, starred: bool) -> None:
    entry = _load_one(session_id)
    entry["starred"] = starred
    _save_one(session_id, entry)


def set_custom_title(session_id: str, title: str) -> None:
    entry = _load_one(session_id)
    entry["custom_title"] = title
    _save_one(session_id, entry)


def set_resident_work(session_id: str, resident: bool = True) -> None:
    """Mark a Codex exec session as user-visible Serena-owned work."""

    entry = _load_one(session_id)
    if resident:
        entry["resident_work"] = True
    else:
        entry.pop("resident_work", None)
    _save_one(session_id, entry)


def set_done(session_id: str, done: bool, done_at: str | None = None) -> None:
    """Mark or unmark a session as 'done'. done_at is the ISO timestamp when marked."""
    entry = _load_one(session_id)
    if done:
        entry["done"] = True
        if done_at:
            entry["done_at"] = done_at
    else:
        entry.pop("done", None)
        entry.pop("done_at", None)
    _save_one(session_id, entry)


def add_tag_meta(session_id: str, tag: str) -> None:
    entry = _load_one(session_id)
    tags = set(entry.get("tags", []))
    tags.add(tag)
    entry["tags"] = sorted(tags)
    _save_one(session_id, entry)


def remove_tag_meta(session_id: str, tag: str) -> None:
    entry = _load_one(session_id)
    tags = set(entry.get("tags", []))
    tags.discard(tag)
    entry["tags"] = sorted(tags)
    _save_one(session_id, entry)


def delete_meta(session_id: str) -> None:
    _ensure_migrated()
    try:
        _session_path(session_id).unlink()
    except FileNotFoundError:
        pass


def get_all_meta() -> dict:
    """Get all metadata. Used during index rebuild to apply synced state."""
    return _load_all()


# ─────────────────────────────────────────────────────────────────────────────
# === GROUP FEATURE START === (delete this block + the route block in
# ui/web.py and the JS block to remove the linked-chats / shared-thread
# feature.)
# ─────────────────────────────────────────────────────────────────────────────
import secrets


def _new_group_id() -> str:
    return "g_" + secrets.token_hex(6)


def get_group(session_id: str) -> str | None:
    return _load_one(session_id).get("group") or None


def set_group(session_id: str, group_id: str | None) -> None:
    entry = _load_one(session_id)
    if group_id:
        entry["group"] = group_id
    else:
        entry.pop("group", None)
    _save_one(session_id, entry)


def list_group_members(group_id: str) -> list[str]:
    if not group_id:
        return []
    return [sid for sid, m in _load_all().items() if isinstance(m, dict) and m.get("group") == group_id]


def link_sessions(session_ids: list[str]) -> str:
    """Link N sessions into the same group. If any of them already have a
    group, that group wins (and any others get merged into it). Returns the
    final group_id."""
    session_ids = [s for s in session_ids if s]
    if len(session_ids) < 2:
        raise ValueError("link_sessions requires at least 2 session ids")
    data = _load_all()
    existing: list[str] = []
    for sid in session_ids:
        g = (data.get(sid) or {}).get("group")
        if g and g not in existing:
            existing.append(g)
    target_group = existing[0] if existing else _new_group_id()
    # If multiple existing groups, sweep all members of each into target_group
    if len(existing) > 1:
        merge_groups = set(existing[1:])
        for sid, m in data.items():
            if isinstance(m, dict) and m.get("group") in merge_groups and m.get("group") != target_group:
                m["group"] = target_group
                _save_one(sid, m)
    # Apply to incoming sids
    for sid in session_ids:
        entry = _load_one(sid)
        entry["group"] = target_group
        _save_one(sid, entry)
    return target_group


def unlink_session(session_id: str) -> None:
    """Remove this session from its group. Other members keep the group.
    If only one member remains afterward, also clear that one (singletons
    aren't groups)."""
    g = get_group(session_id)
    if not g:
        return
    entry = _load_one(session_id)
    entry.pop("group", None)
    _save_one(session_id, entry)
    remaining = list_group_members(g)
    if len(remaining) == 1:
        sole = _load_one(remaining[0])
        sole.pop("group", None)
        _save_one(remaining[0], sole)


def unlink_group(group_id: str) -> None:
    """Disband the group entirely — clear it from every member."""
    if not group_id:
        return
    for sid in list_group_members(group_id):
        entry = _load_one(sid)
        entry.pop("group", None)
        _save_one(sid, entry)
# === GROUP FEATURE END ===
