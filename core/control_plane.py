"""Small shared lifecycle envelope and obligation ledger for Serena.

This is a control-plane seam, not a replacement runtime. Surfaces can publish
their native events through an idempotent outbox while retaining their own
authoritative state stores.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONTROL_DB_PATH = Path.home() / ".local" / "state" / "serena" / "control-plane.sqlite3"
SCHEMA_VERSION = 1
MAX_PAYLOAD_CHARS = 64_000
MAX_SUMMARY_CHARS = 4_000
SURFACES = frozenset(
    {
        "chat",
        "voice",
        "fleet",
        "memory",
        "tool",
        "notification",
        "system",
        # Doing something in the world, and the device layer underneath it.
        # core.action_authority publishes here so one ledger holds every
        # action Serena took, whichever surface asked for it.
        "action",
        "device",
    }
)
DELIVERY_STATES = frozenset({"not_applicable", "pending", "delivered", "uncertain", "failed"})
OBLIGATION_STATES = frozenset({"open", "fulfilled", "ambiguous", "cancelled"})
_TOKEN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    event_id: str
    surface: str
    event_type: str
    session_id: str | None
    turn_id: str | None
    request_id: str | None
    job_id: str | None
    provider: str | None
    authority: str
    lifecycle_state: str
    delivery_state: str
    payload: dict[str, Any]
    occurred_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ObligationRule:
    """One surface's declaration of something Serena owes and how it closes."""

    surface: str
    kind: str
    slug: str
    open_on: str
    fulfill_on: tuple[str, ...]
    fail_on: tuple[str, ...] = ()
    cancel_on: tuple[str, ...] = ()
    default_summary: str = "deliver this result"
    default_error: str = "delivery failed"
    # A payload flag that means this event should not create an obligation at
    # all, such as a Fleet dry run that owes nobody anything.
    suppress_key: str = ""


# Each row is a real promise to Raghav with a real closing event. Adding a
# surface here is what "migrating" it means: its native journal stays
# authoritative, and this ledger tracks only the outstanding obligation.
OBLIGATION_RULES: tuple[ObligationRule, ...] = (
    ObligationRule(
        surface="fleet",
        kind="final_result_delivery",
        slug="final-delivery",
        open_on="run.created",
        fulfill_on=("run.notification.delivered",),
        fail_on=("run.notification.failed",),
        cancel_on=("run.cancelled",),
        default_summary="deliver the Fleet result",
        default_error="notification delivery failed",
        suppress_key="dry_run",
    ),
    ObligationRule(
        surface="voice",
        kind="spoken_job_result_delivery",
        slug="result-delivery",
        open_on="job.accepted",
        fulfill_on=("job.result.delivered",),
        fail_on=("job.result.failed",),
        cancel_on=("job.cancelled",),
        default_summary="tell Raghav how the spoken coding job ended",
        default_error="spoken result delivery failed",
    ),
    ObligationRule(
        surface="memory",
        kind="memory_proposal_review",
        slug="proposal-review",
        open_on="proposal.created",
        fulfill_on=("proposal.approved", "proposal.rejected"),
        cancel_on=("proposal.discarded",),
        default_summary="get a decision on this proposed memory change",
        default_error="memory proposal review failed",
    ),
    ObligationRule(
        surface="notification",
        kind="notification_delivery",
        slug="delivery",
        open_on="notice.queued",
        fulfill_on=("notice.delivered",),
        fail_on=("notice.failed",),
        cancel_on=("notice.cancelled",),
        default_summary="deliver this notice to Raghav",
        default_error="notice delivery failed",
    ),
    ObligationRule(
        surface="tool",
        kind="tool_result_delivery",
        slug="result-delivery",
        open_on="call.started",
        fulfill_on=("call.completed",),
        fail_on=("call.failed",),
        cancel_on=("call.cancelled",),
        default_summary="return this tool result to the caller",
        default_error="tool call failed",
    ),
    ObligationRule(
        surface="chat",
        kind="chat_reply_delivery",
        slug="reply",
        open_on="turn.started",
        fulfill_on=("turn.completed",),
        fail_on=("turn.failed",),
        cancel_on=("turn.cancelled",),
        default_summary="finish answering this turn",
        default_error="chat reply failed",
    ),
    ObligationRule(
        surface="action",
        kind="authorized_action_completion",
        slug="completion",
        open_on="action.authorized",
        # An authorized action that never reports back is the dangerous case:
        # Serena does not know whether the light changed or the message went.
        # Keeping it open is what lets restart recovery ask instead of assume.
        fulfill_on=("action.completed", "action.simulated"),
        fail_on=("action.failed",),
        cancel_on=("action.denied", "action.abandoned"),
        default_summary="finish and report this authorized action",
        default_error="the authorized action failed",
        suppress_key="dry_run",
    ),
)


@dataclass(frozen=True, slots=True)
class Obligation:
    obligation_id: str
    surface: str
    kind: str
    summary: str
    state: str
    session_id: str | None
    request_id: str | None
    job_id: str | None
    fulfillment_event_id: str | None
    attempts: int
    last_error: str | None
    created_at: float
    updated_at: float
    resolved_at: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ControlPlaneStore:
    def __init__(self, path: Path | None = None) -> None:
        configured = os.environ.get("SERENA_CONTROL_PLANE_DB_PATH", "").strip()
        fleet_path = os.environ.get("SERENA_FLEET_DB_PATH", "").strip()
        colocated = Path(fleet_path).expanduser().with_name("control-plane.sqlite3") if fleet_path else None
        self.path = Path(path or configured or colocated or DEFAULT_CONTROL_DB_PATH).expanduser()
        self._initialize()

    def append_event(
        self,
        *,
        surface: str,
        event_type: str,
        lifecycle_state: str,
        payload: dict[str, Any] | None = None,
        event_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        request_id: str | None = None,
        job_id: str | None = None,
        provider: str | None = None,
        authority: str = "internal",
        delivery_state: str = "not_applicable",
        occurred_at: float | None = None,
    ) -> EventEnvelope:
        envelope = EventEnvelope(
            event_id=_identifier(event_id or str(uuid.uuid4()), "event_id"),
            surface=_surface(surface),
            event_type=_event_type(event_type),
            session_id=_optional_identifier(session_id, "session_id"),
            turn_id=_optional_identifier(turn_id, "turn_id"),
            request_id=_optional_identifier(request_id, "request_id"),
            job_id=_optional_identifier(job_id, "job_id"),
            provider=_optional_text(provider, 64),
            authority=_required_text(authority, "authority", 64),
            lifecycle_state=_required_text(lifecycle_state, "lifecycle_state", 64),
            delivery_state=_delivery_state(delivery_state),
            payload=_bounded_payload(payload or {}),
            occurred_at=float(time.time() if occurred_at is None else occurred_at),
        )
        return self.append_envelope(envelope)

    def append_envelope(self, value: EventEnvelope | dict[str, Any]) -> EventEnvelope:
        raw = value.to_dict() if isinstance(value, EventEnvelope) else dict(value)
        envelope = EventEnvelope(
            event_id=_identifier(raw.get("event_id"), "event_id"),
            surface=_surface(raw.get("surface")),
            event_type=_event_type(raw.get("event_type")),
            session_id=_optional_identifier(raw.get("session_id"), "session_id"),
            turn_id=_optional_identifier(raw.get("turn_id"), "turn_id"),
            request_id=_optional_identifier(raw.get("request_id"), "request_id"),
            job_id=_optional_identifier(raw.get("job_id"), "job_id"),
            provider=_optional_text(raw.get("provider"), 64),
            authority=_required_text(raw.get("authority"), "authority", 64),
            lifecycle_state=_required_text(
                raw.get("lifecycle_state"), "lifecycle_state", 64
            ),
            delivery_state=_delivery_state(raw.get("delivery_state")),
            payload=_bounded_payload(raw.get("payload") or {}),
            occurred_at=float(raw.get("occurred_at") or time.time()),
        )
        encoded = json.dumps(
            envelope.payload, separators=(",", ":"), sort_keys=True, default=str
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO control_events(
                    event_id, surface, event_type, session_id, turn_id, request_id,
                    job_id, provider, authority, lifecycle_state, delivery_state,
                    payload_json, occurred_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope.event_id,
                    envelope.surface,
                    envelope.event_type,
                    envelope.session_id,
                    envelope.turn_id,
                    envelope.request_id,
                    envelope.job_id,
                    envelope.provider,
                    envelope.authority,
                    envelope.lifecycle_state,
                    envelope.delivery_state,
                    encoded,
                    envelope.occurred_at,
                    time.time(),
                ),
            ).rowcount
            if inserted:
                self._apply_obligation_rules(connection, envelope)
            row = connection.execute(
                "SELECT * FROM control_events WHERE event_id = ?", (envelope.event_id,)
            ).fetchone()
            assert row is not None
            return _event_from_row(row)

    def events(
        self,
        *,
        surface: str | None = None,
        job_id: str | None = None,
        limit: int = 100,
    ) -> list[EventEnvelope]:
        clauses: list[str] = []
        params: list[object] = []
        if surface is not None:
            clauses.append("surface = ?")
            params.append(_surface(surface))
        if job_id is not None:
            clauses.append("job_id = ?")
            params.append(_identifier(job_id, "job_id"))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(min(1_000, max(1, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM control_events"
                + where
                + " ORDER BY occurred_at DESC, rowid DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def create_obligation(
        self,
        *,
        surface: str,
        kind: str,
        summary: str,
        obligation_id: str | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
        job_id: str | None = None,
    ) -> Obligation:
        identifier = _identifier(obligation_id or str(uuid.uuid4()), "obligation_id")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO control_obligations(
                    obligation_id, surface, kind, summary, state, session_id,
                    request_id, job_id, attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, 0, ?, ?)
                """,
                (
                    identifier,
                    _surface(surface),
                    _required_text(kind, "kind", 128),
                    _required_text(summary, "summary", MAX_SUMMARY_CHARS),
                    _optional_identifier(session_id, "session_id"),
                    _optional_identifier(request_id, "request_id"),
                    _optional_identifier(job_id, "job_id"),
                    now,
                    now,
                ),
            )
            return self._require_obligation(connection, identifier)

    def resolve_obligation(
        self,
        obligation_id: str,
        *,
        state: str,
        event_id: str | None = None,
        error: str | None = None,
    ) -> Obligation:
        identifier = _identifier(obligation_id, "obligation_id")
        final_state = str(state or "").strip().lower()
        if final_state not in OBLIGATION_STATES - {"open"}:
            raise ValueError("obligation state must be fulfilled, ambiguous, or cancelled")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_obligation(connection, identifier)
            connection.execute(
                """
                UPDATE control_obligations SET state = ?, fulfillment_event_id = ?,
                    last_error = ?, resolved_at = ?, updated_at = ?
                WHERE obligation_id = ?
                """,
                (
                    final_state,
                    _optional_identifier(event_id, "event_id"),
                    _optional_text(error, MAX_SUMMARY_CHARS),
                    now,
                    now,
                    identifier,
                ),
            )
            return self._require_obligation(connection, identifier)

    def obligations(
        self,
        *,
        state: str | None = None,
        surface: str | None = None,
        limit: int = 100,
    ) -> list[Obligation]:
        clauses: list[str] = []
        params: list[object] = []
        if state is not None:
            clean_state = str(state).strip().lower()
            if clean_state not in OBLIGATION_STATES:
                raise ValueError("invalid obligation state")
            clauses.append("state = ?")
            params.append(clean_state)
        if surface is not None:
            clauses.append("surface = ?")
            params.append(_surface(surface))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(min(1_000, max(1, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM control_obligations"
                + where
                + " ORDER BY created_at, obligation_id LIMIT ?",
                tuple(params),
            ).fetchall()
        return [_obligation_from_row(row) for row in rows]

    def _apply_obligation_rules(
        self,
        connection: sqlite3.Connection,
        event: EventEnvelope,
    ) -> None:
        """Open, fulfil, or fail the obligations this event speaks to.

        Fleet's final-delivery rule used to be written inline here. It is now
        one row in OBLIGATION_RULES with identical semantics, so the other
        surfaces that owe Raghav something can declare their lifecycle instead
        of each growing a private copy of this logic.
        """

        if not event.job_id:
            return
        for rule in OBLIGATION_RULES:
            if rule.surface != event.surface:
                continue
            obligation_id = f"{rule.surface}:{event.job_id}:{rule.slug}"
            if event.event_type == rule.open_on:
                if rule.suppress_key and bool(event.payload.get(rule.suppress_key)):
                    continue
                now = time.time()
                connection.execute(
                    """
                    INSERT OR IGNORE INTO control_obligations(
                        obligation_id, surface, kind, summary, state, session_id,
                        request_id, job_id, attempts, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        obligation_id,
                        rule.surface,
                        rule.kind,
                        str(event.payload.get("summary") or rule.default_summary)[
                            :MAX_SUMMARY_CHARS
                        ],
                        event.session_id,
                        event.request_id,
                        event.job_id,
                        now,
                        now,
                    ),
                )
            elif event.event_type in rule.fulfill_on:
                now = time.time()
                connection.execute(
                    """
                    UPDATE control_obligations SET state = 'fulfilled',
                        fulfillment_event_id = ?, last_error = NULL,
                        resolved_at = ?, updated_at = ?
                    WHERE obligation_id = ? AND state = 'open'
                    """,
                    (event.event_id, now, now, obligation_id),
                )
            elif event.event_type in rule.fail_on:
                # A failed attempt is durable evidence, not a resolution. The
                # obligation stays open so recovery can retry it.
                connection.execute(
                    """
                    UPDATE control_obligations SET attempts = attempts + 1,
                        last_error = ?, updated_at = ?
                    WHERE obligation_id = ? AND state = 'open'
                    """,
                    (
                        str(event.payload.get("error") or rule.default_error)[
                            :MAX_SUMMARY_CHARS
                        ],
                        time.time(),
                        obligation_id,
                    ),
                )
            elif event.event_type in rule.cancel_on:
                now = time.time()
                connection.execute(
                    """
                    UPDATE control_obligations SET state = 'cancelled',
                        resolved_at = ?, updated_at = ?
                    WHERE obligation_id = ? AND state = 'open'
                    """,
                    (now, now, obligation_id),
                )

    def recoverable_obligations(
        self,
        *,
        surface: str | None = None,
        older_than: float = 0.0,
        max_attempts: int = 0,
        now: float | None = None,
    ) -> list[Obligation]:
        """Open obligations a restarted Serena still owes and may retry.

        Crash recovery reads this, not each surface's private journal. The
        native journal still owns how to redeliver; this only says what is
        outstanding and how many times it has already been tried.
        """

        moment = time.time() if now is None else float(now)
        cutoff = moment - max(0.0, float(older_than))
        entries = [
            item
            for item in self.obligations(state="open", surface=surface, limit=1_000)
            if item.created_at <= cutoff
        ]
        if max_attempts > 0:
            entries = [item for item in entries if item.attempts < int(max_attempts)]
        return entries

    def abandon_obligation(self, obligation_id: str, *, reason: str) -> Obligation:
        """Give up on an obligation without ever calling it delivered.

        This resolves to `ambiguous`, never `fulfilled`. Serena not knowing
        whether Raghav got something is a real state, and flattening it into
        success is the exact dishonesty the ledger exists to prevent.
        """

        return self.resolve_obligation(obligation_id, state="ambiguous", error=reason)

    @staticmethod
    def _require_obligation(
        connection: sqlite3.Connection, obligation_id: str
    ) -> Obligation:
        row = connection.execute(
            "SELECT * FROM control_obligations WHERE obligation_id = ?", (obligation_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown Serena obligation {obligation_id}")
        return _obligation_from_row(row)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS control_events (
                    event_id TEXT PRIMARY KEY,
                    surface TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    session_id TEXT,
                    turn_id TEXT,
                    request_id TEXT,
                    job_id TEXT,
                    provider TEXT,
                    authority TEXT NOT NULL,
                    lifecycle_state TEXT NOT NULL,
                    delivery_state TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS control_events_job_idx
                    ON control_events(job_id, occurred_at);
                CREATE INDEX IF NOT EXISTS control_events_session_idx
                    ON control_events(session_id, occurred_at);
                CREATE INDEX IF NOT EXISTS control_events_request_idx
                    ON control_events(request_id, occurred_at);

                CREATE TABLE IF NOT EXISTS control_obligations (
                    obligation_id TEXT PRIMARY KEY,
                    surface TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    state TEXT NOT NULL,
                    session_id TEXT,
                    request_id TEXT,
                    job_id TEXT,
                    fulfillment_event_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    resolved_at REAL
                );
                CREATE INDEX IF NOT EXISTS control_obligations_state_idx
                    ON control_obligations(state, surface, created_at);
                """
            )
        if os.name != "nt":
            with suppress(OSError):
                self.path.chmod(0o600)


class SurfaceOutbox:
    """A transactional outbox any SQLite-backed Serena surface can adopt.

    Fleet proved the pattern inline: commit the envelope into the surface's own
    database in the same transaction as the state change, then publish
    idempotently later. A crash between the two can delay publication but
    cannot invent or lose an event.

    This generalizes it so voice, memory, tool, and notification stores get the
    same guarantee without their native journals stopping being authoritative.
    """

    def __init__(self, surface: str, path: Path) -> None:
        self.surface = _surface(surface)
        self.path = Path(path).expanduser()
        self._initialize()

    def stage(self, connection: sqlite3.Connection, envelope: EventEnvelope) -> None:
        """Queue an envelope inside the caller's already-open transaction.

        The caller passes its own connection on purpose. That is what makes the
        outbox row and the surface's own state change atomic.
        """

        connection.execute(
            "INSERT OR IGNORE INTO control_outbox(event_id, envelope_json, created_at) "
            "VALUES (?, ?, ?)",
            (
                envelope.event_id,
                json.dumps(envelope.to_dict(), separators=(",", ":"), sort_keys=True, default=str),
                time.time(),
            ),
        )

    def stage_event(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        lifecycle_state: str,
        event_id: str | None = None,
        **fields: Any,
    ) -> EventEnvelope:
        envelope = EventEnvelope(
            event_id=_identifier(event_id or str(uuid.uuid4()), "event_id"),
            surface=self.surface,
            event_type=_event_type(event_type),
            session_id=_optional_identifier(fields.get("session_id"), "session_id"),
            turn_id=_optional_identifier(fields.get("turn_id"), "turn_id"),
            request_id=_optional_identifier(fields.get("request_id"), "request_id"),
            job_id=_optional_identifier(fields.get("job_id"), "job_id"),
            provider=_optional_text(fields.get("provider"), 64),
            authority=_required_text(fields.get("authority") or "internal", "authority", 64),
            lifecycle_state=_required_text(lifecycle_state, "lifecycle_state", 64),
            delivery_state=_delivery_state(fields.get("delivery_state") or "not_applicable"),
            payload=_bounded_payload(fields.get("payload") or {}),
            occurred_at=float(fields.get("occurred_at") or time.time()),
        )
        self.stage(connection, envelope)
        return envelope

    def flush(self, control_store: "ControlPlaneStore | None" = None, *, limit: int = 250) -> int:
        """Publish staged envelopes, stopping at the first failure.

        Stopping preserves ordering and keeps the committed source row, which is
        what lets a later pass retry after the control plane is repaired.
        """

        target = control_store or ControlPlaneStore()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_id, envelope_json FROM control_outbox ORDER BY rowid LIMIT ?",
                (min(1_000, max(1, int(limit))),),
            ).fetchall()
        flushed = 0
        for row in rows:
            try:
                envelope = json.loads(str(row["envelope_json"]))
                if not isinstance(envelope, dict):
                    raise ValueError("outbox envelope must be an object")
                target.append_envelope(envelope)
            except Exception:
                break
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM control_outbox WHERE event_id = ?", (str(row["event_id"]),)
                )
            flushed += 1
        return flushed

    def pending(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM control_outbox").fetchone()
        return int(row["count"] or 0)

    def connect(self) -> sqlite3.Connection:
        return self._connect()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS control_outbox (
                    event_id TEXT PRIMARY KEY,
                    envelope_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )


def _event_from_row(row: sqlite3.Row) -> EventEnvelope:
    try:
        payload = json.loads(str(row["payload_json"] or "{}"))
    except json.JSONDecodeError:
        payload = {}
    return EventEnvelope(
        event_id=str(row["event_id"]),
        surface=str(row["surface"]),
        event_type=str(row["event_type"]),
        session_id=row["session_id"],
        turn_id=row["turn_id"],
        request_id=row["request_id"],
        job_id=row["job_id"],
        provider=row["provider"],
        authority=str(row["authority"]),
        lifecycle_state=str(row["lifecycle_state"]),
        delivery_state=str(row["delivery_state"]),
        payload=payload if isinstance(payload, dict) else {},
        occurred_at=float(row["occurred_at"]),
    )


def _obligation_from_row(row: sqlite3.Row) -> Obligation:
    return Obligation(
        obligation_id=str(row["obligation_id"]),
        surface=str(row["surface"]),
        kind=str(row["kind"]),
        summary=str(row["summary"]),
        state=str(row["state"]),
        session_id=row["session_id"],
        request_id=row["request_id"],
        job_id=row["job_id"],
        fulfillment_event_id=row["fulfillment_event_id"],
        attempts=int(row["attempts"] or 0),
        last_error=row["last_error"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        resolved_at=float(row["resolved_at"]) if row["resolved_at"] else None,
    )


def _bounded_payload(value: object) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {"value": value}
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    if len(encoded) <= MAX_PAYLOAD_CHARS:
        return json.loads(encoded)
    return {"truncated": True, "preview": encoded[:MAX_PAYLOAD_CHARS]}


def _surface(value: object) -> str:
    clean = str(value or "").strip().lower()
    if clean not in SURFACES:
        raise ValueError("invalid Serena event surface")
    return clean


def _delivery_state(value: object) -> str:
    clean = str(value or "").strip().lower()
    if clean not in DELIVERY_STATES:
        raise ValueError("invalid Serena event delivery state")
    return clean


def _event_type(value: object) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > 160 or not re.fullmatch(r"[A-Za-z0-9._:-]+", clean):
        raise ValueError("invalid Serena event type")
    return clean


def _identifier(value: object, name: str) -> str:
    clean = str(value or "").strip()
    if not _TOKEN.fullmatch(clean):
        raise ValueError(f"invalid Serena {name}")
    return clean


def _optional_identifier(value: object, name: str) -> str | None:
    clean = str(value or "").strip()
    return _identifier(clean, name) if clean else None


def _required_text(value: object, name: str, limit: int) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > limit:
        raise ValueError(f"invalid Serena {name}")
    return clean


def _optional_text(value: object, limit: int) -> str | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    return clean[:limit]
