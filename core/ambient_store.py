"""Capped local store for ambient activity metadata.

SQLite on disk plus an in-memory ring buffer of 2,000 events. Consecutive
identical records collapse ActivityWatch-style: the heartbeat only extends the
open event's duration, which is what keeps a full day inside a few thousand
rows. Retention (30 days, row cap, size ceiling) is swept on start and every
six hours. The file is 0600, local only, never synced, never backed up
off-machine.

This is a short-lived buffer, not a second memory system. Anything worth
keeping is written through the existing memory path.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.sqlite_connection import connect_database

SCHEMA_VERSION = 1
DEFAULT_AMBIENT_DB = Path.home() / ".local" / "state" / "serena" / "ambient.sqlite3"
RING_LIMIT = 2_000
RETENTION_DAYS = 30
MAX_DISK_ROWS = 100_000
MAX_DB_BYTES = 100 * 1024 * 1024
SIZE_TRIM_BATCH_ROWS = 5_000
SIZE_TRIM_KEEP_ROWS = 1_000
SWEEP_INTERVAL_SECONDS = 6 * 3_600

# `sleep` (PrepareForSleep tailing) and `agent_input` (raw agent key counts)
# are reserved taxonomy with no producer yet; everything else has a writer.
EVENT_KINDS = (
    "window",
    "tab",
    "idle",
    "active",
    "lock",
    "unlock",
    "sleep",
    "power",
    "clipboard",
    "agent_input",
    "vcs",
)


@dataclass(frozen=True, slots=True)
class AmbientEvent:
    event_id: str
    kind: str
    app: str = ""
    title: str = ""
    url: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    agent_driven: bool = False
    started_at: float = 0.0
    ended_at: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.ended_at - self.started_at)


def _clean(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


class AmbientStore:
    """Durable ring of what he was working on, metadata only."""

    def __init__(self, path: Path | None = None) -> None:
        configured = os.environ.get("SERENA_AMBIENT_DB_PATH", "").strip()
        self.path = Path(path or configured or DEFAULT_AMBIENT_DB).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        with suppress(OSError):
            self.path.chmod(0o600)
        self._buffer: deque[AmbientEvent] = deque(maxlen=RING_LIMIT)
        self._last_sweep = 0.0
        self._vacuum_wanted = False
        self.retention_sweep()
        self._last_sweep = time.time()

    # -- recording ------------------------------------------------------

    def record(
        self,
        kind: str,
        *,
        app: str = "",
        title: str = "",
        url: str = "",
        meta: dict[str, Any] | None = None,
        agent_driven: bool = False,
        now: float | None = None,
    ) -> AmbientEvent | None:
        """Record one heartbeat; identical consecutive beats only extend."""

        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown ambient event kind {kind!r}")
        moment = time.time() if now is None else float(now)
        if self.paused():
            return None
        clean_meta = {
            _clean(key, 64): _clean(value, 500)
            for key, value in (meta or {}).items()
        }
        candidate = AmbientEvent(
            event_id=str(uuid.uuid4()),
            kind=kind,
            app=_clean(app, 256),
            title=_clean(title, 500),
            url=_clean(url, 2_000),
            meta=clean_meta,
            agent_driven=bool(agent_driven),
            started_at=moment,
            ended_at=moment,
        )
        previous = self._buffer[-1] if self._buffer else None
        if previous is not None and self._same_beat(previous, candidate):
            merged = AmbientEvent(
                event_id=previous.event_id,
                kind=previous.kind,
                app=previous.app,
                title=previous.title,
                url=previous.url,
                meta=previous.meta,
                agent_driven=previous.agent_driven,
                started_at=previous.started_at,
                ended_at=moment,
            )
            self._buffer[-1] = merged
            with self._connect() as connection:
                connection.execute(
                    "UPDATE ambient_events SET ended_at = ? WHERE event_id = ?",
                    (moment, previous.event_id),
                )
            self._maybe_sweep(moment)
            return merged
        self._buffer.append(candidate)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO ambient_events("
                "event_id, kind, app, title, url, meta_json, agent_driven,"
                " started_at, ended_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate.event_id,
                    candidate.kind,
                    candidate.app,
                    candidate.title,
                    candidate.url,
                    json.dumps(candidate.meta, separators=(",", ":")),
                    int(candidate.agent_driven),
                    candidate.started_at,
                    candidate.ended_at,
                ),
            )
        self._maybe_sweep(moment)
        return candidate

    @staticmethod
    def _same_beat(first: AmbientEvent, second: AmbientEvent) -> bool:
        return (
            first.kind == second.kind
            and first.app == second.app
            and first.title == second.title
            and first.url == second.url
            and first.meta == second.meta
            and first.agent_driven == second.agent_driven
        )

    # -- reading --------------------------------------------------------

    def recent(
        self,
        *,
        now: float | None = None,
        limit: int = RING_LIMIT,
        seconds: float | None = None,
    ) -> list[AmbientEvent]:
        """Buffered events, chronological. Empty until the daemon records."""

        moment = time.time() if now is None else float(now)
        events = list(self._buffer)[-max(1, min(int(limit), RING_LIMIT)):]
        if seconds is not None:
            cutoff = moment - float(seconds)
            events = [event for event in events if event.ended_at >= cutoff]
        return events

    def recent_from_disk(
        self,
        *,
        now: float | None = None,
        limit: int = 500,
        seconds: float | None = 1_200.0,
    ) -> list[AmbientEvent]:
        """Trailing disk window, chronological. For context after a restart."""

        moment = time.time() if now is None else float(now)
        cutoff = moment - float(seconds) if seconds is not None else 0.0
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ambient_events WHERE ended_at >= ? "
                "ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (cutoff, max(1, min(int(limit), 5_000))),
            ).fetchall()
        # Newest-first for the LIMIT, chronological for the caller.
        return [_event_from_row(row) for row in reversed(rows)]

    def status(self, *, now: float | None = None) -> dict[str, Any]:
        moment = time.time() if now is None else float(now)
        with self._connect() as connection:
            stored = int(
                connection.execute("SELECT COUNT(*) FROM ambient_events").fetchone()[0]
            )
        events = list(self._buffer)
        return {
            "paused": self.paused(),
            "buffered": len(events),
            "stored": stored,
            "oldest": events[0].started_at if events else None,
            "newest": events[-1].ended_at if events else None,
            "db_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "agent_driven": self.is_agent_driven(now=moment),
        }

    # -- forgetting -----------------------------------------------------

    def forget(self, *, minutes: float, now: float | None = None) -> int:
        """Drop the trailing N minutes from memory and disk immediately."""

        moment = time.time() if now is None else float(now)
        cutoff = moment - max(0.0, float(minutes)) * 60.0
        kept = deque(
            (event for event in self._buffer if event.ended_at < cutoff),
            maxlen=RING_LIMIT,
        )
        removed_memory = len(self._buffer) - len(kept)
        self._buffer = kept
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM ambient_events WHERE ended_at >= ?", (cutoff,)
            )
        return max(removed_memory, cursor.rowcount)

    def forget_all(self, *, now: float | None = None) -> int:  # noqa: ARG002
        removed_memory = len(self._buffer)
        self._buffer.clear()
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM ambient_events")
        return max(removed_memory, cursor.rowcount)

    # -- retention ------------------------------------------------------

    def retention_sweep(self, *, now: float | None = None) -> int:
        moment = time.time() if now is None else float(now)
        cutoff = moment - RETENTION_DAYS * 86_400
        kept = deque(
            (event for event in self._buffer if event.ended_at >= cutoff),
            maxlen=RING_LIMIT,
        )
        removed_memory = len(self._buffer) - len(kept)
        self._buffer = kept
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM ambient_events WHERE ended_at < ?", (cutoff,)
            )
            # Memory mirrors disk; report distinct removals, not the sum.
            removed = max(removed_memory, max(0, cursor.rowcount))
            removed += self._enforce_disk_caps(connection)
            connection.execute(
                "UPDATE ambient_settings SET last_sweep = ?, updated_at = ?"
                " WHERE singleton = 1",
                (moment, moment),
            )
        if self._vacuum_wanted:
            # Outside the transaction: VACUUM cannot run inside one. Only an
            # oversized file ever sets the flag, so this stays rare.
            self._vacuum_wanted = False
            with suppress(Exception), self._connect() as vacuum:
                vacuum.execute("VACUUM")
        return removed

    def _enforce_disk_caps(self, connection: sqlite3.Connection) -> int:
        removed = 0
        total = int(
            connection.execute("SELECT COUNT(*) FROM ambient_events").fetchone()[0]
        )
        if total > MAX_DISK_ROWS:
            surplus = total - MAX_DISK_ROWS
            connection.execute(
                "DELETE FROM ambient_events WHERE event_id IN ("
                "SELECT event_id FROM ambient_events ORDER BY started_at, rowid LIMIT ?)",
                (surplus,),
            )
            removed += surplus
            total = MAX_DISK_ROWS
        try:
            oversized = self.path.stat().st_size > MAX_DB_BYTES
        except OSError:
            return removed
        if not oversized:
            return removed
        # One bounded trim per sweep, never a loop on the file size: deletes
        # do not shrink the file without a vacuum, so looping there wipes the
        # table while the size never moves. The newest floor always survives.
        trimmable = max(0, total - SIZE_TRIM_KEEP_ROWS)
        batch = min(SIZE_TRIM_BATCH_ROWS, trimmable)
        if batch <= 0:
            return removed
        connection.execute(
            "DELETE FROM ambient_events WHERE event_id IN ("
            "SELECT event_id FROM ambient_events ORDER BY started_at, rowid LIMIT ?)",
            (batch,),
        )
        self._vacuum_wanted = True
        return removed + batch

    def _maybe_sweep(self, moment: float) -> None:
        if moment - self._last_sweep >= SWEEP_INTERVAL_SECONDS:
            self.retention_sweep(now=moment)
            self._last_sweep = moment

    # -- pause + agent tagging ------------------------------------------

    def pause(self, *, now: float | None = None) -> None:
        moment = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute(
                "UPDATE ambient_settings SET paused = 1, updated_at = ?"
                " WHERE singleton = 1",
                (moment,),
            )

    def resume(self, *, now: float | None = None) -> None:
        moment = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute(
                "UPDATE ambient_settings SET paused = 0, updated_at = ?"
                " WHERE singleton = 1",
                (moment,),
            )

    def paused(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT paused FROM ambient_settings WHERE singleton = 1"
            ).fetchone()
        return bool(row and row["paused"])

    def mark_agent_active(self, *, until: float, now: float | None = None) -> None:
        """Tag a window where Serena herself drives input as agent-driven."""

        moment = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute(
                "UPDATE ambient_settings SET agent_driven_until = ?, updated_at = ?"
                " WHERE singleton = 1",
                (float(until), moment),
            )

    def is_agent_driven(self, *, now: float | None = None) -> bool:
        moment = time.time() if now is None else float(now)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT agent_driven_until FROM ambient_settings WHERE singleton = 1"
            ).fetchone()
        return bool(row and row["agent_driven_until"] and float(row["agent_driven_until"]) > moment)

    # -- internals ------------------------------------------------------

    def dump_for_test(self) -> str:
        """Every stored app/title/url. Tests use it to prove disk removal."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT app, title, url FROM ambient_events ORDER BY started_at, rowid"
            ).fetchall()
        return "\n".join(
            f"{row['app']}\n{row['title']}\n{row['url']}" for row in rows
        )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS ambient_events (
                    event_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    app TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL DEFAULT '',
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    agent_driven INTEGER NOT NULL DEFAULT 0,
                    started_at REAL NOT NULL,
                    ended_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ambient_events_time_idx
                ON ambient_events(started_at, ended_at);
                CREATE TABLE IF NOT EXISTS ambient_settings (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    paused INTEGER NOT NULL DEFAULT 0,
                    agent_driven_until REAL NOT NULL DEFAULT 0,
                    last_sweep REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO ambient_settings(singleton) VALUES (1);
                PRAGMA user_version=1;
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return connect_database(self.path, timeout=5)


def _event_from_row(row: sqlite3.Row) -> AmbientEvent:
    try:
        meta = json.loads(str(row["meta_json"]))
        if not isinstance(meta, dict):
            meta = {}
    except (json.JSONDecodeError, TypeError, ValueError):
        meta = {}
    return AmbientEvent(
        event_id=str(row["event_id"]),
        kind=str(row["kind"]),
        app=str(row["app"]),
        title=str(row["title"]),
        url=str(row["url"]),
        meta={str(key): value for key, value in meta.items()},
        agent_driven=bool(row["agent_driven"]),
        started_at=float(row["started_at"]),
        ended_at=float(row["ended_at"]),
    )
