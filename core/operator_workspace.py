"""Durable, local-only operator state for Chats.

This module deliberately does not own terminal layout or provider processes.
It stores prompts until Raghav explicitly dispatches them, exposes a bounded
inspection projection, and decides whether the focused native runtime can
safely accept a next-turn prompt or a mid-turn correction. The renderer stays
the sole owner of focus, split geometry, and sleeping-process policy.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_OPERATOR_DB = (
    Path.home() / ".local" / "state" / "serena" / "operator-workspace.sqlite3"
)
MAX_PROMPT_CHARS = 8_000
MAX_SEARCH_CHARS = 200
PROMPT_STATES = frozenset(
    {"queued", "paused", "stashed", "dispatching", "sent", "cancelled", "ambiguous"}
)
PROMPT_MODES = frozenset({"next_turn", "correction"})
PROMPT_PROVIDERS = frozenset({"codex", "claude"})
_SESSION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


@dataclass(frozen=True, slots=True)
class QueuedPrompt:
    prompt_id: str
    session_id: str
    provider: str
    text: str
    mode: str
    state: str
    revision: int
    created_at: float
    updated_at: float
    dispatched_at: float | None = None
    terminal_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OperatorWorkspaceStore:
    """Small SQLite journal for prompts that have not entered a provider yet."""

    def __init__(self, path: str | Path | None = None) -> None:
        configured = os.environ.get("SERENA_OPERATOR_DB", "").strip()
        self.path = Path(path or configured or DEFAULT_OPERATOR_DB).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialize()

    def queue_prompt(
        self,
        *,
        session_id: str,
        provider: str,
        text: str,
        mode: str = "next_turn",
    ) -> QueuedPrompt:
        sid = _clean_session_id(session_id)
        clean_provider = str(provider or "").strip().lower()
        if clean_provider not in PROMPT_PROVIDERS:
            raise ValueError("provider must be codex or claude")
        clean_mode = str(mode or "").strip().lower()
        if clean_mode not in PROMPT_MODES:
            raise ValueError("prompt mode must be next_turn or correction")
        clean_text = _clean_prompt(text)
        prompt_id = str(uuid.uuid4())
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO operator_prompts(
                    prompt_id, session_id, provider, text, mode, state,
                    revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', 1, ?, ?)
                """,
                (prompt_id, sid, clean_provider, clean_text, clean_mode, now, now),
            )
            return self._require(connection, prompt_id)

    def get(self, prompt_id: str) -> QueuedPrompt | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM operator_prompts WHERE prompt_id = ?",
                (_clean_prompt_id(prompt_id),),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def list_prompts(
        self,
        *,
        session_id: str = "",
        states: tuple[str, ...] | list[str] | None = None,
        query: str = "",
        limit: int = 100,
    ) -> list[QueuedPrompt]:
        clauses: list[str] = []
        values: list[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            values.append(_clean_session_id(session_id))
        if states:
            clean_states = [str(state).strip().lower() for state in states]
            if any(state not in PROMPT_STATES for state in clean_states):
                raise ValueError("unknown prompt state")
            placeholders = ",".join("?" for _ in clean_states)
            clauses.append(f"state IN ({placeholders})")
            values.extend(clean_states)
        clean_query = " ".join(str(query or "").split())[:MAX_SEARCH_CHARS]
        if clean_query:
            clauses.append("(text LIKE ? ESCAPE '\\' OR session_id LIKE ? ESCAPE '\\')")
            pattern = "%" + _like(clean_query) + "%"
            values.extend((pattern, pattern))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(min(200, max(1, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM operator_prompts"
                + where
                + " ORDER BY updated_at DESC, prompt_id DESC LIMIT ?",
                tuple(values),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def edit_prompt(self, prompt_id: str, text: str) -> QueuedPrompt:
        clean_text = _clean_prompt(text)
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require(connection, prompt_id)
            if current.state not in {"queued", "paused", "stashed"}:
                raise RuntimeError(f"a {current.state} prompt cannot be edited")
            connection.execute(
                "UPDATE operator_prompts SET text = ?, revision = revision + 1, "
                "updated_at = ? WHERE prompt_id = ?",
                (clean_text, now, current.prompt_id),
            )
            return self._require(connection, current.prompt_id)

    def transition(self, prompt_id: str, action: str) -> QueuedPrompt:
        clean_action = str(action or "").strip().lower()
        transitions = {
            "pause": ({"queued"}, "paused"),
            "resume": ({"paused", "stashed"}, "queued"),
            "stash": ({"queued", "paused", "ambiguous"}, "stashed"),
            "cancel": ({"queued", "paused", "stashed", "ambiguous"}, "cancelled"),
        }
        if clean_action not in transitions:
            raise ValueError("prompt action must be pause, resume, stash, or cancel")
        allowed, target = transitions[clean_action]
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require(connection, prompt_id)
            if current.state not in allowed:
                raise RuntimeError(f"cannot {clean_action} a {current.state} prompt")
            connection.execute(
                "UPDATE operator_prompts SET state = ?, updated_at = ? WHERE prompt_id = ?",
                (target, now, current.prompt_id),
            )
            return self._require(connection, current.prompt_id)

    def begin_dispatch(self, prompt_id: str, terminal_id: str) -> QueuedPrompt:
        terminal = str(terminal_id or "").strip()
        if not terminal or len(terminal) > 160:
            raise ValueError("terminal id is required")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require(connection, prompt_id)
            if current.state != "queued":
                raise RuntimeError(f"a {current.state} prompt cannot be dispatched")
            connection.execute(
                "UPDATE operator_prompts SET state = 'dispatching', terminal_id = ?, "
                "updated_at = ? WHERE prompt_id = ?",
                (terminal, now, current.prompt_id),
            )
            return self._require(connection, current.prompt_id)

    def finish_dispatch(
        self,
        prompt_id: str,
        *,
        delivered: bool,
        ambiguous: bool = False,
    ) -> QueuedPrompt:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require(connection, prompt_id)
            if current.state != "dispatching":
                raise RuntimeError("prompt has no active dispatch")
            state = "sent" if delivered else "ambiguous" if ambiguous else "queued"
            connection.execute(
                "UPDATE operator_prompts SET state = ?, dispatched_at = ?, updated_at = ? "
                "WHERE prompt_id = ?",
                (state, now if delivered or ambiguous else None, now, current.prompt_id),
            )
            return self._require(connection, current.prompt_id)

    def _require(self, connection: sqlite3.Connection, prompt_id: str) -> QueuedPrompt:
        row = connection.execute(
            "SELECT * FROM operator_prompts WHERE prompt_id = ?",
            (_clean_prompt_id(prompt_id),),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown queued prompt {prompt_id}")
        return _from_row(row)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS operator_prompts (
                    prompt_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    text TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    state TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    dispatched_at REAL,
                    terminal_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS operator_prompts_session_state
                ON operator_prompts(session_id, state, updated_at);
                """
            )
        if os.name != "nt":
            with suppress(OSError):
                self.path.chmod(0o600)


def steering_capability(runtime: dict[str, Any], mode: str) -> dict[str, Any]:
    """Explain whether this exact renderer runtime may accept a prompt now."""

    clean_mode = str(mode or "").strip().lower()
    if clean_mode not in PROMPT_MODES:
        return {"supported": False, "reason": "unknown prompt mode"}
    if not runtime or not runtime.get("alive"):
        return {"supported": False, "reason": "native runtime is not open"}
    if runtime.get("reserved"):
        return {"supported": False, "reason": "runtime is reserved by Serena work"}
    state = str(runtime.get("state") or "closed")
    if state not in {"live", "paused"}:
        return {"supported": False, "reason": f"runtime state is {state}"}
    busy = bool(runtime.get("busy"))
    draft = bool(runtime.get("draft"))
    provider = str(runtime.get("agent") or "").lower()
    if clean_mode == "correction":
        if provider != "codex":
            return {
                "supported": False,
                "reason": "this native provider has no verified safe mid-turn steering path",
            }
        if not busy:
            return {"supported": False, "reason": "there is no active turn to correct"}
        if draft:
            return {"supported": False, "reason": "an unsent draft already owns the input"}
        if state == "paused":
            return {"supported": False, "reason": "a sleeping runtime cannot be corrected"}
        return {
            "supported": True,
            "reason": "Codex accepts a native queued correction on its active turn",
            "wakes_runtime": False,
        }
    if busy:
        return {"supported": False, "reason": "wait for the active turn or queue a correction"}
    if draft:
        return {"supported": False, "reason": "an unsent draft already owns the input"}
    return {
        "supported": True,
        "reason": "idle native runtime can accept the next turn",
        "wakes_runtime": state == "paused",
    }


def inspect_session(session_id: str) -> dict[str, Any]:
    """Compose context, focus, usage, and diff evidence without waking a chat."""

    sid = _clean_session_id(session_id)
    from core import metadata
    from core.fleet_context import redact_value
    from core.indexer import get_session
    from ui import pty_terminal

    session = get_session(sid)
    if not session:
        raise KeyError(f"unknown session {sid}")
    meta = metadata.get_meta(sid)
    runtime_context = pty_terminal.runtime_context_snapshot()
    runtime = next(
        (item for item in runtime_context.get("runtimes", []) if item.get("sid") == sid),
        None,
    )
    usage = _usage_breakdown(session)
    fleet_marker = meta.get("fleet_worker") if isinstance(meta, dict) else None
    fleet: dict[str, Any] | None = None
    diffs: list[dict[str, Any]] = []
    if isinstance(fleet_marker, dict) and fleet_marker.get("run_id"):
        try:
            from core.fleet_supervisor import inspect_run

            fleet = inspect_run(
                str(fleet_marker["run_id"]),
                str(fleet_marker.get("worker_key") or ""),
                event_limit=40,
            )
            isolation = fleet.get("isolation") or {}
            for entry in isolation.get("integrations") or []:
                if not isinstance(entry, dict):
                    continue
                if (
                    fleet_marker.get("worker_key")
                    and entry.get("worker_key") != fleet_marker.get("worker_key")
                ):
                    continue
                diffs.append(
                    {
                        "worker_key": entry.get("worker_key"),
                        "ok": bool(entry.get("ok")),
                        "reason": entry.get("reason"),
                        "changed_paths": list(entry.get("changed_paths") or []),
                        "dirty_conflicts": list(entry.get("dirty_conflicts") or []),
                        "patch_path": entry.get("patch_path") or "",
                        "test_gate": entry.get("test_gate") or {},
                    }
                )
        except (ImportError, KeyError, RuntimeError, ValueError):
            fleet = None
    projection = {
        "session_id": sid,
        "agent": str(session.get("agent") or ""),
        "title": str(session.get("display_title") or session.get("title") or sid),
        "project": str(session.get("last_cwd") or session.get("cwd") or ""),
        "focus": {
            "focused": runtime_context.get("focused_sid") == sid,
            "focused_session_id": runtime_context.get("focused_sid"),
            "split_pair": list(runtime_context.get("split_pair") or []),
        },
        "runtime": runtime,
        "capabilities": {
            "next_turn": steering_capability(runtime or {}, "next_turn"),
            "correction": steering_capability(runtime or {}, "correction"),
        },
        "context_usage": usage,
        "fleet_worker": fleet_marker if isinstance(fleet_marker, dict) else None,
        "fleet": fleet,
        "diffs": diffs[-20:],
    }
    clean, redactions = redact_value(projection)
    clean["redaction_count"] = redactions
    return clean


def _usage_breakdown(session: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "input_tokens": int(session.get("input_tokens") or 0),
        "output_tokens": int(session.get("output_tokens") or 0),
        "cache_read_tokens": int(session.get("cache_read_tokens") or 0),
        "cache_create_tokens": int(session.get("cache_create_tokens") or 0),
    }
    observed = sum(fields.values())
    billable = fields["input_tokens"] + fields["output_tokens"] + fields["cache_create_tokens"]
    return {
        **fields,
        "observed_tokens": observed,
        "billable_tokens": billable,
        "context_window_tokens": None,
        "utilization_percent": None,
        "note": (
            "provider transcript totals are observable; the native CLI did not expose "
            "a trustworthy live context-window ceiling for this chat"
        ),
    }


def _clean_prompt(value: object) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if not text:
        raise ValueError("prompt text is required")
    if len(text) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt text exceeds {MAX_PROMPT_CHARS} characters")
    return text


def _clean_session_id(value: object) -> str:
    session_id = str(value or "").strip()
    if not _SESSION_ID.fullmatch(session_id):
        raise ValueError("invalid session id")
    return session_id


def _clean_prompt_id(value: object) -> str:
    prompt_id = str(value or "").strip()
    try:
        uuid.UUID(prompt_id)
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("invalid prompt id") from error
    return prompt_id


def _from_row(row: sqlite3.Row) -> QueuedPrompt:
    state = str(row["state"])
    mode = str(row["mode"])
    if state not in PROMPT_STATES or mode not in PROMPT_MODES:
        raise RuntimeError("operator prompt journal contains an invalid state")
    return QueuedPrompt(
        prompt_id=str(row["prompt_id"]),
        session_id=str(row["session_id"]),
        provider=str(row["provider"]),
        text=str(row["text"]),
        mode=mode,
        state=state,
        revision=int(row["revision"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        dispatched_at=(
            float(row["dispatched_at"]) if row["dispatched_at"] is not None else None
        ),
        terminal_id=str(row["terminal_id"] or ""),
    )


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def json_projection(value: Any) -> str:
    """Stable debug representation used by the local command palette."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
