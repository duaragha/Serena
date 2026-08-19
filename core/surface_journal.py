"""The adapter every native Serena surface uses to join the control plane.

`core.control_plane` already proved the mechanism: stage an envelope inside the
surface's own transaction, publish it idempotently later, and let the shared
obligation ledger track what is still outstanding. Fleet and the notification
authority were wired to it directly, each with a private copy of the staging
and flushing dance.

This module is that dance, once, so voice, chat, tool, and memory can migrate
without any of them growing their own version. Three things make it safe to put
in front of a live surface:

- The event names are read from ``OBLIGATION_RULES``, never hardcoded here. A
  journal physically cannot emit an event its own surface's ledger rule does
  not recognise, so the seam and the ledger cannot drift apart.
- Event ids are derived, not random. Replaying the same attempt is a no-op;
  only a genuinely new attempt counts as a new attempt. The attempt counter is
  a durable row, so a crash mid-retry does not reset it and let redelivery run
  forever.
- The native journal stays authoritative. Nothing here decides how to deliver
  anything, or writes to the surface's own tables. It records that Serena owes
  Raghav something and, later, that she stopped owing it.

Ambiguity is a first-class outcome. When a surface genuinely cannot tell
whether Raghav received something, it says so, and the obligation resolves
`ambiguous` rather than quietly becoming a success.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from core.control_plane import (
    OBLIGATION_RULES,
    ControlPlaneStore,
    EventEnvelope,
    ObligationRule,
    SurfaceOutbox,
)

DEFAULT_JOURNAL_DIR = Path.home() / ".local" / "state" / "serena"
MAX_ERROR_CHARS = 2_000

# Same shape the control plane enforces on identifiers. Checked here too so a
# caller with a messy native id gets a stable derived token instead of a
# ValueError thrown out of a live voice turn.
_TOKEN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

# Surfaces that own a real, migratable lifecycle. Fleet and notification are
# deliberately absent: both already publish through their own store and would
# double-count if a journal published for them as well.
MIGRATABLE_SURFACES = ("voice", "chat", "tool", "memory")


class SurfaceJournalError(ValueError):
    """This surface cannot be journalled the way the caller asked."""


def rule_for(surface: str) -> ObligationRule:
    """The one ledger rule that defines this surface's lifecycle."""

    clean = str(surface or "").strip().lower()
    for rule in OBLIGATION_RULES:
        if rule.surface == clean:
            return rule
    raise SurfaceJournalError(f"no obligation rule declares the {clean!r} surface")


def safe_token(value: object, *, prefix: str = "id") -> str:
    """A control-plane-legal identifier for an arbitrary native id.

    Native ids are not ours to reformat. A Claude session id is fine as-is; a
    turn key with a space or a slash in it is not. Rather than refuse the event
    and lose the obligation, an illegal id becomes a stable digest of itself,
    so the same native id always maps to the same token.
    """

    raw = str(value or "").strip()
    if not raw:
        raise SurfaceJournalError("a journalled lifecycle needs a native id")
    if _TOKEN.fullmatch(raw):
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


class SurfaceJournal:
    """One surface's transactional outbox onto the shared control plane."""

    def __init__(
        self,
        surface: str,
        path: Path | None = None,
        *,
        control_store: Any | None = None,
    ) -> None:
        self.rule = rule_for(surface)
        self.surface = self.rule.surface
        self.path = Path(
            path or DEFAULT_JOURNAL_DIR / f"{self.surface}-journal.sqlite3"
        ).expanduser()
        self._control_store = control_store
        self._outbox = SurfaceOutbox(self.surface, self.path)
        self._initialize()

    # -- lifecycle names, taken from the ledger rule ------------------------

    @property
    def open_event(self) -> str:
        return self.rule.open_on

    @property
    def fulfil_event(self) -> str:
        return self.rule.fulfill_on[0]

    @property
    def fail_event(self) -> str | None:
        return self.rule.fail_on[0] if self.rule.fail_on else None

    @property
    def cancel_event(self) -> str | None:
        return self.rule.cancel_on[0] if self.rule.cancel_on else None

    def obligation_id(self, job_id: str) -> str:
        return f"{self.surface}:{safe_token(job_id)}:{self.rule.slug}"

    # -- writing ------------------------------------------------------------

    def opened(
        self,
        job_id: str,
        *,
        summary: str = "",
        connection: sqlite3.Connection | None = None,
        **fields: Any,
    ) -> EventEnvelope:
        """Record that Serena has taken something on and now owes a result."""

        token = safe_token(job_id)
        payload = dict(fields.pop("payload", None) or {})
        if summary:
            payload["summary"] = str(summary)[:MAX_ERROR_CHARS]
        return self._stage(
            token,
            event_type=self.open_event,
            lifecycle_state=fields.pop("lifecycle_state", "open"),
            event_id=f"{self.surface}:{token}:{self.open_event}",
            payload=payload,
            connection=connection,
            **fields,
        )

    def fulfilled(
        self,
        job_id: str,
        *,
        connection: sqlite3.Connection | None = None,
        **fields: Any,
    ) -> EventEnvelope:
        """Record that the thing Serena owed actually reached Raghav."""

        token = safe_token(job_id)
        return self._stage(
            token,
            event_type=self.fulfil_event,
            lifecycle_state=fields.pop("lifecycle_state", "completed"),
            delivery_state=fields.pop("delivery_state", "delivered"),
            event_id=f"{self.surface}:{token}:{self.fulfil_event}",
            connection=connection,
            **fields,
        )

    def failed(
        self,
        job_id: str,
        *,
        error: str,
        attempt: int | None = None,
        connection: sqlite3.Connection | None = None,
        **fields: Any,
    ) -> EventEnvelope:
        """Record one failed attempt. The obligation deliberately stays open.

        A failure is evidence, not a resolution. Recovery is allowed to try
        again, and the attempt number is durable so the retry budget survives a
        restart instead of silently starting over.
        """

        if self.fail_event is None:
            raise SurfaceJournalError(
                f"the {self.surface} lifecycle has no failure event to record"
            )
        token = safe_token(job_id)
        index = (
            int(attempt)
            if attempt is not None
            else self._next_attempt(token, self.fail_event, connection=connection)
        )
        payload = dict(fields.pop("payload", None) or {})
        payload["error"] = str(error or self.rule.default_error)[:MAX_ERROR_CHARS]
        payload["attempt"] = index
        return self._stage(
            token,
            event_type=self.fail_event,
            lifecycle_state=fields.pop("lifecycle_state", "failed"),
            delivery_state=fields.pop("delivery_state", "failed"),
            event_id=f"{self.surface}:{token}:{self.fail_event}:{index}",
            payload=payload,
            connection=connection,
            **fields,
        )

    def cancelled(
        self,
        job_id: str,
        *,
        reason: str = "",
        connection: sqlite3.Connection | None = None,
        **fields: Any,
    ) -> EventEnvelope:
        """Close the obligation without ever claiming Raghav got anything."""

        if self.cancel_event is None:
            raise SurfaceJournalError(
                f"the {self.surface} lifecycle has no cancellation event to record"
            )
        token = safe_token(job_id)
        payload = dict(fields.pop("payload", None) or {})
        if reason:
            payload["reason"] = str(reason)[:MAX_ERROR_CHARS]
        return self._stage(
            token,
            event_type=self.cancel_event,
            lifecycle_state=fields.pop("lifecycle_state", "cancelled"),
            event_id=f"{self.surface}:{token}:{self.cancel_event}",
            payload=payload,
            connection=connection,
            **fields,
        )

    def ambiguous(self, job_id: str, *, reason: str) -> bool:
        """Serena does not know whether this landed, and says so.

        This never resolves `fulfilled`. It is the honest end state for the
        case where the send left the process but nothing ever confirmed it.
        """

        self.flush()
        target = self._control()
        try:
            target.abandon_obligation(
                self.obligation_id(job_id), reason=str(reason)[:MAX_ERROR_CHARS]
            )
        except KeyError:
            return False
        return True

    @contextmanager
    def track(
        self,
        job_id: str,
        *,
        summary: str = "",
        **fields: Any,
    ) -> Iterator[str]:
        """Open an obligation and guarantee it is closed with the truth.

        Leaving the block normally fulfils it. Raising fails it, with the real
        exception recorded, and re-raises. A hard crash inside the block leaves
        it open on purpose: that is exactly the state restart recovery exists
        to pick up.
        """

        token = safe_token(job_id)
        self.opened(token, summary=summary, **fields)
        try:
            yield token
        except BaseException as error:  # noqa: BLE001 - recorded then re-raised
            with suppress(Exception):
                self.failed(token, error=f"{type(error).__name__}: {error}")
            raise
        else:
            self.fulfilled(token)

    # -- publication --------------------------------------------------------

    def flush(self, *, limit: int = 250) -> int:
        """Publish staged envelopes onto the shared plane, idempotently."""

        return self._outbox.flush(self._control_store, limit=limit)

    def pending(self) -> int:
        return self._outbox.pending()

    def connect(self) -> sqlite3.Connection:
        """The journal's own connection, for callers staging transactionally."""

        return self._outbox.connect()

    # -- internals ----------------------------------------------------------

    def _control(self) -> Any:
        return self._control_store or ControlPlaneStore()

    def _stage(
        self,
        token: str,
        *,
        event_type: str,
        connection: sqlite3.Connection | None,
        **fields: Any,
    ) -> EventEnvelope:
        fields.setdefault("job_id", token)
        fields.setdefault("authority", f"{self.surface}_journal")
        fields.setdefault("occurred_at", time.time())
        if connection is not None:
            # The caller owns the transaction, so the envelope commits with
            # their state change or not at all. Publishing is their call too.
            return self._outbox.stage_event(
                connection, event_type=event_type, **fields
            )
        with self._outbox.connect() as owned:
            owned.execute("BEGIN IMMEDIATE")
            envelope = self._outbox.stage_event(owned, event_type=event_type, **fields)
        with suppress(Exception):
            self.flush()
        return envelope

    def _next_attempt(
        self,
        token: str,
        event_type: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        """Durably hand out the next attempt number for one job's failures."""

        statement = (
            "INSERT INTO surface_journal_attempts(job_id, event_type, attempts) "
            "VALUES (?, ?, 1) ON CONFLICT(job_id, event_type) DO UPDATE SET "
            "attempts = attempts + 1"
        )
        read = (
            "SELECT attempts FROM surface_journal_attempts "
            "WHERE job_id = ? AND event_type = ?"
        )
        if connection is not None:
            connection.execute(statement, (token, event_type))
            row = connection.execute(read, (token, event_type)).fetchone()
            return int(row[0]) if row else 1
        with self._outbox.connect() as owned:
            owned.execute("BEGIN IMMEDIATE")
            owned.execute(statement, (token, event_type))
            row = owned.execute(read, (token, event_type)).fetchone()
            return int(row[0]) if row else 1

    def _initialize(self) -> None:
        with self._outbox.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS surface_journal_attempts (
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (job_id, event_type)
                )
                """
            )


_JOURNALS: dict[str, SurfaceJournal] = {}


def journal(surface: str, *, control_store: Any | None = None) -> SurfaceJournal:
    """The process-wide journal for one surface.

    Live surfaces call this on a hot path, so the sqlite handle and schema
    check are set up once rather than per turn. Tests that pass an explicit
    control store get a fresh instance instead of the shared one.
    """

    clean = str(surface or "").strip().lower()
    if control_store is not None:
        return SurfaceJournal(clean, control_store=control_store)
    existing = _JOURNALS.get(clean)
    if existing is None:
        existing = SurfaceJournal(clean)
        _JOURNALS[clean] = existing
    return existing


def reset_journals() -> None:
    """Drop cached journals. Used by tests that redirect the state directory."""

    _JOURNALS.clear()


def flush_all(*, limit: int = 250) -> dict[str, int]:
    """Publish every migratable surface's staged events.

    Called on the resident loop so an envelope committed by a surface that then
    went idle does not sit unpublished until that surface next does work.
    """

    published: dict[str, int] = {}
    for surface in MIGRATABLE_SURFACES:
        try:
            published[surface] = journal(surface).flush(limit=limit)
        except Exception:
            published[surface] = 0
    return published
