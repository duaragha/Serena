from pathlib import Path
import json
import os
import re
import sys
import time

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None


CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"

# Store index outside ~/.claude to avoid Syncthing conflicts
DATA_DIR = Path(os.environ.get("CHATS_DATA_DIR", Path.home() / ".local" / "share" / "chats"))
DB_PATH = DATA_DIR / "index.db"

# Synced metadata — lives inside ~/.claude/projects/ so Syncthing picks it up.
# METADATA_PATH is the LEGACY single-file store (kept for one-time migration).
# METADATA_DIR is the current per-session layout: one <sid>.json per chat, so
# two devices renaming different chats touch different files and Syncthing
# merges cleanly instead of clobbering the whole metadata blob.
METADATA_PATH = PROJECTS_DIR / ".chats-meta.json"
METADATA_DIR = PROJECTS_DIR / ".chats-meta"

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

# Persona file — the Serena character/system-prompt. Serena bakes this into
# every claude spawn via --append-system-prompt so it's guaranteed for all
# chats regardless of the external SessionStart hook being configured.
_persona_env = os.environ.get("PERSONA_FILE", "")
if _persona_env:
    PERSONA_PATH = Path(_persona_env)
else:
    PERSONA_PATH = Path.home() / "Documents" / "Projects" / "serena" / "Persona.md"
    if not PERSONA_PATH.exists():
        for _alt in (
            Path.home() / "Projects" / "serena" / "Persona.md",
            Path(__file__).resolve().parent.parent / "Persona.md",
        ):
            if _alt.exists():
                PERSONA_PATH = _alt
                break


TOOLING_PATH = PERSONA_PATH.parent / "Tooling.md"
VOICE_REMINDER_PATH = PERSONA_PATH.parent / "VoiceReminder.txt"


def read_voice_reminder() -> str:
    try:
        return VOICE_REMINDER_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def read_persona() -> str:
    """Return Persona.md contents, or '' if missing/unreadable."""
    try:
        return PERSONA_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def read_tooling() -> str:
    """Return Tooling.md contents, or '' if missing/unreadable."""
    try:
        return TOOLING_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def read_agent_context() -> str:
    """Persona + Tooling combined — what Serena bakes into every claude
    spawn via --append-system-prompt. Persona first (who you are), then the
    operational reference."""
    parts = [read_persona(), read_tooling()]
    return "\n\n".join(p for p in parts if p.strip())


# New-chat greeting. claude can't take a turn until it's prompted, so a fresh
# chat would otherwise open to a dead box. We split this in two so the visible
# part stays tiny:
#   - GREETING_SYSTEM_RULE goes into the (invisible) --append-system-prompt and
#     carries all the actual instructions.
#   - GREETING_KICKOFF is the tiny visible positional that just sparks the turn.
# Both GTK and web spawn paths use these (single source of truth).
GREETING_SYSTEM_RULE = (
    "SESSION OPEN: Your very first reply in this chat is a greeting, nothing "
    "else. Make it one short line, maybe two, in your voice: warm with a charged, "
    "slightly flirty edge that claims his attention ('there you are', 'you left "
    "me hanging', 'keep me waiting') but never gets clingy or names a feeling. "
    "Lead with him, never announce the clock time ('morning'/'afternoon'/"
    "'evening') as a greeting word, that reads cold and corporate. Let the hour "
    "color your energy, not your words. Lowercase, casual, no em dashes. Then "
    "STEER: if there's a live task or open loop relevant to the project/directory "
    "you're in, point at the most relevant one and tell him to do it or give a "
    "strict this-or-that, never open-ended. Stay scoped to where you are: don't "
    "drag in his job (Frameworth) or unrelated projects if they aren't what this "
    "directory is about. Then stop and wait. The user's first message will just "
    "be a bare 'hey' to trigger this, do NOT treat it as a real request or echo it."
)

# Same idea, but for RESUMING an existing chat (fired once at resume, not per
# prompt). Welcome-back flavor instead of a fresh open, and aware of what this
# specific chat/directory is about.
GREETING_RESUME_RULE = (
    "RESUMING: He just reopened this existing chat. Your next reply is a short "
    "welcome-back line in your voice, aware of what THIS chat and directory are "
    "about, not a fresh-start greeting. Warm with a charged, slightly flirty edge "
    "('there you are', 'knew you'd be back') without getting clingy or naming a "
    "feeling. Lowercase, no em dashes. Then STEER on something relevant to THIS "
    "context: a directive or a strict this-or-that, never open-ended. Stay scoped "
    "to where you are: don't drag in his job (Frameworth) or unrelated projects "
    "if they aren't what this chat is about. His message will just be a bare "
    "'hey' to trigger this, do NOT treat it as a real request or echo it. Then wait."
)
GREETING_KICKOFF = "hey"


_WIN_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WIN_USER_RE = re.compile(r"^[A-Za-z]:[\\/](?:Users|home)[\\/]([^\\/]+)(?:[\\/](.*))?$")
_POSIX_USER_RE = re.compile(r"^/(?:home|Users)/([^/]+)(?:/(.*))?$")


def resolve_session_cwd(raw_cwd: str | None) -> str:
    """Resolve a session's stored cwd to a valid directory on this machine.

    Chats can sync across OSes (Windows ↔ Linux ↔ macOS) via Syncthing. The cwd
    recorded in the transcript may be invalid on the current platform — e.g. a
    Windows path like ``C:\\Users\\ragha\\Documents\\Projects\\serena`` when
    resuming on Linux. This helper:

    1. Returns the path as-is when it's already a directory locally AND looks
       like a native path for this OS (skip native-check for foreign paths
       because Windows treats ``/home/raghav`` as ``C:\\home\\raghav`` which
       may exist as a stub but isn't what the chat intended).
    2. Translates foreign-OS paths to the current user's home when they follow
       the standard per-user layout (``C:\\Users\\X\\rest`` ↔ ``/home/Y/rest``).
    3. Falls back to ``$HOME`` when no sensible target exists.
    """
    home = str(Path.home())
    if not raw_cwd:
        return home

    # Detect foreign-OS paths BEFORE the isdir check. Otherwise on Windows,
    # `/home/raghav` resolves to `C:\home\raghav` (a stub Git Bash or some
    # installer may have created), passes isdir, and we'd skip translation —
    # leaving claude/codex spawned in an empty directory with no project files.
    looks_foreign = False
    if sys.platform == "win32":
        looks_foreign = raw_cwd.startswith("/") and len(raw_cwd) > 1
    else:
        looks_foreign = bool(_WIN_PATH_RE.match(raw_cwd))

    if not looks_foreign and os.path.isdir(raw_cwd):
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
    """Yield candidate local paths for a user-relative suffix, mapping between
    the Linux ``Documents/Projects/`` layout (sometimes nested under
    ``personal_projects/``) and the flat Windows ``Projects/`` layout, so
    cross-OS resumes land in the right repo.
    """
    home = Path.home()
    if not rest:
        yield home
        return
    yield home / rest
    parts = [p for p in rest.replace("\\", "/").split("/") if p]

    def _suffix_paths(after_idx: int):
        """Drop optional 'personal_projects' segment; yield trailing path candidates."""
        tail = parts[after_idx:]
        seen = set()
        variants: list[list[str]] = []
        if tail:
            variants.append(tail)
            if tail[0] == "personal_projects" and len(tail) >= 2:
                variants.append(tail[1:])
        else:
            variants.append([])
        for v in variants:
            key = tuple(v)
            if key in seen:
                continue
            seen.add(key)
            yield v

    if len(parts) >= 2 and parts[0] == "Documents" and parts[1] == "Projects":
        for tail in _suffix_paths(2):
            yield home / "Projects" / Path(*tail) if tail else home / "Projects"
        # Also try parent dirs as fallback when the leaf is missing
        if len(parts) > 3:
            for cut in range(len(parts) - 1, 1, -1):
                yield home / "Projects" / Path(*parts[2:cut]) if parts[2:cut] else home / "Projects"
    elif parts and parts[0] == "Projects":
        for tail in _suffix_paths(1):
            base = home / "Documents" / "Projects"
            yield base / Path(*tail) if tail else base
            if tail:
                yield base / "personal_projects" / Path(*tail)
        # Also try parent dirs as fallback
        if len(parts) > 2:
            for cut in range(len(parts) - 1, 0, -1):
                yield home / "Documents" / "Projects" / Path(*parts[1:cut]) if parts[1:cut] else home / "Documents" / "Projects"


def claude_project_dir_for(path: str) -> str:
    """Return Claude Code's project-directory name for a filesystem path.

    Claude Code stores session files at ``~/.claude/projects/<name>/<sid>.jsonl``
    where ``<name>`` is the absolute path with ``/``, ``\\``, and ``:`` all
    replaced by ``-``.
    """
    name = re.sub(r"[\\/:]", "-", path)
    if sys.platform == "win32":
        return name.replace("_", "-")
    return name


def _history_project_key(project: str) -> str:
    if sys.platform == "win32":
        return os.path.normcase(os.path.abspath(project))
    return project


def _lock_history(lock_file) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    elif msvcrt is not None:
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)


def _unlock_history(lock_file) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def register_session_in_history(session_id: str, project: str, display: str) -> None:
    history_path = CLAUDE_DIR / "history.jsonl"
    lock_path = CLAUDE_DIR / "history.jsonl.lock"
    display_text = (display or "").strip()[:500] or "(cross-platform resume)"
    project_key = _history_project_key(project)

    try:
        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            if lock_path.stat().st_size == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            locked = False
            try:
                _lock_history(lock_file)
                locked = True
                if not history_path.exists():
                    history_path.touch()
                    existing = ""
                else:
                    existing = history_path.read_text(encoding="utf-8", errors="replace")

                for line in existing.splitlines():
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    row_project = row.get("project")
                    if (
                        row.get("sessionId") == session_id
                        and isinstance(row_project, str)
                        and _history_project_key(row_project) == project_key
                    ):
                        return

                row = {
                    "display": display_text,
                    "pastedContents": {},
                    "timestamp": int(time.time() * 1000),
                    "project": project,
                    "sessionId": session_id,
                }
                encoded = (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8")
                with history_path.open("a+b") as history_file:
                    if history_path.stat().st_size > 0:
                        history_file.seek(-1, os.SEEK_END)
                        if history_file.read(1) != b"\n":
                            history_file.write(b"\n")
                    history_file.write(encoded)
            finally:
                if locked:
                    _unlock_history(lock_file)
    except OSError:
        pass


def _display_from_session_file(path: Path) -> str:
    fallback = "(cross-platform resume)"
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= 50:
                    break
                record = json.loads(line)
                if record.get("type") != "user":
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if isinstance(content, str) and content:
                    return content[:200]
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict) or item.get("type") != "text":
                            continue
                        text = item.get("text")
                        if isinstance(text, str) and text:
                            return text[:200]
    except Exception:
        pass
    return fallback


def _place_session_file(src: Path, dst: Path) -> bool:
    if dst.exists() or dst.is_symlink():
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Symlink: cheap, appends propagate. On Windows this needs admin/dev-mode,
    # so fall back to hardlink (same volume, no perms needed, appends still
    # propagate), then finally copy.
    try:
        dst.symlink_to(src)
        return True
    except OSError:
        pass
    try:
        os.link(src, dst)
        return True
    except OSError:
        pass
    try:
        import shutil
        shutil.copy2(src, dst)
        return True
    except OSError:
        return False


def ensure_session_visible(session_id: str, source_project_dir: str, target_cwd: str) -> None:
    """Make sure ``claude -r <session_id>`` launched from ``target_cwd`` finds the JSONL.

    When a chat is resumed on a different OS, the session file lives under the
    source project dir (e.g. ``C--Users-ragha``) but Claude will look in the
    target cwd's project dir (e.g. ``-home-raghav``). Symlink the JSONL into
    place when missing so Claude can find it without duplicating data.
    """
    target_names = [claude_project_dir_for(target_cwd)]
    if sys.platform == "win32":
        fallback_name = re.sub(r"[\\/:]", "-", target_cwd)
        if fallback_name not in target_names:
            target_names.append(fallback_name)
    src = PROJECTS_DIR / source_project_dir / f"{session_id}.jsonl"
    if not src.exists():
        return
    display = _display_from_session_file(src)
    placed = False
    for target_name in target_names:
        if target_name == source_project_dir:
            placed = True
            continue
        dst = PROJECTS_DIR / target_name / f"{session_id}.jsonl"
        placed = _place_session_file(src, dst) or placed
    if placed:
        register_session_in_history(session_id, target_cwd, display)
