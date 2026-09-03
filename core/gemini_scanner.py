"""Index Gemini (Antigravity) conversations without decoding their transcripts.

Google retired Google-account sign-in for the standalone `gemini` CLI and moved
individuals onto the Antigravity suite, so `agy` IS the Gemini agent now. It
stores each conversation as a SQLite database whose `steps` table holds
protobuf blobs with numeric step types and no published schema. Reading those
would mean hand-walking an undocumented wire format that a preview product is
free to change under us, which is exactly the trap Codex sprang twice in one
fortnight.

None of that is needed to list a chat. Antigravity also keeps
``history.jsonl``, one line per prompt, carrying the conversation id, the
workspace it ran in, the text the user typed, and a timestamp. That is the
title, the cwd, the first message and the ordering — everything the sidebar
shows. It matched the conversations on disk exactly in both directions when
this was written, so it is the index, and the databases are consulted only for
their size and mtime.

The transcript itself stays with Antigravity: `agy --conversation <id>` reopens
it. Serena lists and launches; it does not try to re-render.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from core.parser import SessionMeta

GEMINI_ROOT = Path.home() / ".gemini" / "antigravity-cli"
CONVERSATIONS_DIR = GEMINI_ROOT / "conversations"
HISTORY_FILE = GEMINI_ROOT / "history.jsonl"

# The agent name Serena uses everywhere. The binary is `agy`; the product the
# user recognises is Gemini.
AGENT = "gemini"


def _conversation_files() -> dict[str, Path]:
    """Every conversation on disk, keyed by id. The stem IS the id."""
    found: dict[str, Path] = {}
    if CONVERSATIONS_DIR.is_dir():
        for path in CONVERSATIONS_DIR.iterdir():
            if path.is_file() and path.suffix in {".db", ".pb"}:
                found[path.stem] = path
    brain_dir = GEMINI_ROOT / "brain"
    if brain_dir.is_dir():
        for path in brain_dir.iterdir():
            if path.is_dir() and path.name not in found:
                t = path / ".system_generated" / "logs" / "transcript.jsonl"
                if t.is_file():
                    found[path.name] = t
    return found



def _history_entries() -> list[dict]:
    """Prompt history, oldest first. Unreadable lines are skipped."""
    try:
        raw = HISTORY_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _moment(value) -> datetime | None:
    """Antigravity writes epoch milliseconds."""
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _is_typed(entry: dict) -> bool:
    """Slash commands are control, not conversation, and make poor titles."""
    if entry.get("type") == "slash_command":
        return False
    text = (entry.get("display") or "").strip()
    return bool(text) and not text.startswith("/")


def scan_gemini_sessions() -> Iterator[tuple[str, Path]]:
    """Yield ``(agent, conversation_path)`` for each Gemini conversation."""
    for path in _conversation_files().values():
        yield AGENT, path


def parse_gemini_metadata(file_path: Path) -> SessionMeta | None:
    """Build a session row for one conversation, from history plus the file."""
    fp = Path(file_path)
    if fp.name == "transcript.jsonl":
        conversation_id = fp.parent.parent.parent.name
    else:
        conversation_id = fp.stem
    if not conversation_id:
        return None


    mine = [e for e in _history_entries() if e.get("conversationId") == conversation_id]

    typed = [e for e in mine if _is_typed(e)]
    first_message = (typed[0].get("display") or "").strip()[:500] if typed else ""

    stamps = [m for m in (_moment(e.get("timestamp")) for e in mine) if m]
    first_timestamp = min(stamps) if stamps else None

    try:
        stat = Path(file_path).stat()
    except OSError:
        return None

    # The file is written on every turn, so its mtime is the end of the
    # conversation even for turns history.jsonl never recorded.
    last_timestamp = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    if stamps:
        last_timestamp = max(last_timestamp, max(stamps))

    import re

    # A slash command is recorded with whatever directory it was typed in,
    # which is often not where the conversation's work happens. Take the
    # workspace from the most recent real prompt, and only fall back to a
    # control entry when there is nothing else to go on.
    cwd = ""
    for source in (reversed(typed), reversed(mine)):
        for entry in source:
            workspace = (entry.get("workspace") or "").strip()
            if workspace and workspace not in ("/home/raghav", "C:/Users/ragha", "C:\\Users\\ragha"):
                cwd = workspace
                break
        if cwd:
            break

    # If workspace was generic home or empty, resolve the real project from transcript or prompts
    if not cwd or cwd in ("/home/raghav", "C:/Users/ragha", "C:\\Users\\ragha"):
        transcript_file = GEMINI_ROOT / "brain" / conversation_id / ".system_generated" / "logs" / "transcript.jsonl"
        counts: dict[str, int] = {}
        if transcript_file.exists():
            try:
                with open(transcript_file, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        for m in re.findall(r"/home/raghav/Documents/Projects/([a-zA-Z0-9_\-]+)", line):
                            counts[m] = counts.get(m, 0) + 1
            except OSError:
                pass
        if counts:
            top_proj = max(counts.items(), key=lambda x: x[1])[0]
            cwd = str(Path.home() / "Documents" / "Projects" / top_proj)
        else:
            found = False
            for entry in typed:
                disp = entry.get("display", "")
                m = re.search(r"/home/raghav/Documents/Projects/([a-zA-Z0-9_\-]+)", disp)
                if m:
                    cwd = str(Path.home() / "Documents" / "Projects" / m.group(1))
                    found = True
                    break
            if not found:
                cwd = str(Path.home() / "Documents" / "Projects" / "serena")

    from core.config import claude_project_dir_for
    from core.codex_scanner import _current_device_tag


    return SessionMeta(
        session_id=conversation_id,
        project_dir=claude_project_dir_for(cwd) if cwd else "gemini",
        cwd=cwd,
        last_cwd=cwd,
        device=_current_device_tag(),
        first_message=first_message,
        first_timestamp=first_timestamp or last_timestamp,
        last_timestamp=last_timestamp,
        # Prompts are all history.jsonl records, so this counts what the user
        # said, not the assistant's replies. Reporting only the half we can
        # actually see beats inventing the other one.
        message_count=len(typed),
        raw_message_count=len(mine),
        model="gemini",
        slug=claude_project_dir_for(cwd) if cwd else "gemini",
        file_path=str(file_path),
        file_size=stat.st_size,
        file_mtime=stat.st_mtime,
    )
