"""Where commitments come from, when the answer has to be local and free.

Three inputs, all of them already on the machine: the Markdown tasks Serena
already keeps, plain iCalendar files on disk, and a local reminders database if
one exists. No hosted calendar, no OAuth, no paid API, no network call. An
adapter that cannot find its input says so and returns nothing; it never guesses
and it never raises into the ingest loop, because one missing calendar file must
not take down the other two sources.

Ingestion is idempotent by construction. Every candidate carries the source that
produced it and a stable reference inside that source, and the store treats that
pair as the identity of the commitment. Re-reading the same calendar ten times
produces one commitment, and a changed due date corrects the existing one rather
than growing a second copy of the same obligation.

Everything arrives as `proposed`. A calendar entry is evidence that something is
happening, not evidence that Raghav agreed Serena should manage it.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.commitments import (
    CommitmentError,
    CommitmentStore,
    Recurrence,
)

# One calendar directory should not be able to produce a thousand commitments in
# a single sweep. Bounded input is the difference between an adapter and a leak.
MAX_CANDIDATES_PER_ADAPTER = 200
MAX_ICS_BYTES = 4 * 1024 * 1024
# Something that ended a week ago is history, not a commitment.
DEFAULT_PAST_HORIZON_SECONDS = 7 * 86_400
DEFAULT_FUTURE_HORIZON_SECONDS = 90 * 86_400


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """Whether an adapter can run right now, and the honest reason if not."""

    available: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CommitmentCandidate:
    """One thing an adapter believes is owed, before the store accepts it."""

    title: str
    source: str
    source_ref: str
    detail: str = ""
    owner: str = "raghav"
    priority: str = "normal"
    due_at: float | None = None
    recurrence: Recurrence | None = None
    subject_entity_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["recurrence"] = self.recurrence.to_dict() if self.recurrence else None
        return value


class CommitmentSource(Protocol):
    """The whole adapter contract. Three methods, no lifecycle, no state."""

    name: str

    def status(self) -> SourceStatus: ...

    def fetch(self, *, now: float) -> list[CommitmentCandidate]: ...


# ---- memory tasks ---------------------------------------------------------


class MemoryTaskAdapter:
    """Raghav's existing Markdown tasks, read as commitments.

    This deliberately does not migrate or rewrite them. `chats memory` stays the
    place tasks are edited, and this mirrors them forward so a briefing can see
    them next to calendar events. Rewriting his memory files to fit a new schema
    would be the kind of "improvement" that loses data.
    """

    name = "memory_task"

    def __init__(self, lister: Callable[[str], Sequence[dict]] | None = None) -> None:
        self._lister = lister

    def _list(self) -> Sequence[dict]:
        if self._lister is not None:
            return self._lister("task")
        from memory.store import list_memories

        return list_memories("task")

    def status(self) -> SourceStatus:
        try:
            self._list()
        except Exception as error:
            return SourceStatus(False, f"memory tasks unreadable: {_short(error)}")
        return SourceStatus(True, "reading Markdown tasks")

    def fetch(self, *, now: float) -> list[CommitmentCandidate]:
        try:
            rows = self._list()
        except Exception:
            return []
        candidates: list[CommitmentCandidate] = []
        for row in list(rows)[:MAX_CANDIDATES_PER_ADAPTER]:
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            identifier = str(row.get("id") or "").strip()
            if not identifier:
                continue
            candidates.append(
                CommitmentCandidate(
                    title=content.splitlines()[0],
                    source=self.name,
                    source_ref=identifier,
                    detail=content,
                    owner="raghav",
                    # His deliberate todo list is what he chose to care about, so
                    # it outranks something Serena scraped out of a calendar.
                    priority="high",
                )
            )
        return candidates


# ---- local iCalendar files ------------------------------------------------

# A parameter value may be quoted, and a quoted one may contain a colon:
# `DTSTART;TZID="(UTC-05:00) Eastern Time":20260810T090000` is what Outlook
# writes. Treating the first colon as the separator chopped that line in half
# and threw the whole event away, so quoted runs are matched as a unit and only
# a bare colon ends the parameter list.
_ICS_LINE = re.compile(
    r'^(?P<name>[A-Za-z0-9-]+)(?P<params>;(?:"[^"]*"|[^:"])*)?:(?P<value>.*)$'
)
_FREQ_MAP = {
    "DAILY": "daily",
    "WEEKLY": "weekly",
    "MONTHLY": "monthly",
    "YEARLY": "yearly",
}


class LocalIcsAdapter:
    """Plain `.ics` files in a directory on this machine.

    Every calendar app worth using can export or sync to a local ics file, and a
    file is free, offline, and inspectable. That is the whole reason this is the
    calendar integration rather than a hosted API: an outage of someone else's
    service cannot take Raghav's schedule away from him.

    The parser is intentionally small. It reads SUMMARY, UID, DTSTART, and a
    simple RRULE, and it ignores everything it does not understand rather than
    failing the file. A calendar entry Serena half-understands is still better
    than a crash, as long as she does not pretend to know more than she read.
    """

    name = "local_ics"

    def __init__(self, directory: str | Path | None = None) -> None:
        configured = directory or os.environ.get("SERENA_CALENDAR_ICS_DIR", "").strip()
        self.directory = Path(configured).expanduser() if configured else None

    def status(self) -> SourceStatus:
        if self.directory is None:
            return SourceStatus(
                False,
                "no local calendar directory configured (set SERENA_CALENDAR_ICS_DIR)",
            )
        if not self.directory.is_dir():
            return SourceStatus(
                False, f"local calendar directory does not exist: {self.directory}"
            )
        return SourceStatus(True, f"reading ics files in {self.directory}")

    def _files(self) -> list[Path]:
        if self.directory is None or not self.directory.is_dir():
            return []
        with suppress(OSError):
            return sorted(
                item
                for item in self.directory.iterdir()
                if item.is_file() and item.suffix.lower() == ".ics"
            )
        return []

    def fetch(self, *, now: float) -> list[CommitmentCandidate]:
        candidates: list[CommitmentCandidate] = []
        floor = now - DEFAULT_PAST_HORIZON_SECONDS
        ceiling = now + DEFAULT_FUTURE_HORIZON_SECONDS
        for path in self._files():
            try:
                if path.stat().st_size > MAX_ICS_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for event in _parse_ics_events(text):
                due = event.get("due_at")
                recurrence = event.get("recurrence")
                # A repeating event is in scope even when its first instance is
                # long past, because the series itself is still live.
                if (
                    due is not None
                    and recurrence is None
                    and not floor <= float(due) <= ceiling
                ):
                    continue
                title = str(event.get("title") or "").strip()
                if not title:
                    continue
                candidates.append(
                    CommitmentCandidate(
                        title=title,
                        source=self.name,
                        source_ref=f"{path.name}:{event.get('uid') or title}",
                        detail=str(event.get("detail") or ""),
                        owner="raghav",
                        priority="normal",
                        due_at=due,
                        recurrence=recurrence,
                    )
                )
                if len(candidates) >= MAX_CANDIDATES_PER_ADAPTER:
                    return candidates
        return candidates


def _unfold(text: str) -> list[str]:
    """iCalendar folds long lines by continuing them with a leading space."""

    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in {" ", "\t"} and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_ics_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in _unfold(text):
        stripped = line.strip()
        if stripped == "BEGIN:VEVENT":
            current = {}
            continue
        if stripped == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None:
            continue
        match = _ICS_LINE.match(stripped)
        if match is None:
            continue
        name = match.group("name").upper()
        params = match.group("params") or ""
        value = match.group("value").strip()
        if name == "SUMMARY":
            current["title"] = _unescape_ics(value)
        elif name == "DESCRIPTION":
            current["detail"] = _unescape_ics(value)
        elif name == "UID":
            current["uid"] = value
        elif name == "DTSTART":
            current["due_at"] = _parse_ics_time(value, params)
        elif name == "RRULE":
            current["recurrence"] = _parse_rrule(value)
    return events


def _unescape_ics(value: str) -> str:
    return (
        value.replace("\\n", " ")
        .replace("\\N", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    ).strip()


def _parse_ics_time(value: str, params: str) -> float | None:
    """Turn a DTSTART into a local timestamp, or nothing if it is unreadable."""

    raw = value.strip()
    if not raw:
        return None
    if "VALUE=DATE" in params.upper() and len(raw) == 8:
        with suppress(ValueError):
            parsed = datetime.strptime(raw, "%Y%m%d")
            # An all-day item is not a 00:00 alarm. Nine in the morning is the
            # honest reading of "some time that day".
            return parsed.replace(hour=9).timestamp()
        return None
    if raw.endswith("Z"):
        with suppress(ValueError):
            return (
                datetime.strptime(raw, "%Y%m%dT%H%M%SZ")
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
        return None
    zone = _declared_zone(params)
    with suppress(ValueError):
        naive = datetime.strptime(raw, "%Y%m%dT%H%M%S")
        # A TZID-qualified time means what it says in that zone. Reading a
        # 09:00 New York standup as 09:00 here is not a rounding error, it is a
        # meeting missed by the width of the Atlantic. A floating time, or a
        # zone this machine's database does not know, still falls back to local
        # — that is the same clock every briefing is spoken against.
        if zone is not None:
            return naive.replace(tzinfo=zone).timestamp()
        return naive.timestamp()
    with suppress(ValueError):
        return datetime.strptime(raw, "%Y%m%d").replace(hour=9).timestamp()
    return None


def _declared_zone(params: str) -> tzinfo | None:
    """The TZID this property declares, when the machine's zone database has it."""

    found = re.search(r"TZID=([^;:]+)", params, flags=re.IGNORECASE)
    if found is None:
        return None
    name = found.group(1).strip().strip('"')
    # Outlook writes TZID="(UTC-05:00) Eastern Time"; ZoneInfo will not know it,
    # and inventing an offset from a display string is how a calendar entry ends
    # up an hour wrong with no way to tell.
    with suppress(ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        return ZoneInfo(name)
    return None


def _parse_rrule(value: str) -> Recurrence | None:
    parts = {}
    for chunk in value.split(";"):
        key, _, raw = chunk.partition("=")
        parts[key.strip().upper()] = raw.strip().upper()
    frequency = _FREQ_MAP.get(parts.get("FREQ", ""))
    if frequency is None:
        # HOURLY, MINUTELY, or anything exotic. Saying "I do not model this"
        # beats inventing a daily reminder that was never in the calendar.
        return None
    interval = 1
    with suppress(ValueError):
        interval = max(1, min(365, int(parts.get("INTERVAL", "1"))))
    with suppress(CommitmentError):
        return Recurrence(frequency=frequency, interval=interval)
    return None  # pragma: no cover - Recurrence only raises via parse()


# ---- local reminders database --------------------------------------------


class LocalRemindersDbAdapter:
    """A local sqlite reminders table, read strictly read-only.

    Serena's older reminder daemon kept one, and so do several free Linux
    reminder tools. Opening it in read-only mode is the point: this can surface
    what another program owns without ever becoming a second writer to it.
    """

    name = "local_reminders"

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        table: str = "reminders",
    ) -> None:
        configured = path or os.environ.get("SERENA_REMINDERS_DB_PATH", "").strip()
        self.path = Path(configured).expanduser() if configured else None
        self.table = table if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table) else "reminders"

    def status(self) -> SourceStatus:
        if self.path is None:
            return SourceStatus(
                False,
                "no local reminders database configured (set SERENA_REMINDERS_DB_PATH)",
            )
        if not self.path.is_file():
            return SourceStatus(
                False, f"local reminders database does not exist: {self.path}"
            )
        try:
            with self._connect() as connection:
                connection.execute(f"SELECT 1 FROM {self.table} LIMIT 1").fetchone()
        except sqlite3.Error as error:
            return SourceStatus(False, f"local reminders database unreadable: {_short(error)}")
        return SourceStatus(True, f"reading reminders from {self.path}")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self.path}?mode=ro", uri=True, timeout=5
        )
        connection.row_factory = sqlite3.Row
        return connection

    def fetch(self, *, now: float) -> list[CommitmentCandidate]:
        if not self.status().available:
            return []
        candidates: list[CommitmentCandidate] = []
        try:
            with self._connect() as connection:
                columns = {
                    str(row["name"])
                    for row in connection.execute(
                        f"PRAGMA table_info({self.table})"
                    ).fetchall()
                }
                if "id" not in columns:
                    return []
                message_column = next(
                    (name for name in ("message", "title", "text", "summary") if name in columns),
                    None,
                )
                if message_column is None:
                    return []
                due_column = next(
                    (name for name in ("due_at", "due", "remind_at", "fire_at") if name in columns),
                    None,
                )
                selected = f"id, {message_column} AS message"
                if due_column:
                    selected += f", {due_column} AS due"
                if "fired" in columns:
                    selected += ", fired"
                rows = connection.execute(
                    f"SELECT {selected} FROM {self.table} LIMIT ?",
                    (MAX_CANDIDATES_PER_ADAPTER,),
                ).fetchall()
        except sqlite3.Error:
            return []
        for row in rows:
            keys = row.keys()
            if "fired" in keys and row["fired"]:
                continue
            message = str(row["message"] or "").strip()
            if not message:
                continue
            candidates.append(
                CommitmentCandidate(
                    title=message.splitlines()[0],
                    source=self.name,
                    source_ref=str(row["id"]),
                    detail=message,
                    owner="raghav",
                    priority="normal",
                    due_at=_coerce_due(row["due"] if "due" in keys else None),
                )
            )
        return candidates


def _coerce_due(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raw = str(value).strip()
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        with suppress(ValueError):
            return datetime.strptime(raw, pattern).timestamp()
    with suppress(ValueError):
        return datetime.fromisoformat(raw).timestamp()
    return None


# ---- ingestion ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngestReport:
    created: list[str] = field(default_factory=list)
    corrected: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    unavailable: dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.created) + len(self.corrected) + len(self.unchanged)

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": list(self.created),
            "corrected": list(self.corrected),
            "unchanged": list(self.unchanged),
            "unavailable": dict(self.unavailable),
            "total": self.total,
        }


def default_sources() -> list[CommitmentSource]:
    """Every local input Serena knows about, configured or not.

    Unconfigured adapters are still returned so a status surface can say "no
    calendar directory is set" rather than silently listing two sources and
    leaving him wondering where his calendar went.
    """

    return [MemoryTaskAdapter(), LocalIcsAdapter(), LocalRemindersDbAdapter()]


def ingest(
    store: CommitmentStore,
    sources: Iterable[CommitmentSource] | None = None,
    *,
    actor: str = "serena",
    now: float | None = None,
) -> IngestReport:
    """Pull every available local source into the commitment store, once.

    Idempotent on (source, source_ref). A second pass over an unchanged calendar
    reports everything as unchanged and writes nothing, so this is safe to put
    on a timer.
    """

    import time as _time

    moment = float(_time.time() if now is None else now)
    created: list[str] = []
    corrected: list[str] = []
    unchanged: list[str] = []
    unavailable: dict[str, str] = {}

    for source in list(sources if sources is not None else default_sources()):
        try:
            status = source.status()
        except Exception as error:  # an adapter must not break the sweep
            unavailable[getattr(source, "name", "unknown")] = _short(error)
            continue
        if not status.available:
            unavailable[source.name] = status.reason
            continue
        try:
            candidates = source.fetch(now=moment)
        except Exception as error:
            unavailable[source.name] = f"fetch failed: {_short(error)}"
            continue
        for candidate in candidates[:MAX_CANDIDATES_PER_ADAPTER]:
            try:
                existing = store.find_by_source(candidate.source, candidate.source_ref)
                if existing is None:
                    fresh = store.propose(
                        title=candidate.title,
                        actor=actor,
                        source=candidate.source,
                        source_ref=candidate.source_ref,
                        detail=candidate.detail,
                        owner=candidate.owner,
                        priority=candidate.priority,
                        due_at=candidate.due_at,
                        recurrence=candidate.recurrence,
                        subject_entity_id=candidate.subject_entity_id,
                        now=moment,
                    )
                    created.append(fresh.commitment_id)
                    continue
                if not existing.is_open():
                    # Already finished or dropped. Re-reading the source must not
                    # resurrect something he closed.
                    unchanged.append(existing.commitment_id)
                    continue
                changes = _upstream_changes(candidate, existing)
                if not changes:
                    unchanged.append(existing.commitment_id)
                    continue
                store.correct(
                    existing.commitment_id,
                    actor=actor,
                    reason=f"{candidate.source} changed upstream",
                    now=moment,
                    **changes,
                )
                corrected.append(existing.commitment_id)
            except (CommitmentError, KeyError) as error:
                unavailable[source.name] = f"rejected a candidate: {_short(error)}"
    return IngestReport(
        created=created,
        corrected=corrected,
        unchanged=unchanged,
        unavailable=unavailable,
    )


def source_report(sources: Iterable[CommitmentSource] | None = None) -> dict[str, Any]:
    """What each local input is doing right now, for a status surface."""

    report: dict[str, Any] = {}
    for source in list(sources if sources is not None else default_sources()):
        try:
            status = source.status()
        except Exception as error:
            status = SourceStatus(False, _short(error))
        report[getattr(source, "name", "unknown")] = status.to_dict()
    return report


def _upstream_changes(candidate: CommitmentCandidate, existing: Any) -> dict[str, object]:
    """Every source-owned field the upstream entry now disagrees with.

    Only fields the adapter actually asserts are compared. An adapter that
    returns no detail, or a calendar entry with no RRULE, is saying "I have
    nothing to say about this", not "delete what you have" — a `.ics` file that
    never modelled recurrence must not silently strip a repeat someone set by
    hand. Comparing the whole asserted set is what makes a second pass over a
    genuinely changed calendar converge instead of correcting the title forever
    while the priority stays wrong.
    """

    changes: dict[str, object] = {}
    if candidate.due_at is not None and candidate.due_at != existing.due_at:
        changes["due_at"] = candidate.due_at
    if candidate.title and candidate.title != existing.title:
        changes["title"] = candidate.title
    if candidate.detail and candidate.detail != existing.detail:
        changes["detail"] = candidate.detail
    if candidate.priority and candidate.priority != existing.priority:
        changes["priority"] = candidate.priority
    if candidate.recurrence is not None and candidate.recurrence != existing.recurrence:
        changes["recurrence"] = candidate.recurrence
    return changes


def _short(value: object, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]


__all__ = [
    "CommitmentCandidate",
    "CommitmentSource",
    "IngestReport",
    "LocalIcsAdapter",
    "LocalRemindersDbAdapter",
    "MemoryTaskAdapter",
    "SourceStatus",
    "default_sources",
    "ingest",
    "source_report",
]
