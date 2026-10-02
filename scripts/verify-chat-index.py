"""Prove the shipped scanner discovers and retains native interactive chats.

Uses only the standard library and throwaway stores, so release workflows can
test a frozen candidate independently of its source checkout and user metadata.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile

CLAUDE_ID = "10000000-0000-4000-8000-000000000001"
LEGACY_ID = "10000000-0000-4000-8000-000000000002"
TUI_ID = "10000000-0000-4000-8000-000000000003"
EXEC_ID = "10000000-0000-4000-8000-000000000004"
STAMP = "2026-10-02T13:00:00Z"


def verify(command: list[str]) -> None:
    # /tmp/serena-* is intentionally classified as internal agent plumbing.
    with tempfile.TemporaryDirectory(prefix="chat-history-proof-") as scratch:
        root = Path(scratch)
        home = root / "home"
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
                   CLAUDE_DIR=str(home / ".claude"), CODEX_HOME=str(home / ".codex"),
                   CHATS_DATA_DIR=str(root / "index"), SERENA_CONFIG_DIR=str(root / "config"),
                   XDG_CONFIG_HOME=str(root / "config"), XDG_DATA_HOME=str(root / "data"),
                   XDG_STATE_HOME=str(root / "state"), MEMORY_DIR=str(root / "memory"),
                   KNOWLEDGE_DIR=str(root / "knowledge"))
        claude = home / ".claude/projects/project" / f"{CLAUDE_ID}.jsonl"
        claude.parent.mkdir(parents=True)
        claude.write_text(json.dumps({
            "type": "user", "sessionId": CLAUDE_ID, "cwd": str(root), "timestamp": STAMP,
            "message": {"role": "user", "content": "An independent Claude chat"},
        }) + "\n", encoding="utf-8")
        sessions = home / ".codex/sessions/2026/10/02"
        sessions.mkdir(parents=True)

        def rollout(sid: str, source: str, originator: str) -> Path:
            path = sessions / f"rollout-2026-10-02T13-00-00-{sid}.jsonl"
            records = [
                {"type": "session_meta", "payload": {
                    "id": sid, "source": source, "originator": originator,
                    "cli_version": "0.160.0", "cwd": str(root), "timestamp": STAMP,
                }},
                {"type": "event_msg", "timestamp": STAMP, "payload": {
                    "type": "user_message", "message": "Keep my chat in history",
                }},
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            return path

        def refresh() -> dict[str, tuple]:
            done = subprocess.run([*command, "--index-refresh"], env=env, cwd=root,
                                  capture_output=True, text=True, timeout=90)
            if done.returncode:
                raise RuntimeError(f"Packaged index refresh failed: {done.stderr[-2000:]}")
            with closing(sqlite3.connect(root / "index/index.db")) as conn:
                return {row[0]: row[1:] for row in conn.execute(
                    "SELECT session_id, parent_session_id, is_teammate, is_archived, custom_title "
                    "FROM sessions")}

        rollout(LEGACY_ID, "cli", "codex_cli_rs")
        assert refresh().get(LEGACY_ID) == (None, 0, 0, None), "Legacy chat is missing or hidden"
        tui = rollout(TUI_ID, "vscode", "codex-tui")
        rollout(EXEC_ID, "exec", "codex_exec")
        rows = refresh()
        assert rows.get(TUI_ID) == (None, 0, 0, None), "New Codex TUI chat is missing, hidden or nested"
        assert EXEC_ID not in rows, "Unowned background exec leaked into chat history"

        # Preserve an actual user's title across an unchanged scan and an append.
        # Deliberately omit resident_work: rescue metadata must not mask the bug.
        metadata = home / ".claude/projects/.chats-meta"
        metadata.mkdir(exist_ok=True)
        (metadata / f"{TUI_ID}.json").write_text(json.dumps({"custom_title": "Keep this chat"}))
        for append in (False, True):
            if append:
                with tui.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"type": "event_msg", "timestamp": STAMP,
                        "payload": {"type": "agent_message", "message": "Still here"}}) + "\n")
            assert refresh().get(TUI_ID) == (None, 0, 0, "Keep this chat"), "Chat vanished on refresh"
        print("Packaged chat history: new and existing Codex chats survive refresh without rescue metadata")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    args = parser.parse_args()
    verify([str(args.binary.resolve(strict=True))])
