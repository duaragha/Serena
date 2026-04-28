"""Scanner + parser for Codex CLI session files.

Codex stores sessions at::

    ~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl

Each .jsonl starts with a ``session_meta`` line containing the session id, cwd,
model, and CLI version, followed by event records (task_started, message
events, etc.).

This module exposes a ``scan_codex_sessions()`` generator that mirrors the
shape of ``core.scanner.scan_sessions()`` so the indexer can consume both
agents through a single pipeline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from core.parser import SessionMeta

CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"

# rollout-2026-04-28T13-47-04-019dd533-a211-7e32-8040-e53e98d3a9b7.jsonl
_FILENAME_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-([0-9a-f-]{36})\.jsonl$"
)


@dataclass
class CodexFile:
    path: Path
    session_id: str


def scan_codex_sessions() -> Iterator[tuple[str, Path]]:
    """Yield (project_dir_pseudo, jsonl_path) tuples for every Codex session
    that originated from a real CLI invocation.

    Codex's ``~/.codex/sessions/`` also stores sessions spawned by other
    agents (e.g. Claude calling codex via MCP — those carry
    ``originator: "Claude Code"`` in their session_meta). We only surface
    sessions where the user ran codex directly: ``codex``, ``codex exec``,
    or any future codex-native CLI variant whose originator name starts
    with "codex".
    """
    if not CODEX_SESSIONS_ROOT.exists():
        return
    for jsonl in CODEX_SESSIONS_ROOT.rglob("rollout-*.jsonl"):
        if not jsonl.is_file():
            continue
        if not _is_user_initiated(jsonl):
            continue
        yield "codex", jsonl


def _is_user_initiated(file_path: Path) -> bool:
    """Read the first line and check the originator field. True iff the
    session was started directly via the codex CLI (not by another agent)."""
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return False
    if not first.strip():
        return False
    try:
        obj = json.loads(first)
    except json.JSONDecodeError:
        return False
    payload = obj.get("payload") or {}
    originator = (payload.get("originator") or "").lower()
    # Whitelist anything codex-native; exclude "Claude Code", "anthropic", etc.
    return originator.startswith("codex")


def _slugify_cwd(cwd: str) -> str:
    """Turn an absolute cwd into a Claude-style slug for the project_dir column.

    Matches Claude's convention: ``/home/raghav/foo`` -> ``-home-raghav-foo``.
    """
    if not cwd:
        return "codex"
    # Strip drive prefix on Windows
    if len(cwd) >= 2 and cwd[1] == ":":
        return cwd[0].upper() + "--" + cwd[3:].replace("\\", "-").replace("/", "-")
    norm = cwd.replace("\\", "/")
    return norm.replace("/", "-")


def _coerce_datetime(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Codex emits ISO-8601 with trailing 'Z'
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def parse_codex_metadata(file_path: Path) -> SessionMeta | None:
    """Read a Codex rollout .jsonl and return a SessionMeta-compatible record."""
    m = _FILENAME_RE.match(file_path.name)
    if not m:
        return None
    sid = m.group(1)

    session_meta: dict | None = None
    first_user_msg: str | None = None
    last_ts: datetime | None = None
    msg_count = 0
    raw_count = 0
    last_turn_model: str | None = None

    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                raw_count += 1
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts_str = obj.get("timestamp")
                ts = _coerce_datetime(ts_str)
                if ts is not None:
                    last_ts = ts

                kind = obj.get("type")
                if kind == "session_meta" and session_meta is None:
                    session_meta = obj.get("payload") or {}
                elif kind == "turn_context":
                    # Each turn records the model that ran it. Track the most
                    # recent so the indexed value reflects the session's
                    # current model (in case user switched mid-session).
                    payload = obj.get("payload") or {}
                    if payload.get("model"):
                        last_turn_model = str(payload["model"])
                elif kind == "event_msg":
                    payload = obj.get("payload") or {}
                    inner_kind = payload.get("type")
                    if inner_kind == "user_message" and first_user_msg is None:
                        text = payload.get("message") or payload.get("text") or ""
                        if isinstance(text, str) and text.strip():
                            first_user_msg = text.strip()[:500]
                    if inner_kind in ("user_message", "agent_message", "assistant_message"):
                        msg_count += 1
    except OSError:
        return None

    if session_meta is None:
        return None

    cwd = session_meta.get("cwd") or ""
    project_dir = _slugify_cwd(cwd) if cwd else "codex"

    first_ts = _coerce_datetime(session_meta.get("timestamp"))

    try:
        stat = file_path.stat()
        file_size = stat.st_size
        file_mtime = stat.st_mtime
    except OSError:
        file_size = 0
        file_mtime = 0.0

    # Model resolution priority:
    #   1. Last `turn_context` event's model (this is where codex actually
    #      records the model used per turn — e.g. "gpt-5.4")
    #   2. session_meta's model field (rare in current codex versions)
    #   3. fall back to the provider name only as a last resort
    model = last_turn_model or session_meta.get("model") or session_meta.get("model_provider") or "codex"

    return SessionMeta(
        session_id=sid,
        project_dir=project_dir,
        cwd=cwd,
        last_cwd=cwd,
        device=_current_device_tag(),
        first_message=first_user_msg or "",
        first_timestamp=first_ts,
        last_timestamp=last_ts or first_ts,
        message_count=msg_count,
        raw_message_count=raw_count,
        is_teammate=False,
        model=str(model),
        git_branch=None,
        slug=project_dir,
        file_path=str(file_path),
        file_size=file_size,
        file_mtime=file_mtime,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_create_tokens=0,
    )


def _current_device_tag() -> str:
    import platform
    sysname = platform.system().lower()
    if sysname == "linux":
        return "linux"
    if sysname == "darwin":
        return "macos"
    if sysname == "windows":
        return "windows"
    return sysname
