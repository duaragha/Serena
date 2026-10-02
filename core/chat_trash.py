"""The chat trash bin: deleted chats, listed and put back where they were.

A sidebar delete never unlinks a transcript. `indexer._delete_unowned_session`
moves it to `DATA_DIR/deleted-sessions/<sid>[-stamp]/` beside a
`recovery.json` manifest (original path, metadata, and since the trash bin
exists, a summary of the chat). Restoring moves the transcript back, returns
its metadata (stars, title, link) and indexes it at once so it reappears.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from core import indexer
from core import metadata as meta_sync


class TrashError(RuntimeError):
    pass


def trash_dir() -> Path:
    # Read through the indexer at call time, where deletes write it.
    return indexer.DATA_DIR / "deleted-sessions"


def _agent_for(manifest: dict, original: str) -> str:
    agent = (manifest.get("session") or {}).get("agent")
    if agent:
        return agent
    normalized = original.replace("\\", "/")
    if "/.codex/" in normalized:
        return "codex"
    if "/muse/" in normalized:
        return "muse"
    if "gemini" in normalized:
        return "gemini"
    if "/locket" in normalized:
        return "locket"
    return "claude"


def _parsed_title(agent: str, transcript: Path, original: str) -> str:
    """Name an entry trashed before manifests carried a title."""
    try:
        if agent == "codex":
            meta = indexer.parse_codex_metadata(transcript)
        elif agent == "gemini":
            meta = indexer.parse_gemini_metadata(transcript)
        elif agent == "muse":
            meta = indexer.parse_muse_metadata(transcript)
        elif agent == "claude":
            meta = indexer.parse_metadata(transcript, Path(original).parent.name)
        else:
            return ""
    except Exception:
        return ""
    if meta is None:
        return ""
    return getattr(meta, "native_title", None) or indexer.generate_title(meta.first_message) or ""


def _read_entry(entry: Path) -> dict | None:
    try:
        manifest = json.loads((entry / "recovery.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict) or not manifest.get("session_id") or not manifest.get("original_path"):
        return None
    return manifest


def _entry_path(entry_id: str) -> Path:
    root = trash_dir()
    if not entry_id or "/" in entry_id or "\\" in entry_id or entry_id in (".", ".."):
        raise TrashError("Unknown trash entry")
    entry = root / entry_id
    if not entry.is_dir():
        raise TrashError("That chat is no longer in the trash")
    return entry


def list_trash(limit: int = 100) -> dict:
    """Newest deletions first: {"items": [...], "total": n}."""
    root = trash_dir()
    if not root.is_dir():
        return {"items": [], "total": 0}
    entries = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        manifest = _read_entry(entry)
        if manifest is None:
            continue
        entries.append((manifest.get("deleted_at") or "", entry, manifest))
    entries.sort(key=lambda item: item[0], reverse=True)
    items = []
    for deleted_at, entry, manifest in entries[: max(0, limit)]:
        original = manifest["original_path"]
        transcript = entry / Path(original).name
        agent = _agent_for(manifest, original)
        summary = manifest.get("session") or {}
        title = (
            (manifest.get("metadata") or {}).get("custom_title")
            or summary.get("title")
            or (_parsed_title(agent, transcript, original) if transcript.exists() else "")
            or "Untitled chat"
        )
        items.append({
            "id": entry.name,
            "session_id": manifest["session_id"],
            "title": title,
            "agent": agent,
            "deleted_at": deleted_at,
            "restorable": transcript.exists() and not Path(original).exists(),
        })
    return {"items": items, "total": len(entries)}


def restore(entry_id: str) -> dict:
    """Move one trashed chat back to its original path and index it."""
    entry = _entry_path(entry_id)
    manifest = _read_entry(entry)
    if manifest is None:
        raise TrashError("This trash entry has no readable manifest")
    sid = manifest["session_id"]
    original = Path(manifest["original_path"])
    transcript = entry / original.name
    if not transcript.exists():
        raise TrashError("The transcript is missing from the trash")
    if original.exists():
        raise TrashError("A chat already exists where this one lived")
    agent = _agent_for(manifest, str(original))
    original.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(transcript), str(original))
    meta_sync.restore_meta(sid, manifest.get("metadata") or {})
    shutil.rmtree(entry, ignore_errors=True)
    summary = manifest.get("session") or {}
    try:
        indexed = indexer.index_session_file(agent, original, summary.get("project_dir") or "")
    except Exception as error:
        print(f"[trash] restored {sid[:8]} but could not index it yet: {error}", flush=True)
        indexed = None
    return {"session_id": sid, "path": str(original), "indexed": bool(indexed)}


def restore_latest(session_id: str) -> dict:
    """Undo a delete: restore the newest trash entry for this session id."""
    root = trash_dir()
    candidates = []
    if root.is_dir():
        for entry in root.iterdir():
            if entry.is_dir() and (entry.name == session_id or entry.name.startswith(session_id + "-")):
                manifest = _read_entry(entry)
                if manifest and manifest["session_id"] == session_id:
                    candidates.append((manifest.get("deleted_at") or "", entry.name))
    if not candidates:
        raise TrashError("That chat is no longer in the trash")
    return restore(max(candidates)[1])
