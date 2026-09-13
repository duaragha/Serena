"""Ordered disk-backed events for custom panes that can disconnect independently.

Provider transcripts remain canonical history. This journal preserves the live
control/event stream across renderer reconnects without retaining it all in RAM.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID


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
            conn.execute("BEGIN IMMEDIATE")
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
            conn.execute("""CREATE TABLE IF NOT EXISTS workspace_clears (
                source_id TEXT NOT NULL, request_id TEXT NOT NULL,
                target_id TEXT NOT NULL UNIQUE, target TEXT NOT NULL,
                committed INTEGER NOT NULL DEFAULT 0 CHECK (committed IN (0, 1)),
                PRIMARY KEY (source_id, request_id)
            )""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(workspace_clears)")}
            if "created_at" not in columns:
                conn.execute("ALTER TABLE workspace_clears ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
            if "cataloged" not in columns:
                conn.execute("ALTER TABLE workspace_clears ADD COLUMN cataloged INTEGER NOT NULL DEFAULT 0")
            conn.execute("""CREATE TABLE IF NOT EXISTS workspace_creations (
                request_id TEXT PRIMARY KEY, target_id TEXT NOT NULL UNIQUE,
                target TEXT NOT NULL, created_at TEXT NOT NULL,
                committed INTEGER NOT NULL DEFAULT 0 CHECK (committed IN (0, 1))
            )""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(workspace_creations)")}
            if "cataloged" not in columns:
                conn.execute("ALTER TABLE workspace_creations ADD COLUMN cataloged INTEGER NOT NULL DEFAULT 0")

    def saved_codex_mode(self, session_id: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute("""SELECT event FROM workspace_events
                WHERE session_id=? AND json_extract(event, '$.method')='workspace/settings'
                AND json_type(event, '$.params.collaborationMode') IS NOT NULL
                ORDER BY sequence DESC LIMIT 1""", (session_id,)).fetchone()
        if row is None:
            return None
        mode = json.loads(row[0])["params"]["collaborationMode"]
        if not isinstance(mode, str) or mode not in {"plan", "default"}:
            raise ValueError("Saved Codex mode is invalid")
        return mode

    def saved_codex_speed(self, session_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute("""SELECT event FROM workspace_events
                WHERE session_id=? AND json_extract(event, '$.method')='workspace/speed'
                AND sequence > COALESCE((SELECT MAX(sequence) FROM workspace_events
                    WHERE session_id=? AND json_extract(event, '$.method')='workspace/settingReset'
                    AND json_extract(event, '$.params.setting')='speed'), 0)
                ORDER BY sequence DESC LIMIT 1""", (session_id, session_id)).fetchone()
        if row is None:
            return None
        value = json.loads(row[0])["params"]
        if (not isinstance(value, dict) or set(value) != {"model", "value"}
                or not isinstance(value["model"], str) or not value["model"]
                or (value["value"] is not None and (not isinstance(value["value"], str) or not value["value"]))):
            raise ValueError("Saved Codex speed is invalid")
        return value

    def saved_codex_personality(self, session_id: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute("""SELECT event FROM workspace_events
                WHERE session_id=? AND json_extract(event, '$.method')='workspace/settings'
                AND json_type(event, '$.params.personality') IS NOT NULL
                AND COALESCE(json_extract(event, '$.params.personalityConfirmed'), 1) != 0
                AND sequence > COALESCE((SELECT MAX(sequence) FROM workspace_events
                    WHERE session_id=? AND json_extract(event, '$.method')='workspace/settingReset'
                    AND json_extract(event, '$.params.setting')='personality'), 0)
                ORDER BY sequence DESC LIMIT 1""", (session_id, session_id)).fetchone()
        if row is None:
            return None
        value = json.loads(row[0])["params"]["personality"]
        if not isinstance(value, str) or value not in {"none", "friendly", "pragmatic"}:
            raise ValueError("Saved Codex personality is invalid")
        return value

    def saved_codex_setting_revision(self, session_id, setting):
        if setting not in {"personality", "speed"}:
            raise ValueError("Unknown recoverable preference")
        with closing(self._connect()) as conn:
            return conn.execute("""SELECT COALESCE(MAX(sequence), 0) FROM workspace_events WHERE session_id=? AND (
                (json_extract(event, '$.method')='workspace/settingReset' AND json_extract(event, '$.params.setting')=?)
                OR (?='speed' AND json_extract(event, '$.method')='workspace/speed')
                OR (?='personality' AND json_extract(event, '$.method')='workspace/settings'
                    AND json_type(event, '$.params.personality') IS NOT NULL
                    AND COALESCE(json_extract(event, '$.params.personalityConfirmed'), 1) != 0))""",
                                (session_id, setting, setting, setting)).fetchone()[0]

    def reset_saved_codex_setting(self, session_id, setting, expected, failure_id, expected_revision):
        if setting not in {"personality", "speed"} or not isinstance(failure_id, str) or not failure_id:
            raise ValueError("An exact recoverable setting and failure identity are required")
        reader = self.saved_codex_personality if setting == "personality" else self.saved_codex_speed
        with closing(self._connect()) as conn, conn:
            # Reserve the writer before reading so concurrent journal updates
            # cannot change the preference between comparison and reset.
            conn.execute("BEGIN IMMEDIATE")
            if (self.saved_codex_setting_revision(session_id, setting) != expected_revision
                    or reader(session_id) != expected):
                raise ValueError("The saved setting changed; retry connection before recovery")
            sequence = (conn.execute("SELECT MAX(sequence) FROM workspace_events WHERE session_id=?",
                                     (session_id,)).fetchone()[0] or 0) + 1
            event = {"method": "workspace/settingReset", "params": {"setting": setting, "failure_id": failure_id}}
            conn.execute("INSERT INTO workspace_events VALUES (?, ?, ?)",
                         (session_id, sequence, json.dumps(event)))

    def pending_target(self, session_id: str) -> dict | None:
        target = self.clear_target(session_id, uncataloged_only=True)
        if target:
            return target
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT target, committed FROM workspace_creations WHERE target_id=? AND cataloged=0", (session_id,)).fetchone()
        return {**json.loads(row[0]), "committed": bool(row[1])} if row else None

    def recoverable_bridge_queue(self, session_id: str, provider: str) -> list[dict]:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT event FROM workspace_events WHERE session_id=? AND json_extract(event, '$.method')='workspace/bridgeQueue' ORDER BY sequence DESC LIMIT 1",
                               (session_id,)).fetchone()
            if row is None:
                return []
            params = json.loads(row[0]).get("params", {})
            requests = params.get("requests")
            if params.get("threadId") != session_id or not isinstance(requests, list) or params.get("count") != len(requests):
                raise ValueError("Saved bridge queue is invalid")
            recovered, seen = [], set()
            for item in requests:
                if (not isinstance(item, dict) or set(item) - {"id", "prompt", "message"} or not {"id", "prompt"} <= set(item)
                        or not isinstance(item["id"], str) or not 1 <= len(item["id"]) <= 100
                        or item["id"] in seen or not isinstance(item["prompt"], str) or not item["prompt"].strip()):
                    raise ValueError("Saved bridge request is invalid")
                if "message" in item and (not isinstance(item["message"], dict)
                        or set(item["message"]) - {"inputs", "options"}
                        or not isinstance(item["message"].get("inputs"), list)
                        or not item["message"]["inputs"]
                        or not isinstance(item["message"].get("options", {}), dict)):
                    raise ValueError("Saved queued message is invalid")
                seen.add(item["id"])
                command = conn.execute("SELECT payload, result FROM workspace_commands WHERE session_id=? AND request_id=?",
                                       (session_id, "bridge:" + item["id"])).fetchone()
                if command is None or json.loads(command[0]).get("provider") != provider:
                    raise ValueError("Saved bridge request does not match this session provider")
                if command[1] is None:
                    recovered.append(item)
            return recovered

    def uncataloged_targets(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT target, created_at FROM workspace_creations WHERE committed=1 AND cataloged=0").fetchall()
        return sorted(self.uncataloged_clears() + [{**json.loads(target), "created_at": created} for target, created in rows],
                      key=lambda target: target["created_at"], reverse=True)

    def mark_target_cataloged(self, session_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE workspace_clears SET cataloged=1 WHERE target_id=? AND committed=1", (session_id,))
            conn.execute("UPDATE workspace_creations SET cataloged=1 WHERE target_id=? AND committed=1", (session_id,))

    def prepare_creation(self, request_id: str, target: dict) -> None:
        sid = target.get("session_id")
        if (not isinstance(sid, str) or str(UUID(sid)) != sid
                or target.get("provider") not in {"codex", "claude", "gemini"} or set(target) != {"session_id", "provider", "cwd"}
                or not isinstance(target.get("cwd"), str) or not Path(target["cwd"]).is_absolute()):
            raise ValueError("Exact native creation target required")
        encoded = json.dumps(target, sort_keys=True, allow_nan=False)
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            command = conn.execute("SELECT payload, result FROM workspace_commands WHERE session_id=? AND request_id=?",
                                   ("new:" + request_id, request_id)).fetchone()
            expected = {"action": "create_session", "payload": {"provider": target["provider"], "cwd": target["cwd"], "confirmed": True}}
            if command:
                supplied = json.loads(command[0]).get("payload", {})
                if "seed" in supplied:
                    seed = supplied["seed"]
                    if not isinstance(seed, str) or not seed or "\0" in seed or len(seed.encode("utf-8")) > 1024 * 1024:
                        raise ValueError("Invalid initial context")
                    expected["payload"]["seed"] = seed
            if not command or json.loads(command[0]) != expected or command[1] is not None:
                raise ValueError("An unfinished explicit creation request is required")
            row = conn.execute("SELECT target FROM workspace_creations WHERE request_id=?", (request_id,)).fetchone()
            if row:
                if row[0] != encoded:
                    raise ValueError("Creation already recorded a different identity")
                return
            conn.execute("INSERT INTO workspace_creations (request_id, target_id, target, created_at) VALUES (?, ?, ?, ?)",
                         (request_id, sid, encoded, datetime.now(timezone.utc).isoformat()))

    def mark_creation_ready(self, request_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            changed = conn.execute("UPDATE workspace_creations SET committed=1 WHERE request_id=?", (request_id,)).rowcount
            if changed != 1:
                raise ValueError("Native creation checkpoint is missing")

    def complete_creation(self, request_id: str, initial: dict | None = None) -> dict:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT target FROM workspace_creations WHERE request_id=?", (request_id,)).fetchone()
            if not row:
                raise ValueError("Native creation checkpoint is missing")
            receipt = {"ok": True, "result": json.loads(row[0])}
            if initial is not None:
                receipt["initial_message"] = initial
            changed = conn.execute("UPDATE workspace_commands SET result=? WHERE session_id=? AND request_id=? AND result IS NULL",
                                   (json.dumps(receipt), "new:" + request_id, request_id)).rowcount
            if changed != 1:
                raise ValueError("Creation request is missing or already finished")
            conn.execute("UPDATE workspace_creations SET committed=1 WHERE request_id=?", (request_id,))
            return receipt

    def creation_target(self, request_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT target, committed FROM workspace_creations WHERE request_id=?", (request_id,)).fetchone()
        return {**json.loads(row[0]), "committed": bool(row[1])} if row else None

    def prepare_clear(self, source_id: str, request_id: str, target: dict) -> None:
        sid = target.get("session_id")
        requested_name = target.get("requestedName")
        name_confirmed = target.get("nameConfirmed")
        name_error = target.get("nameError")
        identity_keys = {"session_id", "provider", "cwd"}
        name_keys = {"requestedName", "nameConfirmed"} | ({"nameError"} if name_error is not None else set())
        named = requested_name is not None
        if (not isinstance(sid, str) or str(UUID(sid)) != sid or sid == source_id
                or target.get("provider") not in {"claude", "codex"} or not isinstance(target.get("cwd"), str)
                or not Path(target["cwd"]).is_absolute()
                or set(target) != identity_keys | (name_keys if named else set())
                or (named and
                    (not isinstance(requested_name, str)
                     or not requested_name or requested_name != requested_name.strip()
                     or len(requested_name) > 1000
                     or any(ord(char) < 32 or ord(char) == 127 for char in requested_name)
                     or type(name_confirmed) is not bool
                     or (name_confirmed and name_error is not None)
                     or (not name_confirmed and
                         (not isinstance(name_error, str) or not name_error or len(name_error) > 1000
                          or any(ord(char) < 32 or ord(char) == 127 for char in name_error)))))):
            raise ValueError("Exact native clear target required")
        encoded = json.dumps(target, sort_keys=True, allow_nan=False)
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            command = conn.execute("SELECT payload, result FROM workspace_commands WHERE session_id=? AND request_id=?",
                                   (source_id, request_id)).fetchone()
            payload = {"confirmed": True, **({"name": requested_name} if named else {})}
            if not command or json.loads(command[0]) != {"action": "clear_session", "payload": payload} or command[1] is not None:
                raise ValueError("An unfinished explicit clear command is required")
            row = conn.execute("SELECT target FROM workspace_clears WHERE source_id=? AND request_id=?",
                               (source_id, request_id)).fetchone()
            if row:
                if row[0] != encoded:
                    raise ValueError("Clear already recorded a different identity")
                return
            conn.execute("INSERT INTO workspace_clears (source_id, request_id, target_id, target, created_at) VALUES (?, ?, ?, ?, ?)",
                         (source_id, request_id, sid, encoded, datetime.now(timezone.utc).isoformat()))

    def uncataloged_clears(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT target, created_at FROM workspace_clears WHERE committed=1 AND cataloged=0 ORDER BY rowid DESC").fetchall()
        return [{**json.loads(target), "created_at": created} for target, created in rows]

    def mark_clear_cataloged(self, session_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE workspace_clears SET cataloged=1 WHERE target_id=? AND committed=1", (session_id,))

    def complete_clear(self, source_id: str, request_id: str) -> dict:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT target FROM workspace_clears WHERE source_id=? AND request_id=?",
                               (source_id, request_id)).fetchone()
            if not row:
                raise ValueError("Native clear checkpoint is missing")
            receipt = {"ok": True, "result": json.loads(row[0])}
            changed = conn.execute("UPDATE workspace_commands SET result=? WHERE session_id=? AND request_id=? AND result IS NULL",
                                   (json.dumps(receipt), source_id, request_id)).rowcount
            if changed != 1:
                raise ValueError("Clear command is missing or already finished")
            conn.execute("UPDATE workspace_clears SET committed=1 WHERE source_id=? AND request_id=?",
                         (source_id, request_id))
            return receipt

    def clear_target(self, session_id: str, *, uncataloged_only=False) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT target, committed FROM workspace_clears WHERE target_id=? AND (?=0 OR cataloged=0)",
                               (session_id, int(uncataloged_only))).fetchone()
        return {**json.loads(row[0]), "committed": bool(row[1])} if row else None

    def has_pending_clear(self, source_id: str) -> bool:
        with closing(self._connect()) as conn:
            return conn.execute("SELECT 1 FROM workspace_clears WHERE source_id=? AND committed=0 LIMIT 1",
                                (source_id,)).fetchone() is not None

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

    def command_record(self, session_id: str, request_id: str):
        """Read a claimed operation's original input and acknowledgement."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload, result FROM workspace_commands WHERE session_id=? AND request_id=?",
                (session_id, request_id),
            ).fetchone()
        return None if row is None else {
            "payload": json.loads(row[0]), "result": json.loads(row[1]) if row[1] is not None else None,
        }

    def has_pending_work(self, session_id: str) -> bool:
        """A pending reserved-dispatch claim is not permission to resend."""
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT 1 FROM workspace_commands WHERE session_id=? "
                "AND request_id LIKE 'work:%' AND result IS NULL LIMIT 1", (session_id,)
            ).fetchone() is not None

    def has_pending_command_conflict(self, session_id: str, allowed: tuple[str, str]) -> bool:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT session_id,request_id FROM workspace_commands "
                "WHERE session_id=? AND result IS NULL",
                (session_id,),
            ).fetchall()
        return any(tuple(row) != allowed for row in rows)

    def has_pending_clear_involving(self, session_id: str) -> bool:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT 1 FROM workspace_clears WHERE committed=0 "
                "AND (source_id=? OR target_id=?) LIMIT 1",
                (session_id, session_id),
            ).fetchone() is not None

    def has_pending_archive_restore(self, session_id: str) -> bool:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT 1 FROM workspace_commands WHERE session_id=? AND result IS NULL "
                "AND json_extract(payload, '$.action')='restore_archive' LIMIT 1", (session_id,)
            ).fetchone() is not None

    @staticmethod
    def _pending_archive_row(conn, session_id):
        rows = conn.execute(
            "SELECT c.session_id,c.request_id FROM workspace_commands AS c "
            "LEFT JOIN workspace_events AS e ON e.session_id=c.session_id "
            "AND json_extract(e.event, '$.method')='workspace/archivePrepared' "
            "AND json_extract(e.event, '$.params.requestId')=c.request_id "
            "WHERE c.result IS NULL AND json_extract(c.payload, '$.action')='archive_session' "
            "AND (c.session_id=? OR EXISTS (SELECT 1 FROM json_each(e.event, '$.params.archive.targets') AS target "
            "WHERE json_extract(target.value, '$.session_id')=?)) LIMIT 2",
            (session_id, session_id),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError("Multiple pending archives require manual inspection")
        return rows[0] if rows else None

    def pending_archive_operation(self, session_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = self._pending_archive_row(conn, session_id)
        return None if row is None else {"session_id": row[0], "request_id": row[1]}

    def has_pending_archive(self, session_id: str) -> bool:
        return self.pending_archive_operation(session_id) is not None

    @staticmethod
    def _pending_delete_row(conn, session_id):
        rows = conn.execute(
            "SELECT c.session_id,c.request_id FROM workspace_commands AS c "
            "LEFT JOIN workspace_events AS e ON e.session_id=c.session_id "
            "AND json_extract(e.event, '$.method')='workspace/deletePrepared' "
            "AND json_extract(e.event, '$.params.requestId')=c.request_id "
            "WHERE c.result IS NULL AND json_extract(c.payload, '$.action')='delete_session' "
            "AND (c.session_id=? OR EXISTS (SELECT 1 FROM json_each(e.event, '$.params.delete.targets') AS target "
            "WHERE json_extract(target.value, '$.session_id')=?)) LIMIT 2",
            (session_id, session_id),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError("Multiple pending deletes require manual inspection")
        return rows[0] if rows else None

    def pending_delete_operation(self, session_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = self._pending_delete_row(conn, session_id)
        return None if row is None else {"session_id": row[0], "request_id": row[1]}

    def has_pending_delete(self, session_id: str) -> bool:
        return self.pending_delete_operation(session_id) is not None

    def claim_archive(self, session_id: str, request_id: str):
        payload = json.dumps(
            {"action": "archive_session", "payload": {"confirmed": True}}, sort_keys=True
        )
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload,result FROM workspace_commands WHERE session_id=? AND request_id=?",
                (session_id, request_id),
            ).fetchone()
            if row:
                if row[0] != payload:
                    raise ValueError("Request ID was already used with different content")
                return False, json.loads(row[1]) if row[1] is not None else None
            if self._pending_archive_row(conn, session_id):
                raise ValueError("Previous archive outcome is unconfirmed; it will not be repeated")
            if self._pending_delete_row(conn, session_id):
                raise ValueError("Delete outcome is unconfirmed; archiving is unavailable")
            if conn.execute(
                "SELECT 1 FROM workspace_commands WHERE session_id=? AND result IS NULL "
                "AND json_extract(payload, '$.action')='restore_archive' LIMIT 1", (session_id,)
            ).fetchone():
                raise ValueError("Archive restoration is unconfirmed")
            conn.execute(
                "INSERT INTO workspace_commands VALUES (?, ?, ?, NULL)",
                (session_id, request_id, payload),
            )
            return True, None

    @staticmethod
    def _validate_archive_checkpoint(source_id, checkpoint):
        targets = checkpoint.get("targets") if isinstance(checkpoint, dict) else None
        if (not isinstance(source_id, str) or str(UUID(source_id)) != source_id
                or not isinstance(checkpoint, dict)
                or set(checkpoint) != {"session_id", "provider", "cwd", "targets"}
                or checkpoint.get("session_id") != source_id or checkpoint.get("provider") != "codex"
                or not isinstance(checkpoint.get("cwd"), str) or not Path(checkpoint["cwd"]).is_absolute()
                or not isinstance(targets, list) or not 1 <= len(targets) <= 4150
                or any(not isinstance(target, dict)
                       or set(target) != {"session_id", "provider", "cwd"}
                       or target.get("provider") != "codex"
                       or not isinstance(target.get("session_id"), str)
                       or str(UUID(target["session_id"])) != target["session_id"]
                       or not isinstance(target.get("cwd"), str) or not Path(target["cwd"]).is_absolute()
                       for target in targets)
                or targets[0]["session_id"] != source_id
                or Path(targets[0]["cwd"]).resolve() != Path(checkpoint["cwd"]).resolve()
                or len({target["session_id"] for target in targets}) != len(targets)):
            raise ValueError("Exact native archive checkpoint required")

    def prepare_archive(self, source_id: str, request_id: str, checkpoint: dict) -> None:
        self._validate_archive_checkpoint(source_id, checkpoint)
        targets = checkpoint["targets"]
        encoded = json.dumps(checkpoint, sort_keys=True, allow_nan=False)
        payload = {"action": "archive_session", "payload": {"confirmed": True}}
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            command = conn.execute(
                "SELECT payload,result FROM workspace_commands WHERE session_id=? AND request_id=?",
                (source_id, request_id),
            ).fetchone()
            if not command or json.loads(command[0]) != payload or command[1] is not None:
                raise ValueError("An unfinished explicit archive command is required")
            expected_operation = (source_id, request_id)
            target_ids = [target["session_id"] for target in targets]
            for identity in target_ids:
                pending = self._pending_archive_row(conn, identity)
                if pending is not None and tuple(pending) != expected_operation:
                    raise ValueError("An archive descendant has another unconfirmed archive")
                if self._pending_delete_row(conn, identity):
                    raise ValueError("An archive descendant has an unconfirmed delete")
            placeholders = ",".join("?" for _ in target_ids)
            if conn.execute(
                f"SELECT 1 FROM workspace_commands WHERE session_id IN ({placeholders}) AND result IS NULL "
                "AND json_extract(payload, '$.action')='restore_archive' LIMIT 1",
                target_ids,
            ).fetchone():
                raise ValueError("An archive descendant has an unconfirmed restoration")
            pending_commands = conn.execute(
                f"SELECT session_id,request_id FROM workspace_commands WHERE session_id IN ({placeholders}) "
                "AND result IS NULL",
                target_ids,
            ).fetchall()
            if any(tuple(row) != expected_operation for row in pending_commands):
                raise ValueError("An archive descendant has another unconfirmed operation")
            rows = conn.execute(
                "SELECT event FROM workspace_events WHERE session_id=? "
                "AND json_extract(event, '$.method')='workspace/archivePrepared' "
                "AND json_extract(event, '$.params.requestId')=? LIMIT 2",
                (source_id, request_id),
            ).fetchall()
            if rows:
                if len(rows) != 1 or json.dumps(json.loads(rows[0][0])["params"]["archive"], sort_keys=True) != encoded:
                    raise ValueError("Archive already recorded a different native family")
                return
            sequence = (conn.execute(
                "SELECT MAX(sequence) FROM workspace_events WHERE session_id=?", (source_id,)
            ).fetchone()[0] or 0) + 1
            event = {"method": "workspace/archivePrepared", "params": {
                "threadId": source_id, "requestId": request_id, "archive": checkpoint,
            }}
            conn.execute(
                "INSERT INTO workspace_events VALUES (?, ?, ?)",
                (source_id, sequence, json.dumps(event, allow_nan=False)),
            )

    def archive_checkpoint(self, session_id: str, request_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT event FROM workspace_events WHERE session_id=? "
                "AND json_extract(event, '$.method')='workspace/archivePrepared' "
                "AND json_extract(event, '$.params.requestId')=? LIMIT 2",
                (session_id, request_id),
            ).fetchall()
        if len(rows) > 1:
            raise ValueError("Archive checkpoint is ambiguous")
        if not rows:
            return None
        checkpoint = json.loads(rows[0][0])["params"]["archive"]
        self._validate_archive_checkpoint(session_id, checkpoint)
        return checkpoint

    def complete_archive(self, session_id: str, request_id: str, receipt: dict) -> dict:
        result = receipt.get("result") if isinstance(receipt, dict) else None
        thread_ids = result.get("thread_ids") if isinstance(result, dict) else None
        if (not isinstance(receipt, dict) or set(receipt) != {"ok", "result"} or receipt.get("ok") is not True
                or not isinstance(result, dict) or result.get("session_id") != session_id
                or result.get("provider") != "codex" or result.get("archived") is not True
                or result.get("cataloged") is not True
                or not isinstance(thread_ids, list) or not thread_ids
                or result.get("thread_count") != len(thread_ids)):
            raise ValueError("Exact completed archive receipt required")
        encoded = json.dumps(receipt, allow_nan=False)
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            command = conn.execute(
                "SELECT payload,result FROM workspace_commands WHERE session_id=? AND request_id=?",
                (session_id, request_id),
            ).fetchone()
            expected = {"action": "archive_session", "payload": {"confirmed": True}}
            if not command or json.loads(command[0]) != expected or command[1] is not None:
                raise ValueError("Archive command is missing or already finished")
            rows = conn.execute(
                "SELECT event FROM workspace_events WHERE session_id=? "
                "AND json_extract(event, '$.method')='workspace/archivePrepared' "
                "AND json_extract(event, '$.params.requestId')=? LIMIT 2",
                (session_id, request_id),
            ).fetchall()
            if len(rows) != 1:
                raise ValueError("Exact native archive checkpoint required")
            checkpoint = json.loads(rows[0][0])["params"]["archive"]
            self._validate_archive_checkpoint(session_id, checkpoint)
            if [target["session_id"] for target in checkpoint["targets"]] != thread_ids:
                raise ValueError("Completed archive family differs from its checkpoint")
            sequence = (conn.execute(
                "SELECT MAX(sequence) FROM workspace_events WHERE session_id=?", (session_id,)
            ).fetchone()[0] or 0) + 1
            event = {"method": "workspace/archived", "params": {
                "threadId": session_id, "threadIds": thread_ids, "count": len(thread_ids),
            }}
            changed = conn.execute(
                "UPDATE workspace_commands SET result=? WHERE session_id=? AND request_id=? AND result IS NULL",
                (encoded, session_id, request_id),
            ).rowcount
            if changed != 1:
                raise ValueError("Archive command is missing or already finished")
            conn.execute(
                "INSERT INTO workspace_events VALUES (?, ?, ?)",
                (session_id, sequence, json.dumps(event, allow_nan=False)),
            )
        return receipt

    def pending_archive_restores(self, session_ids):
        if not isinstance(session_ids, list) or len(session_ids) > 50 or not all(isinstance(sid, str) for sid in session_ids):
            raise ValueError('Expected one bounded catalog page')
        if not session_ids:
            return {}
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT session_id,request_id FROM workspace_commands WHERE session_id IN ({','.join('?' for _ in session_ids)}) "
                "AND result IS NULL AND json_extract(payload, '$.action')='restore_archive'", session_ids).fetchall()
        result = {}
        for sid, request_id in rows:
            if sid in result:
                raise ValueError('Multiple pending archive restorations require manual inspection')
            result[sid] = request_id
        return result

    def pending_archives(self, session_ids):
        if (not isinstance(session_ids, list) or len(session_ids) > 50
                or not all(isinstance(sid, str) for sid in session_ids)):
            raise ValueError("Expected one bounded catalog page")
        if not session_ids:
            return {}
        with closing(self._connect()) as conn:
            return {
                sid: {"session_id": row[0], "request_id": row[1]}
                for sid in session_ids
                if (row := self._pending_archive_row(conn, sid)) is not None
            }

    def pending_deletes(self, session_ids):
        if (not isinstance(session_ids, list) or len(session_ids) > 50
                or not all(isinstance(sid, str) for sid in session_ids)):
            raise ValueError("Expected one bounded catalog page")
        if not session_ids:
            return {}
        with closing(self._connect()) as conn:
            return {
                sid: {"session_id": row[0], "request_id": row[1]}
                for sid in session_ids
                if (row := self._pending_delete_row(conn, sid)) is not None
            }

    def claim_delete(self, session_id: str, request_id: str):
        payload = json.dumps(
            {"action": "delete_session", "payload": {"confirmed": True}}, sort_keys=True
        )
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload,result FROM workspace_commands WHERE session_id=? AND request_id=?",
                (session_id, request_id),
            ).fetchone()
            if row:
                if row[0] != payload:
                    raise ValueError("Request ID was already used with different content")
                return False, json.loads(row[1]) if row[1] is not None else None
            if self._pending_delete_row(conn, session_id):
                raise ValueError("Previous delete outcome is unconfirmed; it will not be repeated")
            if self._pending_archive_row(conn, session_id):
                raise ValueError("Archive outcome is unconfirmed; deletion is unavailable")
            if conn.execute(
                "SELECT 1 FROM workspace_commands WHERE session_id=? AND result IS NULL LIMIT 1",
                (session_id,),
            ).fetchone():
                raise ValueError("Session has another unconfirmed operation")
            conn.execute(
                "INSERT INTO workspace_commands VALUES (?, ?, ?, NULL)",
                (session_id, request_id, payload),
            )
            return True, None

    @staticmethod
    def _validate_delete_checkpoint(source_id, checkpoint):
        from core.workspace_archive import validate_codex_delete_checkpoint

        if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("cwd"), str):
            raise ValueError("Exact native delete checkpoint required")
        targets = checkpoint.get("targets")
        if not isinstance(targets, list) or not 1 <= len(targets) <= 4150:
            raise ValueError("Exact native delete checkpoint required")
        try:
            validate_codex_delete_checkpoint(source_id, Path(checkpoint["cwd"]).resolve(), checkpoint)
        except (TypeError, ValueError) as error:
            raise ValueError("Exact native delete checkpoint required") from error

    def prepare_delete(self, source_id: str, request_id: str, checkpoint: dict) -> None:
        self._validate_delete_checkpoint(source_id, checkpoint)
        targets = checkpoint["targets"]
        encoded = json.dumps(checkpoint, sort_keys=True, allow_nan=False)
        payload = {"action": "delete_session", "payload": {"confirmed": True}}
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            command = conn.execute(
                "SELECT payload,result FROM workspace_commands WHERE session_id=? AND request_id=?",
                (source_id, request_id),
            ).fetchone()
            if not command or json.loads(command[0]) != payload or command[1] is not None:
                raise ValueError("An unfinished explicit delete command is required")
            expected_operation = (source_id, request_id)
            target_ids = [target["session_id"] for target in targets]
            for identity in target_ids:
                pending = self._pending_delete_row(conn, identity)
                if pending is not None and tuple(pending) != expected_operation:
                    raise ValueError("A delete descendant has another unconfirmed delete")
                if self._pending_archive_row(conn, identity):
                    raise ValueError("A delete descendant has an unconfirmed archive")
            placeholders = ",".join("?" for _ in target_ids)
            pending_commands = conn.execute(
                f"SELECT session_id,request_id FROM workspace_commands WHERE session_id IN ({placeholders}) "
                "AND result IS NULL",
                target_ids,
            ).fetchall()
            if any(tuple(row) != expected_operation for row in pending_commands):
                raise ValueError("A delete descendant has another unconfirmed operation")
            if conn.execute(
                f"SELECT 1 FROM workspace_clears WHERE committed=0 AND "
                f"(source_id IN ({placeholders}) OR target_id IN ({placeholders})) LIMIT 1",
                [*target_ids, *target_ids],
            ).fetchone():
                raise ValueError("A delete descendant has an unconfirmed clear handoff")
            rows = conn.execute(
                "SELECT event FROM workspace_events WHERE session_id=? "
                "AND json_extract(event, '$.method')='workspace/deletePrepared' "
                "AND json_extract(event, '$.params.requestId')=? LIMIT 2",
                (source_id, request_id),
            ).fetchall()
            if rows:
                if (len(rows) != 1
                        or json.dumps(json.loads(rows[0][0])["params"]["delete"], sort_keys=True) != encoded):
                    raise ValueError("Delete already recorded a different native family")
                return
            sequence = (conn.execute(
                "SELECT MAX(sequence) FROM workspace_events WHERE session_id=?", (source_id,)
            ).fetchone()[0] or 0) + 1
            event = {"method": "workspace/deletePrepared", "params": {
                "threadId": source_id, "requestId": request_id, "delete": checkpoint,
            }}
            conn.execute(
                "INSERT INTO workspace_events VALUES (?, ?, ?)",
                (source_id, sequence, json.dumps(event, allow_nan=False)),
            )

    def delete_checkpoint(self, session_id: str, request_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT event FROM workspace_events WHERE session_id=? "
                "AND json_extract(event, '$.method')='workspace/deletePrepared' "
                "AND json_extract(event, '$.params.requestId')=? LIMIT 2",
                (session_id, request_id),
            ).fetchall()
        if len(rows) > 1:
            raise ValueError("Delete checkpoint is ambiguous")
        if not rows:
            return None
        checkpoint = json.loads(rows[0][0])["params"]["delete"]
        self._validate_delete_checkpoint(session_id, checkpoint)
        return checkpoint

    def complete_delete(self, session_id: str, request_id: str, receipt: dict) -> dict:
        result = receipt.get("result") if isinstance(receipt, dict) else None
        thread_ids = result.get("thread_ids") if isinstance(result, dict) else None
        required = {
            "session_id", "provider", "cwd", "deleted", "thread_ids", "recovery_dir",
            "catalog_removed", "thread_count",
        }
        if (not isinstance(receipt, dict) or set(receipt) != {"ok", "result"}
                or receipt.get("ok") is not True or not isinstance(result, dict) or set(result) != required
                or result.get("session_id") != session_id or result.get("provider") != "codex"
                or result.get("deleted") is not True or result.get("catalog_removed") is not True
                or not isinstance(thread_ids, list) or not thread_ids
                or result.get("thread_count") != len(thread_ids)):
            raise ValueError("Exact completed delete receipt required")
        encoded = json.dumps(receipt, allow_nan=False)
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            command = conn.execute(
                "SELECT payload,result FROM workspace_commands WHERE session_id=? AND request_id=?",
                (session_id, request_id),
            ).fetchone()
            expected = {"action": "delete_session", "payload": {"confirmed": True}}
            if not command or json.loads(command[0]) != expected or command[1] is not None:
                raise ValueError("Delete command is missing or already finished")
            checkpoint = self._delete_checkpoint_from_connection(conn, session_id, request_id)
            self._validate_delete_checkpoint(session_id, checkpoint)
            if (result["cwd"] != checkpoint["cwd"] or result["recovery_dir"] != checkpoint["recovery_dir"]
                    or thread_ids != [target["session_id"] for target in checkpoint["targets"]]):
                raise ValueError("Completed delete family differs from its checkpoint")
            sequence = (conn.execute(
                "SELECT MAX(sequence) FROM workspace_events WHERE session_id=?", (session_id,)
            ).fetchone()[0] or 0) + 1
            event = {"method": "workspace/deleted", "params": {
                "threadId": session_id, "threadIds": thread_ids, "count": len(thread_ids),
            }}
            changed = conn.execute(
                "UPDATE workspace_commands SET result=? WHERE session_id=? AND request_id=? AND result IS NULL",
                (encoded, session_id, request_id),
            ).rowcount
            if changed != 1:
                raise ValueError("Delete command is missing or already finished")
            conn.execute(
                "INSERT INTO workspace_events VALUES (?, ?, ?)",
                (session_id, sequence, json.dumps(event, allow_nan=False)),
            )
        return receipt

    @staticmethod
    def _delete_checkpoint_from_connection(conn, session_id, request_id):
        rows = conn.execute(
            "SELECT event FROM workspace_events WHERE session_id=? "
            "AND json_extract(event, '$.method')='workspace/deletePrepared' "
            "AND json_extract(event, '$.params.requestId')=? LIMIT 2",
            (session_id, request_id),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError("Exact native delete checkpoint required")
        return json.loads(rows[0][0])["params"]["delete"]

    def claim_archive_restore(self, session_id: str, request_id: str):
        payload = json.dumps({'action': 'restore_archive', 'payload': {'confirmed': True}}, sort_keys=True)
        with closing(self._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT payload,result FROM workspace_commands WHERE session_id=? AND request_id=?',
                               (session_id, request_id)).fetchone()
            if row:
                if row[0] != payload:
                    raise ValueError('Request ID was already used with different content')
                return False, json.loads(row[1]) if row[1] is not None else None
            if conn.execute("SELECT 1 FROM workspace_commands WHERE session_id=? AND result IS NULL "
                            "AND json_extract(payload, '$.action')='restore_archive' LIMIT 1", (session_id,)).fetchone():
                raise ValueError('Previous archive restoration is unconfirmed; it will not be repeated')
            if self._pending_archive_row(conn, session_id):
                raise ValueError("Archive outcome is unconfirmed; restoration is unavailable")
            if self._pending_delete_row(conn, session_id):
                raise ValueError("Delete outcome is unconfirmed; restoration is unavailable")
            conn.execute('INSERT INTO workspace_commands VALUES (?, ?, ?, NULL)', (session_id, request_id, payload))
            return True, None

    def recover_completed_work(self, session_id: str) -> int:
        """Repair lost receipts only from exact acceptance and terminal evidence."""
        recovered = 0
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            commands = conn.execute(
                "SELECT request_id, payload FROM workspace_commands WHERE session_id=? "
                "AND request_id LIKE 'work:%' AND result IS NULL", (session_id,)).fetchall()
            for key, encoded in commands:
                payload = json.loads(encoded)
                if payload.get("action") != "work_submit":
                    continue
                checkpoints = conn.execute(
                    "SELECT event FROM workspace_events WHERE session_id=? "
                    "AND json_extract(event, '$.method')='workspace/workSubmitted' "
                    "AND json_extract(event, '$.params.requestId')=? LIMIT 2", (session_id, key)).fetchall()
                if len(checkpoints) > 1:
                    continue
                checkpoint = (json.loads(checkpoints[0][0])["params"] if checkpoints
                              else self._work_event_checkpoint(conn, session_id, payload))
                if checkpoint is None:
                    continue
                receipt = checkpoint.get("receipt", {})
                turn_id = receipt.get("turn_id")
                if (checkpoint.get("threadId") != session_id or checkpoint.get("payload") != payload
                        or receipt.get("ok") is not True or receipt.get("committed") is not True
                        or receipt.get("session_id") != session_id
                        or type(receipt.get("start_offset")) is not int or receipt["start_offset"] < 0
                        or receipt["start_offset"] != payload.get("start_offset")
                        or not isinstance(turn_id, str) or not turn_id):
                    continue
                completion = conn.execute(
                    "SELECT event FROM workspace_events WHERE session_id=? "
                    "AND json_extract(event, '$.method')='turn/completed' "
                    "AND json_extract(event, '$.params.turn.id')=? ORDER BY sequence DESC LIMIT 1",
                    (session_id, turn_id)).fetchone()
                if not completion:
                    continue
                params = json.loads(completion[0])["params"]
                if (params.get("threadId") != session_id
                        or params["turn"].get("status") not in {"completed", "failed", "interrupted"}):
                    continue
                recovered += conn.execute(
                    "UPDATE workspace_commands SET result=? WHERE session_id=? AND request_id=? AND result IS NULL",
                    (json.dumps(receipt, allow_nan=False), session_id, key)).rowcount
        return recovered

    @staticmethod
    def _work_event_checkpoint(conn, session_id, payload):
        """Infer only a unique, exact text input after the pre-dispatch boundary."""
        boundary = payload.get("event_start")
        if type(boundary) is not int or boundary < 0:
            return None
        rows = conn.execute(
            "SELECT event FROM workspace_events WHERE session_id=? AND sequence>? "
            "AND (json_extract(event, '$.method')='turn/started' OR "
            "(json_extract(event, '$.method') IN ('item/started', 'item/completed') "
            "AND json_extract(event, '$.params.item.type')='userMessage')) "
            "ORDER BY sequence", (session_id, boundary)).fetchall()
        turns, inputs = set(), {}
        for row in rows:
            event = json.loads(row[0])
            params = event.get("params", {})
            if params.get("threadId") != session_id:
                return None
            if event["method"] == "turn/started":
                turn = params.get("turn", {}).get("id")
                if not isinstance(turn, str) or not turn:
                    return None
                turns.add(turn)
                continue
            item = params.get("item", {})
            if item.get("type") != "userMessage":
                continue
            turn, item_id, content = params.get("turnId"), item.get("id"), item.get("content")
            if (not isinstance(turn, str) or not turn or not isinstance(item_id, str) or not item_id
                    or not isinstance(content, list) or len(content) != 1
                    or not isinstance(content[0], dict) or content[0].get("type") != "text"
                    or not isinstance(content[0].get("text"), str)):
                return None
            digest = hashlib.sha256(content[0]["text"].encode()).hexdigest()
            if digest != payload.get("prompt_sha256"):
                return None
            inputs[(turn, item_id)] = digest
        if len(turns) != 1 or len(inputs) != 1:
            return None
        turn = next(iter(turns))
        if next(iter(inputs))[0] != turn:
            return None
        return {"threadId": session_id, "payload": payload, "receipt": {
            "ok": True, "committed": True, "session_id": session_id,
            "turn_id": turn, "start_offset": payload.get("start_offset")}}

    def turn_completion(self, session_id: str, turn_id: str):
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT event FROM workspace_events WHERE session_id=? "
                "AND json_extract(event, '$.method')='turn/completed' "
                "AND json_extract(event, '$.params.turn.id')=? ORDER BY sequence DESC LIMIT 1",
                (session_id, turn_id),
            ).fetchone()
        return None if row is None else json.loads(row[0])["params"]["turn"]

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

    def replay(self, session_id: str, *, after=0):
        """Stream a finite journal snapshot without per-page HTTP round trips."""
        if type(after) is not int or after < 0:
            raise ValueError("Invalid replay cursor")
        with closing(self._connect()) as conn:
            through = conn.execute("SELECT COALESCE(MAX(sequence), 0) FROM workspace_events WHERE session_id=?",
                                   (session_id,)).fetchone()[0]
            cursor = conn.execute("SELECT sequence,event FROM workspace_events WHERE session_id=? AND sequence>? AND sequence<=? ORDER BY sequence",
                                  (session_id, after, through))
            while rows := cursor.fetchmany(200):
                # Events are validated JSON on insertion; avoid decoding and
                # re-encoding large native history records just for transport.
                yield ('{"events":[' + ','.join('{"sequence":' + str(seq) + ',"event":' + raw + '}'
                       for seq, raw in rows) + ']}\n').encode('utf-8')

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
