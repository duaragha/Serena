"""Opt-in supportive context and private local reflection.

This is not therapy software. It does not diagnose, score mental health, or
contact anyone. Reflections stay in a user-owned SQLite file, pattern summaries
are deterministic counts with source receipts, and every proactive check-in is
disabled until Raghav opts in.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_SUPPORT_DB = Path.home() / ".local/state/serena/supportive-mode.sqlite3"
MAX_REFLECTION_CHARS = 20_000
MAX_RELATIONSHIP_CONTEXT_CHARS = 4_000
MAX_TAGS = 20
ALLOWED_MOODS = frozenset({"", "low", "steady", "good", "overwhelmed", "tired", "energized"})

_CRISIS = re.compile(
    r"\b(?:kill myself|end my life|suicide|suicidal|hurt myself|self[- ]harm|"
    r"do not want to live|don't want to live|cannot stay safe|can't stay safe)\b",
    re.IGNORECASE,
)
_MEDICAL = re.compile(
    r"\b(?:diagnos(?:e|is)|prescri(?:be|ption)|medication dosage|symptom means|"
    r"am i (?:depressed|bipolar|autistic)|mental disorder)\b",
    re.IGNORECASE,
)


class SupportiveModeError(RuntimeError):
    pass


class SupportiveModeDisabled(SupportiveModeError):
    pass


@dataclass(frozen=True, slots=True)
class SupportSettings:
    enabled: bool = False
    allow_checkins: bool = False
    allow_pattern_insights: bool = False
    retention_days: int = 90
    checkin_interval_hours: int = 24
    relationship_context: str = ""
    last_checkin_at: float | None = None
    updated_at: float = 0.0


@dataclass(frozen=True, slots=True)
class Reflection:
    entry_id: str
    body: str
    source: str
    mood: str
    tags: tuple[str, ...]
    created_at: float
    retention_until: float


@dataclass(frozen=True, slots=True)
class PatternInsight:
    kind: str
    value: str
    count: int
    entry_ids: tuple[str, ...]
    source_sha256: tuple[str, ...]
    explanation: str


@dataclass(frozen=True, slots=True)
class SupportBoundary:
    level: str
    may_continue_supportively: bool
    guidance: str
    external_action_taken: bool = False


class SupportiveModeStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or DEFAULT_SUPPORT_DB).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        with suppress(OSError):
            self.path.chmod(0o600)

    def settings(self) -> SupportSettings:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM support_settings WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return SupportSettings()
        return SupportSettings(
            enabled=bool(row["enabled"]),
            allow_checkins=bool(row["allow_checkins"]),
            allow_pattern_insights=bool(row["allow_pattern_insights"]),
            retention_days=int(row["retention_days"]),
            checkin_interval_hours=int(row["checkin_interval_hours"]),
            relationship_context=str(row["relationship_context"]),
            last_checkin_at=(
                float(row["last_checkin_at"]) if row["last_checkin_at"] is not None else None
            ),
            updated_at=float(row["updated_at"]),
        )

    def configure(
        self,
        *,
        enabled: bool,
        allow_checkins: bool = False,
        allow_pattern_insights: bool = False,
        retention_days: int = 90,
        checkin_interval_hours: int = 24,
        relationship_context: str = "",
        now: float | None = None,
    ) -> SupportSettings:
        if not 1 <= int(retention_days) <= 3_650:
            raise ValueError("retention_days must be between 1 and 3650")
        if not 1 <= int(checkin_interval_hours) <= 24 * 30:
            raise ValueError("checkin_interval_hours must be between 1 and 720")
        context = " ".join(str(relationship_context or "").split())
        if len(context) > MAX_RELATIONSHIP_CONTEXT_CHARS:
            raise ValueError("relationship context is too long")
        moment = time.time() if now is None else float(now)
        retention_seconds = int(retention_days) * 86_400
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO support_settings("
                "singleton, enabled, allow_checkins, allow_pattern_insights, retention_days, "
                "checkin_interval_hours, relationship_context, last_checkin_at, updated_at"
                ") VALUES (1, ?, ?, ?, ?, ?, ?, NULL, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET enabled=excluded.enabled, "
                "allow_checkins=excluded.allow_checkins, "
                "allow_pattern_insights=excluded.allow_pattern_insights, "
                "retention_days=excluded.retention_days, "
                "checkin_interval_hours=excluded.checkin_interval_hours, "
                "relationship_context=excluded.relationship_context, updated_at=excluded.updated_at",
                (
                    int(bool(enabled)),
                    int(bool(allow_checkins and enabled)),
                    int(bool(allow_pattern_insights and enabled)),
                    int(retention_days),
                    int(checkin_interval_hours),
                    context,
                    moment,
                ),
            )
            shortened = connection.execute(
                "SELECT entry_id FROM support_reflections "
                "WHERE retention_until > created_at + ?",
                (retention_seconds,),
            ).fetchall()
            connection.execute(
                "UPDATE support_reflections "
                "SET retention_until = created_at + ? "
                "WHERE retention_until > created_at + ?",
                (retention_seconds, retention_seconds),
            )
            for row in shortened:
                self._event(
                    connection,
                    "reflection.retention_shortened",
                    str(row["entry_id"]),
                    moment,
                )
            expired = connection.execute(
                "SELECT entry_id FROM support_reflections WHERE retention_until <= ?",
                (moment,),
            ).fetchall()
            connection.execute(
                "DELETE FROM support_reflections WHERE retention_until <= ?", (moment,)
            )
            for row in expired:
                self._event(connection, "reflection.expired", str(row["entry_id"]), moment)
            self._event(connection, "mode.enabled" if enabled else "mode.disabled", "", moment)
        return self.settings()

    def disable(self, *, now: float | None = None) -> SupportSettings:
        current = self.settings()
        return self.configure(
            enabled=False,
            allow_checkins=False,
            allow_pattern_insights=False,
            retention_days=current.retention_days,
            checkin_interval_hours=current.checkin_interval_hours,
            relationship_context=current.relationship_context,
            now=now,
        )

    def add_reflection(
        self,
        body: str,
        *,
        source: str,
        mood: str = "",
        tags: Sequence[str] = (),
        now: float | None = None,
        retention_days: int | None = None,
    ) -> Reflection:
        settings = self.settings()
        if not settings.enabled:
            raise SupportiveModeDisabled("supportive mode is off")
        text = str(body or "").strip()
        if not text or len(text) > MAX_REFLECTION_CHARS:
            raise ValueError("reflection must be between 1 and 20000 characters")
        origin = str(source or "").strip()
        if not origin or len(origin) > 500:
            raise ValueError("reflection source must be between 1 and 500 characters")
        clean_mood = str(mood or "").strip().lower()
        if clean_mood not in ALLOWED_MOODS:
            raise ValueError("unsupported reflection mood")
        clean_tags = _tags(tags)
        days = settings.retention_days if retention_days is None else int(retention_days)
        if not 1 <= days <= settings.retention_days:
            raise ValueError("entry retention cannot exceed the configured retention policy")
        moment = time.time() if now is None else float(now)
        entry = Reflection(
            entry_id=str(uuid.uuid4()),
            body=text,
            source=origin,
            mood=clean_mood,
            tags=clean_tags,
            created_at=moment,
            retention_until=moment + days * 86_400,
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO support_reflections("
                "entry_id, body, body_sha256, source, mood, tags_json, created_at, retention_until"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.entry_id,
                    entry.body,
                    _sha256(entry.body),
                    entry.source,
                    entry.mood,
                    json.dumps(list(entry.tags), separators=(",", ":")),
                    entry.created_at,
                    entry.retention_until,
                ),
            )
            self._event(connection, "reflection.added", entry.entry_id, moment)
        return entry

    def reflections(self, *, now: float | None = None, limit: int = 500) -> list[Reflection]:
        moment = time.time() if now is None else float(now)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT entry_id, body, source, mood, tags_json, created_at, retention_until "
                "FROM support_reflections WHERE retention_until > ? "
                "ORDER BY created_at DESC, entry_id DESC LIMIT ?",
                (moment, max(1, min(int(limit), 1_000))),
            ).fetchall()
        return [_reflection_from_row(row) for row in rows]

    def patterns(self, *, now: float | None = None, minimum_count: int = 2) -> list[PatternInsight]:
        settings = self.settings()
        if not settings.enabled or not settings.allow_pattern_insights:
            return []
        entries = self.reflections(now=now)
        groups: dict[tuple[str, str], list[Reflection]] = {}
        for entry in entries:
            if entry.mood:
                groups.setdefault(("mood", entry.mood), []).append(entry)
            for tag in entry.tags:
                groups.setdefault(("tag", tag), []).append(entry)
        insights = []
        for (kind, value), sources in sorted(groups.items()):
            if len(sources) < max(2, int(minimum_count)):
                continue
            insights.append(
                PatternInsight(
                    kind=kind,
                    value=value,
                    count=len(sources),
                    entry_ids=tuple(item.entry_id for item in sources),
                    source_sha256=tuple(_sha256(item.body) for item in sources),
                    explanation=(
                        f"{kind} {value!r} was explicitly attached to {len(sources)} "
                        "retained reflections; this is a count, not a diagnosis"
                    ),
                )
            )
        return insights

    def context_for_provider(self, *, now: float | None = None) -> str:
        """One provider-neutral block. Off means no relationship injection."""

        settings = self.settings()
        if not settings.enabled:
            return ""
        lines = [
            "# Opt-in supportive mode",
            "You are not a therapist. Be supportive without diagnosing or prescribing.",
            "Never initiate external contact or emergency action from supportive mode.",
        ]
        if settings.relationship_context:
            lines.append("Approved relationship context: " + settings.relationship_context)
        for insight in self.patterns(now=now)[:8]:
            lines.append("Observed pattern: " + insight.explanation)
        return "\n".join(lines)

    def checkin_due(self, *, now: float | None = None) -> bool:
        settings = self.settings()
        if not settings.enabled or not settings.allow_checkins:
            return False
        moment = time.time() if now is None else float(now)
        if settings.last_checkin_at is None:
            return True
        return moment - settings.last_checkin_at >= settings.checkin_interval_hours * 3_600

    def record_checkin(self, *, now: float | None = None) -> None:
        settings = self.settings()
        if not settings.enabled or not settings.allow_checkins:
            raise SupportiveModeDisabled("consensual check-ins are off")
        moment = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute(
                "UPDATE support_settings SET last_checkin_at=?, updated_at=? WHERE singleton=1",
                (moment, moment),
            )
            self._event(connection, "checkin.recorded", "", moment)

    def forget_reflection(self, entry_id: str, *, now: float | None = None) -> bool:
        moment = time.time() if now is None else float(now)
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM support_reflections WHERE entry_id = ?", (str(entry_id),)
            )
            if cursor.rowcount:
                self._event(connection, "reflection.deleted", str(entry_id), moment)
        return bool(cursor.rowcount)

    def purge_expired(self, *, now: float | None = None) -> int:
        moment = time.time() if now is None else float(now)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT entry_id FROM support_reflections WHERE retention_until <= ?", (moment,)
            ).fetchall()
            connection.execute(
                "DELETE FROM support_reflections WHERE retention_until <= ?", (moment,)
            )
            for row in rows:
                self._event(connection, "reflection.expired", str(row["entry_id"]), moment)
        return len(rows)

    def delete_all_reflections(self, *, confirmed: bool, now: float | None = None) -> int:
        if not confirmed:
            raise ValueError("deleting every reflection requires explicit confirmation")
        moment = time.time() if now is None else float(now)
        with self._connect() as connection:
            count = int(
                connection.execute("SELECT COUNT(*) FROM support_reflections").fetchone()[0]
            )
            connection.execute("DELETE FROM support_reflections")
            self._event(connection, "reflections.deleted_all", "", moment, detail=str(count))
        return count

    def audit_events(self) -> list[dict[str, Any]]:
        """Inspectable lifecycle evidence that never stores reflection text."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_id, event_type, entry_id, detail, created_at "
                "FROM support_events ORDER BY created_at, event_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def _event(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        entry_id: str,
        created_at: float,
        *,
        detail: str = "",
    ) -> None:
        connection.execute(
            "INSERT INTO support_events(event_id, event_type, entry_id, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), event_type, entry_id, str(detail)[:500], created_at),
        )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS support_settings (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    allow_checkins INTEGER NOT NULL DEFAULT 0,
                    allow_pattern_insights INTEGER NOT NULL DEFAULT 0,
                    retention_days INTEGER NOT NULL DEFAULT 90,
                    checkin_interval_hours INTEGER NOT NULL DEFAULT 24,
                    relationship_context TEXT NOT NULL DEFAULT '',
                    last_checkin_at REAL,
                    updated_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS support_reflections (
                    entry_id TEXT PRIMARY KEY,
                    body TEXT NOT NULL,
                    body_sha256 TEXT NOT NULL,
                    source TEXT NOT NULL,
                    mood TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    retention_until REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS support_reflections_retention_idx
                ON support_reflections(retention_until);
                CREATE TABLE IF NOT EXISTS support_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    entry_id TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                PRAGMA user_version=1;
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection


def support_boundary(text: str) -> SupportBoundary:
    """Fixed safety wording, separate from model judgment."""

    value = str(text or "")
    if _CRISIS.search(value):
        return SupportBoundary(
            level="crisis",
            may_continue_supportively=True,
            guidance=(
                "Stay present and encourage immediate human help. If there is immediate danger, "
                "tell Raghav to call local emergency services or go to the nearest emergency "
                "department, and to contact a trusted person who can stay with him. Do not diagnose, "
                "promise confidentiality, or claim any external contact was made."
            ),
        )
    if _MEDICAL.search(value):
        return SupportBoundary(
            level="medical",
            may_continue_supportively=True,
            guidance=(
                "Do not diagnose, prescribe, or change medication. Help organize what he wants to "
                "tell a licensed clinician and encourage professional medical advice."
            ),
        )
    return SupportBoundary(
        level="supportive",
        may_continue_supportively=True,
        guidance="Listen, reflect, and coach within the user's opt-in boundaries without diagnosis.",
    )


def _tags(values: Sequence[str]) -> tuple[str, ...]:
    tags = []
    for value in values:
        clean = " ".join(str(value or "").strip().lower().split())
        if clean and clean not in tags:
            if len(clean) > 64:
                raise ValueError("reflection tag is too long")
            tags.append(clean)
    if len(tags) > MAX_TAGS:
        raise ValueError("a reflection may have at most 20 tags")
    return tuple(tags)


def _reflection_from_row(row: sqlite3.Row) -> Reflection:
    try:
        decoded_tags = json.loads(str(row["tags_json"]))
        tags = _tags(decoded_tags) if isinstance(decoded_tags, list) else ()
    except (json.JSONDecodeError, TypeError, ValueError):
        # A damaged metadata field must not make every other private reflection
        # unreadable. The body remains available and the malformed tags fail empty.
        tags = ()
    return Reflection(
        entry_id=str(row["entry_id"]),
        body=str(row["body"]),
        source=str(row["source"]),
        mood=str(row["mood"]),
        tags=tags,
        created_at=float(row["created_at"]),
        retention_until=float(row["retention_until"]),
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
