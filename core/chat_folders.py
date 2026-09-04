"""Move a chat into a project folder of your choosing.

A chat's home is normally inferred: the directory it ran in decides its slug,
its sidebar chip and its place in the mirror tree. That is right almost always
and wrong exactly when the inference has nothing to go on -- a chat run from
``~`` or from a Windows path that means nothing on this machine, or one whose
subject has simply moved on from where it happened to be typed.

This records an explicit home for one chat and makes every surface agree with
it. The home is a real directory path, not a label, because that is what the
rest of Serena already speaks: the slug is derived from it, the sidebar chip is
derived from the slug, and the mirror subpath is derived from the path. Storing
anything else would mean teaching four places about a fifth concept.

Moving a chat therefore:

* creates the project folder on disk if it is not there yet, so the sidebar has
  something real to point at and the next chat started there lands beside it;
* records the override in the chat's metadata, where it survives re-indexing
  (the index is rebuilt from files, so anything the files do not say has to
  live here);
* rewrites the indexed row so the sidebar moves it now rather than after the
  next scan;
* relocates the mirror symlink and removes the one left behind, so the
  canonical tree does not grow a second copy.

The transcript itself never moves. It stays where its CLI wrote it, which is
the only place that CLI will look when you resume the chat.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from core import metadata
from core.config import DB_PATH, claude_project_dir_for

META_KEY = "chat_folder"

# Folder names are directory names. Keep them boring: no separators to escape,
# no leading dots to hide the folder from a listing, nothing that needs quoting
# in a shell.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def projects_root() -> Path:
    """Where this machine keeps checkouts.

    Resolved per machine rather than hardcoded: the Linux laptop keeps them
    under ~/Documents/Projects and the Windows PC under ~/Projects, and a
    folder named on one must mean the same folder on the other.
    """
    from core.machine_context import projects_root as _machine_projects_root

    root = _machine_projects_root()
    return Path(root) if root else Path.home() / "Documents" / "Projects"


def normalize_segment(name: str) -> str:
    """One folder name, safe to create and stable across machines."""
    cleaned = _UNSAFE.sub("-", str(name or "").strip()).strip("-._")
    return cleaned.lower()


def resolve_folder(folder: str) -> Path:
    """Turn ``frameworth/it`` into an absolute path under the projects root.

    An absolute path is taken as given, so a chat can be moved somewhere the
    projects root does not cover. A relative one is read against the root,
    which is what makes ``frameworth/it`` mean the obvious thing.
    """
    text = str(folder or "").strip()
    if not text:
        raise ValueError("a destination folder is required")

    candidate = Path(text).expanduser()
    if candidate.is_absolute():
        resolved = Path(os.path.normpath(str(candidate)))
        # "///" is absolute and normalises to the filesystem root. A chat filed
        # at / is not filed anywhere, so require a real directory name.
        if not resolved.name:
            raise ValueError(f"{folder!r} has no usable folder name in it")
        return resolved

    segments = [normalize_segment(part) for part in re.split(r"[\\/]+", text) if part.strip()]
    segments = [part for part in segments if part]
    if not segments:
        raise ValueError(f"{folder!r} has no usable folder name in it")
    return projects_root().joinpath(*segments)


def get_folder(session_id: str) -> str | None:
    """The explicit home set for this chat, if any."""
    value = (metadata.get_meta(session_id) or {}).get(META_KEY)
    return str(value) if value else None


def clear_folder(session_id: str) -> None:
    """Give the chat back to inference."""
    entry = metadata._load_one(session_id)
    if entry.pop(META_KEY, None) is not None:
        metadata._save_one(session_id, entry)


def _set_folder(session_id: str, path: Path) -> None:
    entry = metadata._load_one(session_id)
    entry[META_KEY] = str(path)
    metadata._save_one(session_id, entry)


def move_chat(session_id: str, folder: str, *, create: bool = True) -> dict:
    """Give one chat a new home. Returns what changed.

    ``create`` exists so a caller can offer the move without making a directory
    the user has not agreed to yet.
    """
    import sqlite3

    session_id = str(session_id or "").strip()
    if not session_id:
        raise ValueError("a session id is required")

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT session_id, agent, project_dir, cwd, file_path FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"no chat with id {session_id}")

        destination = resolve_folder(folder)
        created = False
        if not destination.is_dir():
            if not create:
                raise ValueError(f"{destination} does not exist")
            destination.mkdir(parents=True, exist_ok=True)
            created = True

        previous_slug = str(row["project_dir"] or "")
        slug = claude_project_dir_for(str(destination))
        _set_folder(session_id, destination)

        # Move the row now. The indexer would get there eventually, but the
        # sidebar is what the user is looking at while they do this.
        conn.execute(
            "UPDATE sessions SET project_dir = ?, slug = ? WHERE session_id = ?",
            (slug, slug, session_id),
        )
        conn.commit()
    finally:
        conn.close()

    moved_mirror = _remirror(session_id, str(row["agent"] or "claude"), row["file_path"])

    return {
        "ok": True,
        "session_id": session_id,
        "folder": str(destination),
        "folder_created": created,
        "project_dir": slug,
        "previous_project_dir": previous_slug,
        "mirror": moved_mirror,
    }


def _remirror(session_id: str, agent: str, file_path: str | None) -> dict:
    """Point the canonical tree at the new home and drop the old link."""
    from core.project_mirror import (
        CLAUDE_PROJECTS,
        CODEX_PROJECTS,
        GEMINI_PROJECTS,
        canonical_subpath,
        folder_subpath,
    )

    roots = {"claude": CLAUDE_PROJECTS, "codex": CODEX_PROJECTS, "gemini": GEMINI_PROJECTS}
    root = roots.get(agent)
    result: dict[str, object] = {"linked": None, "unlinked": []}
    if root is None or not file_path or not os.path.exists(file_path):
        return result

    source = Path(file_path)
    subpath = folder_subpath(get_folder(session_id))
    if not subpath:
        return result

    target_dir = root / subpath
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f"{session_id}{source.suffix}" if agent == "gemini" else source.name
    destination = target_dir / name

    # Take out every other link to this transcript first, so the chat appears
    # in exactly one folder rather than in its old home as well.
    for existing in root.rglob(name):
        if existing == destination or not existing.is_symlink():
            continue
        try:
            if Path(os.readlink(existing)).name == source.name:
                existing.unlink()
                result["unlinked"].append(str(existing))
        except OSError:
            continue

    if not destination.exists():
        try:
            destination.symlink_to(source)
            result["linked"] = str(destination)
        except OSError:
            pass
    else:
        result["linked"] = str(destination)
    return result


def list_folders(*, depth: int = 3) -> list[dict]:
    """Folders under the projects root, for a picker to offer.

    Only directories are listed, and only to a shallow depth: this is a place
    to file a chat, not a file browser, and walking a checkout's node_modules
    to offer it as a destination would be absurd.
    """
    root = projects_root()
    if not root.is_dir():
        return []

    # Directories that are never a home for a chat.
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next"}

    found: list[dict] = []

    def walk(directory: Path, level: int) -> None:
        if level > depth:
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith(".") or entry.name in skip:
                continue
            found.append(
                {
                    "path": str(entry),
                    "relative": str(entry.relative_to(root)),
                    "depth": level,
                }
            )
            walk(entry, level + 1)

    walk(root, 1)
    return found
