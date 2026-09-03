"""Create a standalone chat seed from both sides of a linked conversation."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import metadata
from core.indexer import get_session
from core.parser import parse_full

DEFAULT_CONTEXT_FORK_DIR = Path.home() / ".local" / "share" / "serena" / "context-forks"


def build_context_fork(
    source_session_id: str,
    target_agent: str,
    *,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Write a complete text-only transcript bundle for a fresh agent chat."""

    source_id = str(source_session_id or "").strip()
    target = str(target_agent or "").strip().lower()
    if not source_id:
        raise ValueError("source session id is required")
    if target not in {"claude", "codex"}:
        raise ValueError("target agent must be claude or codex")

    source = get_session(source_id)
    if source is None:
        raise ValueError(f"session {source_id} was not found")
    source_id = str(source["session_id"])
    if metadata.get_meta(source_id).get("fleet_worker"):
        raise ValueError("Fleet worker chats cannot be used as linked-chat forks")

    group_id = metadata.get_group(source_id)
    if not group_id:
        raise ValueError("this chat is not linked to another chat")

    latest_by_agent: dict[str, dict[str, Any]] = {}
    for member_id in metadata.list_group_members(group_id):
        if metadata.get_meta(member_id).get("fleet_worker"):
            continue
        member = get_session(member_id)
        if member is None:
            continue
        agent = str(member.get("agent") or "claude").strip().lower()
        if agent not in {"claude", "codex"}:
            continue
        previous = latest_by_agent.get(agent)
        if previous is None or _activity_key(member) > _activity_key(previous):
            latest_by_agent[agent] = member

    if set(latest_by_agent) != {"claude", "codex"}:
        raise ValueError("the linked thread needs both a Claude chat and a Codex chat")

    sources = [latest_by_agent["claude"], latest_by_agent["codex"]]
    rendered, message_count = _render_context(sources, group_id=group_id)
    root = Path(
        output_dir
        or os.environ.get("SERENA_CONTEXT_FORK_DIR", "").strip()
        or DEFAULT_CONTEXT_FORK_DIR
    ).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"context-fork-{uuid.uuid4().hex}.md"
    path.write_text(rendered, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass

    source_title = str(
        source.get("display_title") or source.get("custom_title") or source.get("title") or "Chat"
    ).strip()
    title = f"Fork: {source_title}"[:120]
    cwd = str(source.get("cwd") or source.get("last_cwd") or Path.home())
    if not Path(cwd).expanduser().is_dir():
        cwd = str(Path.home())
    prompt = (
        f"Read the complete linked-chat context at {path}. It contains only user and "
        "assistant text from the prior Claude and Codex chats. This is a new standalone "
        "branch, not part of their linked thread. Use both transcripts as prior context, "
        "then reply only: context loaded. Wait for my next request."
    )
    return {
        "ok": True,
        "target_agent": target,
        "context_path": str(path),
        "cwd": cwd,
        "title": title,
        "source_session_ids": [str(item["session_id"]) for item in sources],
        "message_count": message_count,
        "prompt": prompt,
    }


def _activity_key(session: dict[str, Any]) -> tuple[str, str]:
    return (
        str(session.get("last_timestamp") or session.get("first_timestamp") or ""),
        str(session.get("session_id") or ""),
    )


def _render_context(
    sessions: list[dict[str, Any]],
    *,
    group_id: str,
) -> tuple[str, int]:
    lines = [
        "# Serena Standalone Context Fork",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Source linked group: {group_id}",
        "",
        "This file contains user and assistant text only. Tool calls, tool results,",
        "system records, and raw runtime payloads are intentionally excluded.",
        "",
    ]
    message_count = 0
    for session in sessions:
        agent = str(session.get("agent") or "claude").strip().lower()
        title = str(
            session.get("display_title")
            or session.get("custom_title")
            or session.get("title")
            or "Untitled chat"
        ).strip()
        lines.extend(
            [
                f"## {agent.title()} Chat: {title}",
                "",
                f"Session: {session['session_id']}",
                f"Working directory: {session.get('cwd') or session.get('last_cwd') or ''}",
                "",
            ]
        )
        path = Path(str(session.get("file_path") or ""))
        if not path.exists():
            raise ValueError(f"source transcript is missing for {session['session_id']}")
        for message in parse_full(path):
            if message.role not in {"user", "assistant"}:
                continue
            text = str(message.text or "").strip()
            if not text:
                continue
            role = "Raghav" if message.role == "user" else agent.title()
            timestamp = message.timestamp.isoformat() if message.timestamp else ""
            lines.extend([f"### {role} [{timestamp}]", "", text, ""])
            message_count += 1
    return "\n".join(lines).rstrip() + "\n", message_count
