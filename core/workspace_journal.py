"""Ordered disk-backed events for custom panes that can disconnect independently.

Provider transcripts remain canonical history. This journal preserves the live
control/event stream across renderer reconnects without retaining it all in RAM.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path


class WorkspaceJournal:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create private storage before SQLite also creates its WAL/SHM files.
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS workspace_events (
                session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event TEXT NOT NULL,
                PRIMARY KEY (session_id, sequence)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS workspace_commands (
                session_id TEXT NOT NULL, request_id TEXT NOT NULL,
                payload TEXT NOT NULL, result TEXT,
                PRIMARY KEY (session_id, request_id)
            )""")

    def claim_command(
        self, session_id: str, request_id: str, payload: dict
    ) -> tuple[bool, dict | None]:
        encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload, result FROM workspace_commands WHERE session_id=? AND request_id=?",
                (session_id, request_id),
            ).fetchone()
            if row:
                if row[0] != encoded:
                    raise ValueError("Request ID was already used with different content")
                return False, json.loads(row[1]) if row[1] is not None else None
            conn.execute(
                "INSERT INTO workspace_commands VALUES (?, ?, ?, NULL)",
                (session_id, request_id, encoded),
            )
            return True, None

    def command_receipt(self, session_id: str, request_id: str, payload: dict):
        encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload, result FROM workspace_commands WHERE session_id=? AND request_id=?",
                (session_id, request_id),
            ).fetchone()
        if row is None:
            return False, None
        if row[0] != encoded:
            raise ValueError("Request ID was already used with different content")
        return True, json.loads(row[1]) if row[1] is not None else None

    def finish_command(self, session_id: str, request_id: str, result: dict) -> None:
        with closing(self._connect()) as conn, conn:
            changed = conn.execute(
                "UPDATE workspace_commands SET result=? WHERE session_id=? AND request_id=? AND result IS NULL",
                (json.dumps(result, allow_nan=False), session_id, request_id),
            ).rowcount
            if changed != 1:
                raise ValueError("Command is missing or already finished")

    def fork_checkpoint(self, session_id: str, request_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT event FROM workspace_events WHERE session_id=? "
                "AND json_extract(event, '$.method')='workspace/sessionForked' "
                "AND json_extract(event, '$.params.requestId')=? LIMIT 2",
                (session_id, request_id),
            ).fetchall()
        if len(rows) > 1:
            raise ValueError("Fork checkpoint is ambiguous; creation will not be repeated")
        if not rows:
            return None
        target = json.loads(rows[0][0])["params"]["fork"]
        if not isinstance(target, dict) or target.get("provider") not in {"claude", "codex"} or any(
            not isinstance(target.get(key), str) or not target[key] for key in ("session_id", "cwd")
        ) or target["session_id"] == session_id:
            raise ValueError("Fork checkpoint has an invalid identity")
        return target

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def append(self, session_id: str, event: dict) -> dict:
        if (
            not session_id
            or not isinstance(event, dict)
            or not isinstance(event.get("method"), str)
        ):
            raise ValueError("An identified session and protocol event are required")
        params = event.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("Event params must be an object")
        sid = params.get("threadId")
        if event["method"] == "workspace/history":
            sid = (params.get("thread") or {}).get("id")
        if sid is not None and sid != session_id:
            raise ValueError("Cannot publish another session into this journal")
        encoded = json.dumps(event, ensure_ascii=False, allow_nan=False)
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            last = conn.execute(
                "SELECT MAX(sequence) FROM workspace_events WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            sequence = (last or 0) + 1
            conn.execute(
                "INSERT INTO workspace_events VALUES (?, ?, ?)", (session_id, sequence, encoded)
            )
        return {"sequence": sequence, "event": json.loads(encoded)}

    def latest_sequence(self, session_id: str) -> int:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM workspace_events WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]

    def read(self, session_id: str, *, after: int = 0, limit: int = 200) -> dict:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Invalid replay cursor or page size")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT sequence, event FROM workspace_events
                WHERE session_id=? AND sequence>? ORDER BY sequence LIMIT ?""",
                (session_id, after, limit + 1),
            ).fetchall()
        page = rows[:limit]
        return {
            "events": [{"sequence": seq, "event": json.loads(event)} for seq, event in page],
            "cursor": page[-1][0] if page else after,
            "has_more": len(rows) > limit,
        }
