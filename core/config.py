from pathlib import Path
import os
import re
import sys


CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"

# Store index outside ~/.claude to avoid Syncthing conflicts
DATA_DIR = Path(os.environ.get("CHATS_DATA_DIR", Path.home() / ".local" / "share" / "chats"))
DB_PATH = DATA_DIR / "index.db"

# Synced metadata file — lives inside ~/.claude/projects/ so Syncthing picks it up
# This is a single-writer-per-session file (only metadata changes, no conflicts)
METADATA_PATH = PROJECTS_DIR / ".chats-meta.json"

# Knowledge base directory (moved under serena/ alongside chats)
_knowledge_env = os.environ.get("KNOWLEDGE_DIR", "")
if _knowledge_env:
    KNOWLEDGE_DIR = Path(_knowledge_env)
else:
    KNOWLEDGE_DIR = Path.home() / "Documents" / "Projects" / "serena" / "knowledge"
    if not KNOWLEDGE_DIR.exists():
        _alt = Path.home() / "Projects" / "serena" / "knowledge"
        if _alt.exists():
            KNOWLEDGE_DIR = _alt

# Memory directory (filesystem-based memories)
_memory_env = os.environ.get("MEMORY_DIR", "")
if _memory_env:
    MEMORY_DIR = Path(_memory_env)
else:
    MEMORY_DIR = Path.home() / "Documents" / "Projects" / "serena" / "memory"
    if not MEMORY_DIR.exists():
        _alt = Path.home() / "Projects" / "serena" / "memory"
        if _alt.exists():
            MEMORY_DIR = _alt


_WIN_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WIN_USER_RE = re.compile(r"^[A-Za-z]:[\\/](?:Users|home)[\\/]([^\\/]+)(?:[\\/](.*))?$")
_POSIX_USER_RE = re.compile(r"^/(?:home|Users)/([^/]+)(?:/(.*))?$")


def resolve_session_cwd(raw_cwd: str | None) -> str:
    """Resolve a session's stored cwd to a valid directory on this machine.

    Chats can sync across OSes (Windows ↔ Linux ↔ macOS) via Syncthing. The cwd
    recorded in the transcript may be invalid on the current platform — e.g. a
    Windows path like ``C:\\Users\\ragha\\Documents\\Projects\\serena`` when
    resuming on Linux. This helper:

    1. Returns the path as-is when it's already a directory locally.
    2. Translates foreign-OS paths to the current user's home when they follow
       the standard per-user layout (``C:\\Users\\X\\rest`` ↔ ``/home/Y/rest``).
    3. Falls back to ``$HOME`` when no sensible target exists.
    """
    home = str(Path.home())
    if not raw_cwd:
        return home
    if os.path.isdir(raw_cwd):
        return raw_cwd

    if sys.platform == "win32":
        if raw_cwd.startswith("/"):
            m = _POSIX_USER_RE.match(raw_cwd)
            if m:
                rest = (m.group(2) or "").replace("/", os.sep)
                for candidate in _layout_candidates(rest):
                    if candidate.is_dir():
                        return str(candidate)
                if not rest:
                    return home
    else:
        if _WIN_PATH_RE.match(raw_cwd):
            m = _WIN_USER_RE.match(raw_cwd)
            if m:
                rest = (m.group(2) or "").replace("\\", "/")
                for candidate in _layout_candidates(rest):
                    if candidate.is_dir():
                        return str(candidate)
                if not rest:
                    return home
            # WSL fallback: C:\X\Y → /mnt/c/X/Y
            mnt = "/mnt/" + raw_cwd[0].lower() + "/" + raw_cwd[3:].replace("\\", "/")
            if os.path.isdir(mnt):
                return mnt

    return home


def _layout_candidates(rest: str):
    """Yield candidate local paths for a user-relative suffix, swapping the
    Linux ``Documents/Projects/`` layout with the Windows ``Projects/`` layout
    (and vice versa) so cross-OS resumes land in the right repo.
    """
    home = Path.home()
    if not rest:
        yield home
        return
    yield home / rest
    parts = rest.replace("\\", "/").split("/")
    if len(parts) >= 2 and parts[0] == "Documents" and parts[1] == "Projects":
        yield home / "Projects" / Path(*parts[2:]) if parts[2:] else home / "Projects"
    elif parts and parts[0] == "Projects":
        yield home / "Documents" / "Projects" / Path(*parts[1:]) if parts[1:] else home / "Documents" / "Projects"


def claude_project_dir_for(path: str) -> str:
    """Return Claude Code's project-directory name for a filesystem path.

    Claude Code stores session files at ``~/.claude/projects/<name>/<sid>.jsonl``
    where ``<name>`` is the absolute path with ``/``, ``\\``, and ``:`` all
    replaced by ``-``.
    """
    return re.sub(r"[\\/:]", "-", path)


def ensure_session_visible(session_id: str, source_project_dir: str, target_cwd: str) -> None:
    """Make sure ``claude -r <session_id>`` launched from ``target_cwd`` finds the JSONL.

    When a chat is resumed on a different OS, the session file lives under the
    source project dir (e.g. ``C--Users-ragha``) but Claude will look in the
    target cwd's project dir (e.g. ``-home-raghav``). Symlink the JSONL into
    place when missing so Claude can find it without duplicating data.
    """
    target_name = claude_project_dir_for(target_cwd)
    if target_name == source_project_dir:
        return
    src = PROJECTS_DIR / source_project_dir / f"{session_id}.jsonl"
    if not src.exists():
        return
    dst_dir = PROJECTS_DIR / target_name
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{session_id}.jsonl"
    if dst.exists() or dst.is_symlink():
        return
    # Symlink: cheap, appends propagate. On Windows this needs admin/dev-mode,
    # so fall back to hardlink (same volume, no perms needed, appends still
    # propagate), then finally copy.
    try:
        dst.symlink_to(src)
        return
    except OSError:
        pass
    try:
        os.link(src, dst)
        return
    except OSError:
        pass
    try:
        import shutil
        shutil.copy2(src, dst)
    except OSError:
        pass
