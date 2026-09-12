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
import re
from collections.abc import Iterator
from contextlib import suppress
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



def conversation_id_for(file_path) -> str:
    """The conversation id behind an indexed path, whichever form it took."""
    fp = Path(file_path)
    if fp.name == "transcript.jsonl":
        return fp.parent.parent.parent.name
    return fp.stem


def resumable_conversation_path(conversation_id: str) -> Path | None:
    """A transcript is readable history, not Antigravity's resume state."""
    if not conversation_id or not re.fullmatch(r"[A-Za-z0-9_-]+", conversation_id):
        return None
    for suffix in (".db", ".pb"):
        candidate = CONVERSATIONS_DIR / (conversation_id + suffix)
        if candidate.is_file():
            return candidate
    return None


def transcript_path(conversation_id: str) -> Path | None:
    """The readable transcript for a conversation, when Antigravity kept one.

    A conversation that exists in both forms is indexed by its ``.db``, because
    that is the file whose mtime moves. The protobuf inside it is undocumented,
    but Antigravity writes the same turns as plain JSONL under ``brain/``, so
    anything wanting to READ a conversation resolves the id to that instead.
    Only conversations predating the brain directory lack one.
    """
    if not conversation_id:
        return None
    candidate = (
        GEMINI_ROOT / "brain" / conversation_id
        / ".system_generated" / "logs" / "transcript.jsonl"
    )
    return candidate if candidate.is_file() else None


# Antigravity wraps each prompt in tagged blocks: the typed text in
# <USER_REQUEST>, and around it the local time, model switches, and open editor
# tabs. Only the request is what the person actually said.
_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)
_TRAILING_BLOCK_RE = re.compile(
    r"<(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|EPHEMERAL_MESSAGE)>"
)


def _typed_text(content: str) -> str:
    found = _USER_REQUEST_RE.findall(content or "")
    if found:
        return "\n\n".join(part.strip() for part in found if part.strip()).strip()
    # An untagged prompt is still a prompt; the metadata blocks follow it.
    return _TRAILING_BLOCK_RE.split(content or "", maxsplit=1)[0].strip()


def read_turns(path) -> list[dict]:
    """A conversation as ordered turns: role, text, tool call, timestamp.

    The ``.db`` beside this file is protobuf with no published schema, but
    Antigravity writes the same conversation here as plain JSONL, one object per
    step. ``USER_INPUT`` is the person and ``PLANNER_RESPONSE`` is the model,
    which can carry prose and several tool calls in one step.

    ``GENERIC`` steps are tool OUTPUT echoed back -- including, verbatim,
    earlier lines of this same transcript whenever a command happens to read it
    -- so they are dropped the way tool results are for every other agent.
    Keeping them would have a briefing quote the log to itself.
    """
    turns: list[dict] = []
    try:
        handle = Path(path).open("r", encoding="utf-8", errors="replace")
    except OSError:
        return turns
    with handle as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                step = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(step, dict):
                continue
            kind = step.get("type")
            stamp = str(step.get("created_at") or "")
            if kind == "USER_INPUT":
                text = _typed_text(step.get("content") or "")
                if text:
                    turns.append({"role": "user", "text": text, "timestamp": stamp,
                                  "tool_name": None, "tool_input": None})
                continue
            if kind != "PLANNER_RESPONSE":
                continue
            text = (step.get("content") or "").strip()
            if text:
                turns.append({"role": "assistant", "text": text, "timestamp": stamp,
                              "tool_name": None, "tool_input": None})
            for call in step.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                args = call.get("args")
                turns.append({
                    "role": "assistant",
                    "text": "",
                    "timestamp": stamp,
                    "tool_name": str(call.get("name") or "tool"),
                    "tool_input": json.dumps(args) if args is not None else None,
                })
    return turns


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
            if workspace:
                cwd = workspace
                break
        if cwd:
            break

    if not cwd:
        from core.metadata import get_meta
        cwd = get_meta(conversation_id).get('gemini_workspace') or ''
    native_turns = []
    if not typed:
        native_transcript = transcript_path(conversation_id)
        if native_transcript:
            native_turns = [turn for turn in read_turns(native_transcript) if turn['role'] == 'user']
            if native_turns:
                first_message = native_turns[0]['text'][:500]
                with suppress(ValueError):
                    first_timestamp = datetime.fromisoformat(native_turns[0]['timestamp'].replace('Z', '+00:00'))

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
        message_count=len(typed) or len(native_turns),
        raw_message_count=len(mine) or len(native_turns),
        model="gemini",
        slug=claude_project_dir_for(cwd) if cwd else "gemini",
        file_path=str(file_path),
        file_size=stat.st_size,
        file_mtime=stat.st_mtime,
    )
