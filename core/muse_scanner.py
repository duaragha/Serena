"""Index Muse sessions from their on-disk event logs.

Muse keeps one directory per session at
``~/.local/share/muse/sessions/YYYY/MM/DD/<session-id>/`` with a
``session.jsonl`` event log inside it. The id is the directory name, the file
mtime orders the sidebar, and the log itself is plain JSONL, so unlike
Antigravity's protobuf there is no decoding trap: every reader here walks
several plausible event shapes and skips what it does not recognise, and a log
in a shape this code has never seen yields an empty listing rather than a
wrong one.

The transcript stays with Muse: ``muse resume <session-id>`` reopens it.
Serena lists, launches, and briefs from a defensive text extraction; it does
not try to re-render tool streams.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from core.parser import SessionMeta

MUSE_ROOT = (
    Path(os.environ.get("XDG_DATA_HOME", "").strip() or Path.home() / ".local" / "share") / "muse"
)
SESSIONS_DIR = MUSE_ROOT / "sessions"

# The agent name Serena uses everywhere.
AGENT = "muse"

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def _object(value) -> dict:
    return value if isinstance(value, dict) else {}


def _session_files() -> dict[str, Path]:
    """Every session log on disk, keyed by id. The directory IS the id."""
    found: dict[str, Path] = {}
    root = SESSIONS_DIR
    if not root.is_dir():
        return found
    try:
        # Only root conversations. Recursive discovery includes subagent and
        # reminder transcripts, which must not become standalone sidebar chats.
        candidates = sorted(root.glob("*/*/*/*/session.jsonl"))
    except OSError:
        return found
    for path in candidates:
        try:
            if not path.is_file():
                continue
            session_id = path.parent.name
        except OSError:
            continue
        if _ID_RE.fullmatch(session_id) and session_id not in found:
            found[session_id] = path
    return found


def session_id_for(file_path) -> str:
    """The session id behind an indexed path."""
    fp = Path(file_path)
    if fp.name == "session.jsonl":
        return fp.parent.name
    return fp.stem


def resumable_session_path(session_id: str) -> Path | None:
    """The native log a resume needs, or None when it is not on this machine."""
    if not session_id or not _ID_RE.fullmatch(session_id):
        return None
    matches = sorted(SESSIONS_DIR.glob(f"*/*/*/{session_id}/session.jsonl"))
    for candidate in matches:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def transcript_path(session_id: str) -> Path | None:
    """Muse logs are already readable JSONL, so this is the resume target."""
    return resumable_session_path(session_id)


def _iter_events(path: Path) -> Iterator[dict]:
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    yield event
    except OSError:
        return


def _event_text(event: dict) -> str:
    """Best-effort assistant/user text from one event, across known shapes."""
    payload = _object(event.get("payload"))
    if event.get("payload_type") == "runtime.user_intent.accepted":
        messages = payload.get("model_messages")
        if not isinstance(messages, list):
            return ""
        return "\n".join(
            block["text"] for message in messages if isinstance(message, dict)
            and isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("kind") == "text"
            and isinstance(block.get("text"), str)
        ).strip()
    if event.get("payload_type") == "runtime.session" and payload.get("kind") == "run":
        native = _object(payload.get("event"))
        return str(native.get("prompt" if native.get("kind") == "started" else "text") or "").strip()
    for scope in (event, event.get("message"), event.get("payload"), event.get("data")):
        if not isinstance(scope, dict):
            continue
        if scope.get("type") == "agent_message":
            text = scope.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
        content = scope.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        return text.strip()
        elif isinstance(content, str) and content.strip():
            return content.strip()
        for key in ("result", "response", "output", "text", "answer", "prompt"):
            value = scope.get(key)
            if isinstance(value, str) and value.strip():
                stripped = value.strip()
                if not (stripped.startswith("{") and stripped.endswith("}")):
                    return stripped
    return ""


def _event_role(event: dict) -> str:
    payload = _object(event.get("payload"))
    if event.get("payload_type") == "runtime.user_intent.accepted":
        # Internal peer/reminder deliveries are not user messages.
        return "user" if _object(payload.get("semantic_kind")).get("kind") == "chat" else ""
    if event.get("payload_type") == "runtime.session" and payload.get("kind") == "run":
        kind = _object(payload.get("event")).get("kind")
        return {"started": "user", "assistant_message_committed": "assistant"}.get(kind, "")
    for scope in (event, event.get("message"), event.get("payload")):
        if not isinstance(scope, dict):
            continue
        role = str(scope.get("role") or "").strip().lower()
        if role in {"user", "assistant"}:
            return role
        kind = str(scope.get("type") or scope.get("event") or "").strip().lower()
        if kind in {"user", "user_input", "user_message"}:
            return "user"
        if kind in {"assistant", "agent_message", "result", "response"}:
            return "assistant"
    return ""


def _event_moment(event: dict) -> str:
    # Native durable records use microseconds since epoch, not seconds.
    value = event.get("recorded_at")
    if isinstance(value, (int, float)) and value > 0:
        with suppress(ValueError, OSError, OverflowError):
            return datetime.fromtimestamp(value / 1_000_000, tz=timezone.utc).isoformat()
    for key in ("timestamp", "created_at", "at", "time"):
        value = event.get(key)
        if isinstance(value, (int, float)) and value > 0:
            with suppress(ValueError, OSError, OverflowError):
                return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def read_turns(path) -> list[dict]:
    """A session as ordered turns: role, text, timestamp.

    Tool calls and tool results are dropped the way they are for every other
    agent: a briefing that quotes the log to itself is worse than none.
    """
    turns: list[dict] = []
    accepted = set()
    for event in _iter_events(Path(path)):
        payload = _object(event.get("payload"))
        if event.get("payload_type") == "runtime.user_intent.accepted":
            if isinstance(payload.get("intent_id"), str):
                accepted.add(payload["intent_id"])
        elif (payload.get("kind") == "run"
              and _object(payload.get("event")).get("kind") == "started"
              and isinstance(payload.get("run_id"), str)
              and payload.get("run_id") in accepted):
            continue
        role = _event_role(event)
        if role not in {"user", "assistant"}:
            continue
        text = _event_text(event)
        if not text:
            continue
        turns.append(
            {
                "role": role,
                "text": text,
                "timestamp": _event_moment(event),
                "tool_name": None,
                "tool_input": None,
            }
        )
    return turns


def _event_workspace(path: Path) -> str:
    """The checkout a session ran in, when the log says so."""
    for event in _iter_events(path):
        payload = _object(event.get("payload"))
        for scope in (event, event.get("message"), payload, payload.get("record")):
            if not isinstance(scope, dict):
                continue
            for key in ("workspace_root", "workspace", "cwd", "working_directory", "workdir"):
                value = scope.get(key)
                if isinstance(value, str) and value.strip():
                    candidate = Path(value.strip()).expanduser()
                    if candidate.is_absolute():
                        return str(candidate)
    return ""


def scan_muse_sessions() -> Iterator[tuple[str, Path]]:
    """Yield ``(agent, session_log)`` for each Muse session on disk."""
    for path in _session_files().values():
        yield AGENT, path


def parse_muse_metadata(file_path: Path) -> SessionMeta | None:
    """Build a session row for one log, from the log plus the file."""
    fp = Path(file_path)
    session_id = fp.parent.name if fp.name == "session.jsonl" else fp.stem
    if not session_id or not _ID_RE.fullmatch(session_id):
        return None
    try:
        stat = fp.stat()
    except OSError:
        return None

    turns = read_turns(fp)
    user_turns = [turn for turn in turns if turn["role"] == "user"]
    if not user_turns:
        # Startup-only logs are not chats. A newly created Serena conversation
        # remains accessible through the host's pending-creation catalog.
        return None
    first_message = user_turns[0]["text"][:500] if user_turns else ""
    first_timestamp = None
    if user_turns and user_turns[0]["timestamp"]:
        try:
            first_timestamp = datetime.fromisoformat(
                user_turns[0]["timestamp"].replace("Z", "+00:00")
            )
        except ValueError:
            first_timestamp = None
    last_timestamp = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

    cwd = _event_workspace(fp)
    if not cwd:
        try:
            from core.metadata import get_meta

            cwd = get_meta(session_id).get("muse_workspace") or ""
        except Exception:
            cwd = ""

    from core.codex_scanner import _current_device_tag
    from core.config import claude_project_dir_for

    return SessionMeta(
        session_id=session_id,
        project_dir=claude_project_dir_for(cwd) if cwd else "muse",
        cwd=cwd,
        last_cwd=cwd,
        device=_current_device_tag(),
        first_message=first_message,
        first_timestamp=first_timestamp or last_timestamp,
        last_timestamp=last_timestamp,
        message_count=len(turns),
        raw_message_count=len(turns),
        model="muse",
        slug=claude_project_dir_for(cwd) if cwd else "muse",
        file_path=str(fp),
        file_size=stat.st_size,
        file_mtime=stat.st_mtime,
    )


def is_canonical_session_id(value: str) -> bool:
    """Muse ids are UUID-shaped; the catalog requires exact UUIDs."""
    try:
        return str(UUID(str(value))) == str(value)
    except (ValueError, TypeError):
        return False
