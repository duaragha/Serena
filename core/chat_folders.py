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
# Where the transcript lived before it was ever filed. Kept so clearing a
# folder is a real undo rather than a chat left in a folder it no longer
# claims to be in.
ORIGIN_KEY = "chat_folder_origin"

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


def clear_folder(session_id: str) -> dict:
    """Give the chat back to inference, and put its transcript back.

    Filing a chat moves a file. Un-filing it has to move that file back, or the
    transcript stays in a folder nothing points at any more -- which is worse
    than never having moved it, because now no surface agrees on where it is.
    """
    import sqlite3

    entry = metadata._load_one(session_id)
    had_folder = entry.pop(META_KEY, None)
    origin = entry.pop(ORIGIN_KEY, None)
    if had_folder is None and origin is None:
        return {"ok": True, "session_id": session_id, "folder": None, "restored": False}
    metadata._save_one(session_id, entry)

    restored = False
    if origin:
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT agent, file_path FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is not None:
                restored = _restore_transcript(
                    str(row["agent"] or "claude"), row["file_path"], Path(origin)
                )
                if restored:
                    conn.execute(
                        "UPDATE sessions SET file_path = ? WHERE session_id = ?",
                        (origin, session_id),
                    )
                    conn.commit()
        finally:
            conn.close()

    return {"ok": True, "session_id": session_id, "folder": None, "restored": restored}


def _restore_transcript(agent: str, current_path: str | None, origin: Path) -> bool:
    """Put a filed transcript back where its CLI originally wrote it."""
    import shutil

    if not current_path:
        return False
    current = Path(current_path)
    # For Gemini the recorded path may be the flat symlink we left behind; the
    # real file is what has to come home.
    real = current.resolve() if current.is_symlink() else current
    if not real.is_file() or real == origin:
        return False
    if origin.exists() and not origin.is_symlink():
        return False

    origin.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(real, origin.with_suffix(origin.suffix + ".restoring"))
        staged = origin.with_suffix(origin.suffix + ".restoring")
        if staged.stat().st_size != real.stat().st_size:
            staged.unlink()
            return False
        if origin.is_symlink():
            origin.unlink()
        staged.replace(origin)
        real.unlink()
        if current.is_symlink():
            current.unlink()
    except OSError:
        return False
    return True


def get_folder_origin(session_id: str) -> str | None:
    """Where this chat's transcript lived before it was first filed."""
    value = (metadata.get_meta(session_id) or {}).get(ORIGIN_KEY)
    return str(value) if value else None


def _set_origin(session_id: str, path: str) -> None:
    entry = metadata._load_one(session_id)
    entry[ORIGIN_KEY] = path
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
        agent = str(row["agent"] or "claude")
        slug = claude_project_dir_for(str(destination))
        _set_folder(session_id, destination)

        # The transcript goes with the chat -- that is the whole point of
        # filing one. Everything below is recorded only if the file actually
        # landed, so a failed move leaves the index describing where the
        # transcript really is.
        if row["file_path"] and not get_folder_origin(session_id):
            _set_origin(session_id, str(row["file_path"]))
        transcript = move_transcript(session_id, agent, row["file_path"], destination)
        file_path = transcript.get("to") or row["file_path"]

        # The folder becomes the chat's working directory. That is not
        # bookkeeping: `claude -r` looks for a session under the slug of the
        # cwd it was launched in, so a chat filed in frameworth/it is found
        # there and nowhere else. Leaving the old cwd would make Serena stage a
        # second copy of the transcript back under the old slug on every
        # resume, which is the opposite of filing it.
        conn.execute(
            """UPDATE sessions
                   SET project_dir = ?, slug = ?, cwd = ?, last_cwd = ?, file_path = ?
                 WHERE session_id = ?""",
            (slug, slug, str(destination), str(destination), file_path, session_id),
        )
        conn.commit()
    finally:
        conn.close()

    moved_mirror = _remirror(session_id, agent, file_path)

    return {
        "ok": True,
        "session_id": session_id,
        "agent": agent,
        "folder": str(destination),
        "folder_created": created,
        "project_dir": slug,
        "previous_project_dir": previous_slug,
        "transcript": transcript,
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


# --- moving the transcript itself --------------------------------------------
#
# Where each CLI will still find a session after it has been moved. Measured,
# not assumed -- each of these was tested by relocating a real transcript and
# resuming it:
#
#   codex   scans ~/.codex/sessions recursively, so any subdirectory works.
#   claude  looks in ~/.claude/projects/<slug-of-cwd>/, so the destination is
#           the slug of the chat's new folder -- which is why filing a chat
#           also makes that folder its working directory.
#   gemini  looks up conversations/<id>.db by name and does not recurse. It
#           answered "conversation not found" from a subdirectory, and resumed
#           normally through a symlink left at the flat path.

CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
GEMINI_CONVERSATIONS = Path.home() / ".gemini" / "antigravity-cli" / "conversations"


def transcript_destination(agent: str, source: Path, folder: Path) -> Path | None:
    """Where this agent's transcript should live once filed under ``folder``."""
    from core.project_mirror import folder_subpath

    agent = (agent or "").lower()
    if agent == "claude":
        return CLAUDE_PROJECTS_DIR / claude_project_dir_for(str(folder)) / source.name
    subpath = folder_subpath(str(folder))
    if not subpath:
        return None
    if agent == "codex":
        return CODEX_SESSIONS / subpath / source.name
    if agent == "gemini":
        return GEMINI_CONVERSATIONS / subpath / source.name
    return None


def _same_bytes(left: Path, right: Path) -> bool:
    return left.stat().st_size == right.stat().st_size


def move_transcript(session_id: str, agent: str, file_path: str, folder: Path) -> dict:
    """Relocate the transcript, or explain why it stayed put.

    Copy, verify, then unlink. A bare rename is atomic only within one
    filesystem, and these trees are large enough that a half-written 75 MB
    transcript is a chat destroyed rather than a chat misfiled. The original is
    removed only once the copy is on disk at the right size.
    """
    result: dict[str, object] = {"moved": False, "from": file_path, "to": None, "link": None}
    agent = (agent or "").lower()
    source = Path(file_path or "")
    if not file_path or not source.is_file() or source.is_symlink():
        # A symlink here is one of our own from an earlier move; the real file
        # is already filed and nothing needs to happen.
        return result

    destination = transcript_destination(agent, source, folder)
    if destination is None:
        return result
    if destination == source:
        # Already filed here. The outcome the caller cares about is "the
        # transcript is in the folder", which is true, so say so rather than
        # reporting a failure for a move that was not needed.
        result.update(moved=True, to=str(destination))
        return result
    if destination.exists():
        # Same chat already there (a repeated move); different file with the
        # same name would be someone else's transcript, so never overwrite.
        if destination.is_file() and _same_bytes(source, destination):
            result.update(moved=True, to=str(destination))
            return result
        result["error"] = f"{destination} already exists"
        return result

    destination.parent.mkdir(parents=True, exist_ok=True)
    import shutil

    try:
        shutil.copy2(source, destination)
        if not _same_bytes(source, destination):
            raise OSError("the copy did not match the original's size")
    except OSError as exc:
        with __import__("contextlib").suppress(OSError):
            destination.unlink()
        result["error"] = f"could not copy the transcript: {exc}"
        return result

    # Gemini finds a conversation by name in one flat directory, so the moved
    # file has to stay reachable there. The link goes in before the original
    # comes out: if it cannot be made, the move is abandoned rather than
    # leaving a chat the CLI can no longer open.
    if agent == "gemini":
        try:
            source.unlink()
            source.symlink_to(destination)
            result["link"] = str(source)
        except OSError as exc:
            with __import__("contextlib").suppress(OSError):
                if not source.exists():
                    shutil.copy2(destination, source)
                destination.unlink()
            result["error"] = f"could not leave a link for the Gemini CLI: {exc}"
            return result
    else:
        try:
            source.unlink()
        except OSError as exc:
            # The copy is good; the original could not be removed. Leaving both
            # would index the chat twice, so undo rather than duplicate.
            with __import__("contextlib").suppress(OSError):
                destination.unlink()
            result["error"] = f"could not remove the original: {exc}"
            return result

    result.update(moved=True, to=str(destination))
    return result
