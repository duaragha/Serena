"""Durable delivery of spoken work requests to Serena's coding worker."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

DEFAULT_VOICE_INBOX_PATH = (
    Path.home() / ".local" / "state" / "serena" / "voice_inbox.sqlite3"
)
DEFAULT_VOICE_WORK_MARKER_PATH = (
    Path.home() / ".config" / "serena" / "voice_working"
)
MAX_VOICE_REQUEST_CHARS = 4_000
CLAIM_TTL_SECONDS = 30.0
WORK_TTL_SECONDS = 24 * 60 * 60
RESIDENT_LEASE_TTL_SECONDS = 5.0
_RESIDENT_CLAIM_ERROR = "resident voice worker owns queue"


@dataclass(frozen=True, slots=True)
class VoiceInboxItem:
    item_id: str
    request: str
    call_id: str
    turn_id: str
    state: str
    created_at: float
    target_sid: str = ""

    @property
    def prompt(self) -> str:
        return (
            "Raghav said this aloud to me just now. Treat it exactly as a message "
            "he typed in this chat. Continue in the current project and act on it "
            "now, following the normal safety and wait-for-go rules. Do not ask him "
            "to repeat it.\n\nSpoken request:\n"
            + self.request
        )


class VoiceInboxStore:
    """Small SQLite outbox shared by the voice host and Serena desktop app."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        work_marker_path: str | Path | None = None,
    ) -> None:
        configured = os.environ.get("SERENA_VOICE_INBOX_PATH")
        self.path = Path(path or configured or DEFAULT_VOICE_INBOX_PATH).expanduser()
        configured_marker = os.environ.get("SERENA_VOICE_WORK_MARKER_PATH")
        if work_marker_path is not None:
            marker = work_marker_path
        elif path is not None:
            marker = self.path.with_name("voice_working")
        else:
            marker = configured_marker or DEFAULT_VOICE_WORK_MARKER_PATH
        self.work_marker_path = Path(marker).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._schema_lock = threading.Lock()
        self._schema_ready = False
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with self._connect() as connection:
                connection.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE IF NOT EXISTS voice_inbox (
                        item_id TEXT PRIMARY KEY,
                        request TEXT NOT NULL,
                        call_id TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        state TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        claimed_at REAL,
                        delivered_at REAL,
                        target_sid TEXT NOT NULL DEFAULT '',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT NOT NULL DEFAULT '',
                        UNIQUE(call_id, turn_id)
                    );
                    CREATE INDEX IF NOT EXISTS voice_inbox_state_created
                    ON voice_inbox(state, created_at);

                    CREATE TABLE IF NOT EXISTS voice_work (
                        item_id TEXT PRIMARY KEY,
                        target_sid TEXT NOT NULL,
                        cwd TEXT NOT NULL DEFAULT '',
                        session_id TEXT NOT NULL DEFAULT '',
                        state TEXT NOT NULL,
                        started_at REAL NOT NULL,
                        finished_at REAL,
                        summary TEXT NOT NULL DEFAULT '',
                        last_error TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS voice_work_state_target
                    ON voice_work(state, target_sid);

                    CREATE TABLE IF NOT EXISTS voice_worker_lease (
                        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                        owner_id TEXT NOT NULL,
                        pid INTEGER NOT NULL,
                        heartbeat REAL NOT NULL
                    );

                    CREATE TRIGGER IF NOT EXISTS voice_inbox_resident_owner
                    BEFORE UPDATE OF state, target_sid ON voice_inbox
                    WHEN NEW.state = 'claimed'
                         AND NEW.target_sid NOT LIKE 'headless-voice-%'
                         AND EXISTS (
                             SELECT 1 FROM voice_worker_lease
                             WHERE singleton = 1
                               AND heartbeat >= (
                                   CAST(strftime('%s', 'now') AS REAL) - 5.0
                               )
                         )
                    BEGIN
                        SELECT RAISE(ABORT, 'resident voice worker owns queue');
                    END;
                    """
                )
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(voice_work)")
                }
                if "session_id" not in columns:
                    connection.execute(
                        "ALTER TABLE voice_work ADD COLUMN session_id TEXT "
                        "NOT NULL DEFAULT ''"
                    )
                if "summary" not in columns:
                    connection.execute(
                        "ALTER TABLE voice_work ADD COLUMN summary TEXT "
                        "NOT NULL DEFAULT ''"
                    )
            with suppress(OSError):
                self.path.chmod(0o600)
            self._schema_ready = True
        self._expire_stale_work()
        self._sync_work_marker()

    def enqueue(self, request: str, *, call_id: str, turn_id: str) -> VoiceInboxItem:
        clean = " ".join(str(request).strip().split())
        if not clean:
            raise ValueError("spoken work request cannot be empty")
        if len(clean) > MAX_VOICE_REQUEST_CHARS:
            raise ValueError("spoken work request is too long")
        item_id = str(uuid.uuid4())
        created_at = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO voice_inbox(
                    item_id, request, call_id, turn_id, state, created_at
                ) VALUES (?, ?, ?, ?, 'queued', ?)
                """,
                (item_id, clean, call_id, turn_id, created_at),
            )
            row = connection.execute(
                "SELECT * FROM voice_inbox WHERE call_id=? AND turn_id=?",
                (call_id, turn_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("spoken work request was not persisted")
        return self._item(row)

    def claim_next(
        self,
        target_sid: str,
        *,
        claim_ttl: float = CLAIM_TTL_SECONDS,
    ) -> VoiceInboxItem | None:
        target_sid = str(target_sid).strip()
        if not target_sid:
            raise ValueError("target session id is required")
        now = time.time()
        cutoff = now - max(1.0, claim_ttl)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE voice_inbox
                SET state='queued', claimed_at=NULL, target_sid='',
                    last_error='delivery claim expired'
                WHERE state='claimed' AND claimed_at < ?
                """,
                (cutoff,),
            )
            row = connection.execute(
                """
                SELECT * FROM voice_inbox
                WHERE state='queued'
                ORDER BY created_at, item_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE voice_inbox
                SET state='claimed', claimed_at=?, target_sid=?, attempts=attempts+1
                WHERE item_id=? AND state='queued'
                """,
                (now, target_sid, row["item_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM voice_inbox WHERE item_id=?",
                (row["item_id"],),
            ).fetchone()
            connection.commit()
        except sqlite3.DatabaseError as error:
            connection.rollback()
            if _RESIDENT_CLAIM_ERROR in str(error):
                return None
            raise
        finally:
            connection.close()
        return self._item(claimed) if claimed is not None else None

    def renew_resident_lease(
        self,
        owner_id: str,
        *,
        pid: int,
        heartbeat: float | None = None,
    ) -> None:
        owner_id = str(owner_id).strip()
        if not owner_id:
            raise ValueError("resident worker owner id is required")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO voice_worker_lease(singleton, owner_id, pid, heartbeat)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    owner_id=excluded.owner_id,
                    pid=excluded.pid,
                    heartbeat=excluded.heartbeat
                """,
                (owner_id, int(pid), time.time() if heartbeat is None else heartbeat),
            )

    def clear_resident_lease(self, owner_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM voice_worker_lease WHERE singleton=1 AND owner_id=?",
                (str(owner_id).strip(),),
            )
        return cursor.rowcount == 1

    def resident_lease_active(self, *, now: float | None = None) -> bool:
        cutoff = (time.time() if now is None else now) - RESIDENT_LEASE_TTL_SECONDS
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM voice_worker_lease
                WHERE singleton=1 AND heartbeat >= ?
                """,
                (cutoff,),
            ).fetchone()
        return row is not None

    def acknowledge(self, item_id: str, *, target_sid: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_inbox
                SET state='delivered', delivered_at=?, last_error=''
                WHERE item_id=? AND state='claimed' AND target_sid=?
                """,
                (time.time(), item_id, target_sid),
            )
        return cursor.rowcount == 1

    def acknowledge_started(
        self,
        item_id: str,
        *,
        target_sid: str,
        cwd: str = "",
    ) -> bool:
        """Atomically mark pane delivery and register its running work turn."""

        target_sid = str(target_sid).strip()
        if not target_sid:
            return False
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE voice_inbox
                SET state='delivered', delivered_at=?, last_error=''
                WHERE item_id=? AND state='claimed' AND target_sid=?
                """,
                (now, item_id, target_sid),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            connection.execute(
                """
                INSERT INTO voice_work(
                    item_id, target_sid, cwd, session_id, state, started_at,
                    finished_at, summary, last_error
                ) VALUES (?, ?, ?, '', 'working', ?, NULL, '', '')
                ON CONFLICT(item_id) DO UPDATE SET
                    target_sid=excluded.target_sid,
                    cwd=excluded.cwd,
                    session_id='',
                    state='working',
                    started_at=excluded.started_at,
                    finished_at=NULL,
                    summary='',
                    last_error=''
                """,
                (item_id, target_sid, str(cwd).strip(), now),
            )
            connection.commit()
        finally:
            connection.close()
        self._sync_work_marker()
        return True

    def set_work_session(self, item_id: str, session_id: str) -> bool:
        session_id = str(session_id).strip()
        if not session_id:
            return False
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_work SET session_id=?
                WHERE item_id=? AND state='working'
                """,
                (session_id, item_id),
            )
        return cursor.rowcount == 1

    def finish_work_item(
        self,
        item_id: str,
        *,
        error: str = "",
        summary: str = "",
    ) -> bool:
        state = "failed" if error else "completed"
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_work
                SET state=?, finished_at=?, summary=?, last_error=?
                WHERE item_id=? AND state='working'
                """,
                (
                    state,
                    time.time(),
                    str(summary).strip()[:2_000],
                    str(error).strip()[:500],
                    item_id,
                ),
            )
        if cursor.rowcount:
            self._sync_work_marker()
        return cursor.rowcount == 1

    def recover_headless_work(self) -> int:
        """Requeue work interrupted with the resident headless supervisor."""

        now = time.time()
        item_ids: list[str] = []
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT item_id FROM voice_work
                WHERE state='working' AND target_sid LIKE 'headless-voice-%'
                """
            ).fetchall()
            item_ids = [str(row["item_id"]) for row in rows]
            for item_id in item_ids:
                connection.execute(
                    """
                    UPDATE voice_work
                    SET state='failed', finished_at=?,
                        last_error='resident worker restarted'
                    WHERE item_id=? AND state='working'
                    """,
                    (now, item_id),
                )
                connection.execute(
                    """
                    UPDATE voice_inbox
                    SET state='queued', claimed_at=NULL, delivered_at=NULL,
                        target_sid='', last_error='resident worker restarted'
                    WHERE item_id=?
                    """,
                    (item_id,),
                )
            connection.commit()
        finally:
            connection.close()
        if item_ids:
            self._sync_work_marker()
        return len(item_ids)

    def requeue_work_item(self, item_id: str, *, error: str) -> bool:
        """Return one interrupted resident job to the durable queue."""

        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE voice_work
                SET state='failed', finished_at=?, last_error=?
                WHERE item_id=? AND state='working'
                """,
                (now, str(error).strip()[:500], item_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            connection.execute(
                """
                UPDATE voice_inbox
                SET state='queued', claimed_at=NULL, delivered_at=NULL,
                    target_sid='', last_error=?
                WHERE item_id=?
                """,
                (str(error).strip()[:500], item_id),
            )
            connection.commit()
        finally:
            connection.close()
        self._sync_work_marker()
        return True

    def migrate_work_target(self, old_sid: str, new_sid: str) -> int:
        old_sid = str(old_sid).strip()
        new_sid = str(new_sid).strip()
        if not old_sid or not new_sid or old_sid == new_sid:
            return 0
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_work SET target_sid=?
                WHERE target_sid=? AND state='working'
                """,
                (new_sid, old_sid),
            )
        return cursor.rowcount

    def finish_work_target(self, target_sid: str, *, error: str = "") -> int:
        target_sid = str(target_sid).strip()
        if not target_sid:
            return 0
        state = "failed" if error else "completed"
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_work
                SET state=?, finished_at=?, last_error=?
                WHERE target_sid=? AND state='working'
                """,
                (state, time.time(), str(error)[:500], target_sid),
            )
        if cursor.rowcount:
            self._sync_work_marker()
        return cursor.rowcount

    def working_count(self) -> int:
        self._expire_stale_work()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM voice_work WHERE state='working'"
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def _expire_stale_work(self) -> int:
        cutoff = time.time() - WORK_TTL_SECONDS
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_work
                SET state='failed', finished_at=?, last_error='working lease expired'
                WHERE state='working' AND started_at < ?
                """,
                (time.time(), cutoff),
            )
        return cursor.rowcount

    def _sync_work_marker(self) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM voice_work WHERE state='working'"
            ).fetchone()
        count = int(row["count"] if row is not None else 0)
        if count <= 0:
            with suppress(FileNotFoundError):
                self.work_marker_path.unlink()
            return
        self.work_marker_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.work_marker_path.with_name(
            f".{self.work_marker_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps({"state": "working", "count": count}) + "\n",
                encoding="utf-8",
            )
            with suppress(OSError):
                temporary.chmod(0o600)
            os.replace(temporary, self.work_marker_path)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

    def release(self, item_id: str, *, target_sid: str, error: str = "") -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_inbox
                SET state='queued', claimed_at=NULL, target_sid='', last_error=?
                WHERE item_id=? AND state='claimed' AND target_sid=?
                """,
                (str(error)[:500], item_id, target_sid),
            )
        return cursor.rowcount == 1

    def pending_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM voice_inbox WHERE state != 'delivered'"
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    @staticmethod
    def _item(row: sqlite3.Row) -> VoiceInboxItem:
        return VoiceInboxItem(
            item_id=str(row["item_id"]),
            request=str(row["request"]),
            call_id=str(row["call_id"]),
            turn_id=str(row["turn_id"]),
            state=str(row["state"]),
            created_at=float(row["created_at"]),
            target_sid=str(row["target_sid"] or ""),
        )

_DEFAULT_STORE: VoiceInboxStore | None = None
_DEFAULT_LOCK = threading.Lock()


def get_default_voice_inbox() -> VoiceInboxStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is not None:
        return _DEFAULT_STORE
    with _DEFAULT_LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = VoiceInboxStore()
    return _DEFAULT_STORE
