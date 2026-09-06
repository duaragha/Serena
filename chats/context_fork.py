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

# Every agent whose transcript can be READ, in the order a fork presents them.
# Gemini joined once its Antigravity transcript became readable; before that it
# could receive a fork but never contribute to one.
_FORKABLE_AGENTS = ("claude", "codex", "gemini")
_AGENT_LABELS = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini"}


def _english_list(names: list[str]) -> str:
    if len(names) < 3:
        return " and ".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _read_turns(agent: str, path: Path):
    """Each agent's own reader. Antigravity's transcript is not Claude JSONL,
    and running the Claude parser over it returned zero messages -- a fork that
    silently dropped the Gemini side while claiming to carry it."""
    if agent == "gemini":
        from core.gemini_scanner import read_turns, transcript_path, conversation_id_for

        source = path if path.name == "transcript.jsonl" else transcript_path(
            conversation_id_for(path)
        )
        if source is None:
            return []
        return [
            (turn["role"], turn["text"], turn["timestamp"])
            for turn in read_turns(source)
            if turn["text"].strip()
        ]
    return [
        (m.role, str(m.text or ""), m.timestamp.isoformat() if m.timestamp else "")
        for m in parse_full(path)
    ]


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
    if target not in {"claude", "codex", "gemini"}:
        raise ValueError("target agent must be claude, codex or gemini")

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
        if agent not in _FORKABLE_AGENTS:
            continue
        previous = latest_by_agent.get(agent)
        if previous is None or _activity_key(member) > _activity_key(previous):
            latest_by_agent[agent] = member

    if len(latest_by_agent) < 2:
        raise ValueError(
            "a context fork carries a thread's chats into a fresh one, so the "
            "thread needs at least two chats from different agents to carry"
        )

    # Stable order, so a fork of the same thread reads the same way twice.
    sources = [latest_by_agent[agent] for agent in _FORKABLE_AGENTS if agent in latest_by_agent]
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
    named = _english_list([_AGENT_LABELS[str(item["agent"]).lower()] for item in sources])
    prompt = (
        f"Read the complete linked-chat context at {path}. It contains only user and "
        f"assistant text from the prior {named} chats. This is a new standalone "
        "branch, not part of their linked thread. Use every transcript in it as prior "
        "context, then reply only: context loaded. Wait for my next request."
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
        for role_name, raw, timestamp in _read_turns(agent, path):
            if role_name not in {"user", "assistant"}:
                continue
            text = raw.strip()
            if not text:
                continue
            role = "Raghav" if role_name == "user" else agent.title()
            lines.extend([f"### {role} [{timestamp}]", "", text, ""])
            message_count += 1
    return "\n".join(lines).rstrip() + "\n", message_count
