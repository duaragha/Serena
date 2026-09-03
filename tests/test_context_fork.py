from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from chats import context_fork
from core import indexer
from ui import web


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def test_context_fork_contains_both_linked_transcripts_without_tool_payloads(
    tmp_path,
    monkeypatch,
) -> None:
    claude_path = tmp_path / "claude.jsonl"
    codex_path = tmp_path / ".codex" / "sessions" / "codex.jsonl"
    _write_jsonl(
        claude_path,
        [
            {
                "type": "user",
                "timestamp": "2026-08-10T10:00:00Z",
                "message": {"role": "user", "content": "claude user context"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-08-10T10:01:00Z",
                "message": {
                    "id": "a1",
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "claude answer context"},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "secret-command"}},
                    ],
                },
            },
            {
                "type": "user",
                "timestamp": "2026-08-10T10:02:00Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "secret-tool-output"}],
                },
            },
        ],
    )
    _write_jsonl(
        codex_path,
        [
            {
                "type": "event_msg",
                "timestamp": "2026-08-10T10:03:00Z",
                "payload": {"type": "user_message", "message": "codex user context"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-10T10:04:00Z",
                "payload": {"type": "agent_message", "message": "codex answer context"},
            },
            {
                "type": "response_item",
                "timestamp": "2026-08-10T10:05:00Z",
                "payload": {"type": "function_call_output", "output": "raw-broker-payload"},
            },
        ],
    )
    sessions = {
        "claude-chat": {
            "session_id": "claude-chat",
            "agent": "claude",
            "display_title": "Linked work",
            "cwd": str(tmp_path),
            "file_path": str(claude_path),
            "last_timestamp": "2026-08-10T10:02:00Z",
        },
        "codex-chat": {
            "session_id": "codex-chat",
            "agent": "codex",
            "display_title": "Linked work",
            "cwd": str(tmp_path),
            "file_path": str(codex_path),
            "last_timestamp": "2026-08-10T10:05:00Z",
        },
    }
    monkeypatch.setattr(context_fork, "get_session", lambda sid: sessions.get(sid))
    monkeypatch.setattr(context_fork.metadata, "get_group", lambda _sid: "g-linked")
    monkeypatch.setattr(
        context_fork.metadata,
        "list_group_members",
        lambda _gid: ["claude-chat", "codex-chat"],
    )
    monkeypatch.setattr(context_fork.metadata, "get_meta", lambda _sid: {})

    result = context_fork.build_context_fork(
        "claude-chat",
        "codex",
        output_dir=tmp_path / "forks",
    )

    body = Path(result["context_path"]).read_text(encoding="utf-8")
    assert result["source_session_ids"] == ["claude-chat", "codex-chat"]
    assert result["message_count"] == 4
    assert "claude user context" in body
    assert "claude answer context" in body
    assert "codex user context" in body
    assert "codex answer context" in body
    assert "secret-command" not in body
    assert "secret-tool-output" not in body
    assert "raw-broker-payload" not in body
    assert "standalone branch" in result["prompt"]


def test_fleet_codex_workers_are_never_nested_under_origin_chats(monkeypatch) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            agent TEXT,
            cwd TEXT,
            first_timestamp TEXT,
            last_timestamp TEXT,
            file_path TEXT,
            originator TEXT,
            parent_session_id TEXT
        )
        """
    )
    rows = [
        ("claude-origin", "claude", "/repo", "2026-08-10T10:00:00Z", "2026-08-10T11:00:00Z", "/missing", "", None),
        ("fleet-codex", "codex", "/repo", "2026-08-10T10:30:00Z", "2026-08-10T10:40:00Z", "/missing", "codex_exec:exec", "claude-origin"),
        ("ordinary-codex", "codex", "/repo", "2026-08-10T10:35:00Z", "2026-08-10T10:45:00Z", "/missing", "codex_exec:exec", None),
    ]
    connection.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    monkeypatch.setattr(
        indexer.meta_sync,
        "get_meta",
        lambda sid: {"fleet_worker": {"run_id": "fleet-1"}} if sid == "fleet-codex" else {},
    )
    monkeypatch.setattr(indexer, "_find_claude_parent", lambda *_args: "claude-origin")

    indexer._attribute_codex_parents(connection)

    parents = dict(connection.execute("SELECT session_id, parent_session_id FROM sessions"))
    assert parents["fleet-codex"] is None
    assert parents["ordinary-codex"] == "claude-origin"


def test_context_fork_route_and_right_click_actions(monkeypatch, tmp_path) -> None:
    expected = {
        "ok": True,
        "target_agent": "claude",
        "context_path": str(tmp_path / "context.md"),
        "cwd": str(tmp_path),
        "title": "Fork: Linked work",
        "source_session_ids": ["claude-chat", "codex-chat"],
        "message_count": 12,
        "prompt": "read the context",
    }
    monkeypatch.setattr(context_fork, "build_context_fork", lambda *_args, **_kwargs: expected)
    client = web.app.test_client()

    response = client.post(
        "/api/context-fork",
        json={"source_sid": "claude-chat", "target_agent": "claude"},
    )
    page = client.get("/").get_data(as_text=True)

    assert response.status_code == 200
    assert response.get_json() == expected
    # The rows are built from one agent list rather than spelled out, so that
    # adding a third agent cannot leave a destination unreachable.
    assert "'Fork context → ' + _agentLabel(agent)" in page
    assert "const _HANDOFF_AGENTS = ['claude', 'codex', 'gemini']" in page
    assert "function forkLinkedContext(srcSid, targetAgent)" in page
    assert "pending_group_link_with:" not in page.split(
        "async function forkLinkedContext", 1
    )[1].split("// Voice coding jobs", 1)[0]
