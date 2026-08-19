"""One authority for everything Serena sends Raghav unprompted.

Serena already has several independent ways to interrupt him: the desktop
voice bridge, the Telegram bot, Fleet's terminal notices, document delivery.
Each decided for itself whether to send. That is how an assistant ends up
nagging, double-sending the same event on two channels, or waking someone at
3am because a background job finished.

This module is the chokepoint. It does not replace the existing senders, it
decides whether they may run and records what happened. A caller asks; the
authority answers `sent`, `suppressed`, `deferred`, `pending_approval`, or
`failed`, and every answer is durable.

Bounded on purpose: a fixed channel allowlist, no provider sprawl, no new
messenger integrations. Adding a channel is a code change with a test, not a
config file a plugin can extend.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_DB_PATH = Path.home() / ".local" / "state" / "serena" / "notifications.sqlite3"

# Fixed, reviewed, and small. Not extensible at runtime.
CHANNELS = ("voice", "telegram", "desktop")
DECISIONS = ("sent", "suppressed", "deferred", "pending_approval", "failed")

# Urgency decides what quiet hours may hold back. `critical` is for things that
# are worse to withhold than to interrupt with; it is not a general escape.
URGENCIES = ("low", "normal", "critical")

DEFAULT_QUIET_START_HOUR = 22
DEFAULT_QUIET_END_HOUR = 8
DEFAULT_DEDUPE_WINDOW_SECONDS = 3_600
DEFAULT_HOURLY_LIMIT = 12
DEFAULT_MAX_ATTEMPTS = 3
MAX_SUMMARY_CHARS = 2_000


class NotificationError(ValueError):
    """The notification request was not well formed."""


@dataclass(frozen=True, slots=True)
class NotificationPolicy:
    quiet_start_hour: int = DEFAULT_QUIET_START_HOUR
    quiet_end_hour: int = DEFAULT_QUIET_END_HOUR
    dedupe_window_seconds: int = DEFAULT_DEDUPE_WINDOW_SECONDS
    hourly_limit: int = DEFAULT_HOURLY_LIMIT
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    approval_required_kinds: tuple[str, ...] = ()

    def in_quiet_hours(self, moment: float) -> bool:
        hour = datetime.fromtimestamp(moment).hour
        start, end = self.quiet_start_hour, self.quiet_end_hour
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def next_quiet_end(self, moment: float) -> float:
        """When a deferred notice becomes deliverable again."""

        current = datetime.fromtimestamp(moment)
        candidate = current.replace(
            hour=self.quiet_end_hour % 24, minute=0, second=0, microsecond=0
        )
        release = candidate.timestamp()
        if release <= moment:
            release += 86_400
        return release


@dataclass(frozen=True, slots=True)
class NotificationResult:
    notification_id: str
    decision: str
    reason: str
    channel: str
    attempts: int = 0
    deliver_after: float | None = None

    @property
    def sent(self) -> bool:
        return self.decision == "sent"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class NotificationRequest:
    kind: str
    summary: str
    channel: str = "voice"
    urgency: str = "normal"
    dedupe_key: str = ""
    source_surface: str = "system"
    job_id: str | None = None
    session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _clean(value: object, limit: int = MAX_SUMMARY_CHARS) -> str:
    return " ".join(str(value or "").split())[:limit]


class NotificationAuthority:
    """Durable gatekeeper for outbound notices."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        policy: NotificationPolicy | None = None,
        senders: dict[str, Callable[[NotificationRequest], bool]] | None = None,
        control_store: Any | None = None,
        result_observer: Callable[[NotificationRequest, NotificationResult], None]
        | None = None,
    ) -> None:
        configured = os.environ.get("SERENA_NOTIFICATION_DB_PATH", "").strip()
        self.path = Path(path or configured or DEFAULT_DB_PATH).expanduser()
        self.policy = policy or NotificationPolicy()
        self._senders = dict(senders or {})
        self._control_store = control_store
        self._result_observer = result_observer
        self._initialize()
        from core.control_plane import SurfaceOutbox

        self._outbox = SurfaceOutbox("notification", self.path)

    # -- public API ---------------------------------------------------------

    def request(
        self,
        request: NotificationRequest,
        *,
        now: float | None = None,
    ) -> NotificationResult:
        """Decide and, when allowed, deliver one notice."""

        moment = float(time.time() if now is None else now)
        kind = _clean(request.kind, 128)
        summary = _clean(request.summary)
        channel = _clean(request.channel, 32)
        urgency = _clean(request.urgency, 16) or "normal"
        if not kind:
            raise NotificationError("a notification needs a kind")
        if not summary:
            raise NotificationError("a notification needs a summary")
        if channel not in CHANNELS:
            raise NotificationError(f"unknown notification channel {channel!r}")
        if urgency not in URGENCIES:
            raise NotificationError(f"unknown notification urgency {urgency!r}")

        dedupe_key = _clean(request.dedupe_key, 256) or f"{kind}:{summary}"[:256]
        notification_id = str(uuid.uuid4())

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                """
                SELECT notification_id, created_at FROM notifications
                WHERE dedupe_key = ? AND decision IN ('sent', 'deferred', 'pending_approval')
                  AND created_at >= ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (dedupe_key, moment - self.policy.dedupe_window_seconds),
            ).fetchone()
            if duplicate is not None:
                return self._record(
                    connection,
                    notification_id=notification_id,
                    request=request,
                    kind=kind,
                    summary=summary,
                    channel=channel,
                    urgency=urgency,
                    dedupe_key=dedupe_key,
                    decision="suppressed",
                    reason=(
                        "an equivalent notice was already handled as "
                        f"{duplicate['notification_id']}"
                    ),
                    moment=moment,
                )

            if kind in self.policy.approval_required_kinds:
                return self._record(
                    connection,
                    notification_id=notification_id,
                    request=request,
                    kind=kind,
                    summary=summary,
                    channel=channel,
                    urgency=urgency,
                    dedupe_key=dedupe_key,
                    decision="pending_approval",
                    reason="this notification kind requires explicit approval",
                    moment=moment,
                )

            recent = int(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM notifications "
                    "WHERE channel = ? AND decision = 'sent' AND created_at >= ?",
                    (channel, moment - 3_600),
                ).fetchone()["total"]
                or 0
            )
            if recent >= self.policy.hourly_limit and urgency != "critical":
                return self._record(
                    connection,
                    notification_id=notification_id,
                    request=request,
                    kind=kind,
                    summary=summary,
                    channel=channel,
                    urgency=urgency,
                    dedupe_key=dedupe_key,
                    decision="suppressed",
                    reason=f"hourly limit of {self.policy.hourly_limit} reached on {channel}",
                    moment=moment,
                )

            if urgency != "critical" and self.policy.in_quiet_hours(moment):
                return self._record(
                    connection,
                    notification_id=notification_id,
                    request=request,
                    kind=kind,
                    summary=summary,
                    channel=channel,
                    urgency=urgency,
                    dedupe_key=dedupe_key,
                    decision="deferred",
                    reason="quiet hours",
                    moment=moment,
                    deliver_after=self.policy.next_quiet_end(moment),
                )

            self._record(
                connection,
                notification_id=notification_id,
                request=request,
                kind=kind,
                summary=summary,
                channel=channel,
                urgency=urgency,
                dedupe_key=dedupe_key,
                decision="deferred",
                reason="queued for delivery",
                moment=moment,
                deliver_after=moment,
            )

        return self._attempt_delivery(notification_id, request, moment=moment)

    def approve(self, notification_id: str, *, now: float | None = None) -> NotificationResult:
        """Release one notice that was held for approval."""

        moment = float(time.time() if now is None else now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM notifications WHERE notification_id = ?", (notification_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown notification {notification_id}")
            if str(row["decision"]) != "pending_approval":
                return _result_from_row(row)
            connection.execute(
                "UPDATE notifications SET decision = 'deferred', reason = 'approved', "
                "approved_at = ?, deliver_after = ?, updated_at = ? WHERE notification_id = ?",
                (moment, moment, moment, notification_id),
            )
            request = _request_from_row(row)
        return self._attempt_delivery(notification_id, request, moment=moment)

    def deliver_due(self, *, now: float | None = None, limit: int = 20) -> list[NotificationResult]:
        """Deliver notices whose quiet-hours or retry deadline has passed."""

        moment = float(time.time() if now is None else now)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM notifications
                WHERE decision IN ('deferred', 'failed')
                  AND deliver_after IS NOT NULL AND deliver_after <= ?
                  AND attempts < ?
                ORDER BY created_at LIMIT ?
                """,
                (moment, self.policy.max_attempts, max(1, int(limit))),
            ).fetchall()
        results: list[NotificationResult] = []
        for row in rows:
            results.append(
                self._attempt_delivery(
                    str(row["notification_id"]), _request_from_row(row), moment=moment
                )
            )
        return results

    def redeliver(self, notification_id: str, *, now: float | None = None) -> NotificationResult | None:
        """Retry one durable notice when its native policy says it is due."""

        moment = float(time.time() if now is None else now)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM notifications WHERE notification_id = ?", (notification_id,)
            ).fetchone()
        if row is None:
            return None
        if str(row["decision"]) not in {"deferred", "failed"}:
            return None
        if int(row["attempts"] or 0) >= self.policy.max_attempts:
            return None
        deliver_after = row["deliver_after"]
        if deliver_after is None or float(deliver_after) > moment:
            return None
        return self._attempt_delivery(notification_id, _request_from_row(row), moment=moment)

    def flush_control_outbox(self) -> int:
        return self._outbox.flush(self._control_store)

    def pending_control_events(self) -> int:
        return self._outbox.pending()

    def history(
        self,
        *,
        channel: str | None = None,
        decision: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[object] = []
        if channel is not None:
            clauses.append("channel = ?")
            params.append(channel)
        if decision is not None:
            if decision not in DECISIONS:
                raise NotificationError(f"unknown decision {decision!r}")
            clauses.append("decision = ?")
            params.append(decision)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(min(500, max(1, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM notifications" + where
                + " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_approvals(self) -> list[dict[str, Any]]:
        return self.history(decision="pending_approval", limit=100)

    # -- internals ----------------------------------------------------------

    def _attempt_delivery(
        self,
        notification_id: str,
        request: NotificationRequest,
        *,
        moment: float,
    ) -> NotificationResult:
        sender = self._senders.get(request.channel)
        if sender is None:
            return self._finish(
                notification_id,
                decision="failed",
                reason=f"no sender is registered for {request.channel}",
                moment=moment,
                retryable=False,
            )
        try:
            delivered = bool(sender(request))
            error = "" if delivered else "the channel reported the notice was not delivered"
        except Exception as exc:
            delivered = False
            error = _clean(exc, 500) or "the channel raised while delivering"
        if delivered:
            return self._finish(
                notification_id, decision="sent", reason="delivered", moment=moment
            )
        return self._finish(
            notification_id,
            decision="failed",
            reason=error,
            moment=moment,
            retryable=True,
        )

    def _finish(
        self,
        notification_id: str,
        *,
        decision: str,
        reason: str,
        moment: float,
        retryable: bool = False,
    ) -> NotificationResult:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM notifications WHERE notification_id = ?", (notification_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown notification {notification_id}")
            attempts = int(row["attempts"] or 0) + 1
            deliver_after: float | None = None
            if decision == "failed" and retryable and attempts < self.policy.max_attempts:
                # Bounded exponential backoff. Retrying forever is how a broken
                # channel turns into a pager.
                deliver_after = moment + min(3_600, 30 * (2 ** (attempts - 1)))
            connection.execute(
                "UPDATE notifications SET decision = ?, reason = ?, attempts = ?, "
                "deliver_after = ?, delivered_at = ?, updated_at = ? WHERE notification_id = ?",
                (
                    decision,
                    _clean(reason, 1_000),
                    attempts,
                    deliver_after,
                    moment if decision == "sent" else None,
                    moment,
                    notification_id,
                ),
            )
            result = NotificationResult(
                notification_id=notification_id,
                decision=decision,
                reason=_clean(reason, 1_000),
                channel=str(row["channel"]),
                attempts=attempts,
                deliver_after=deliver_after,
            )
            event_type = "notice.delivered" if decision == "sent" else "notice.failed"
            self._outbox.stage_event(
                connection,
                event_type=event_type,
                lifecycle_state="delivered" if decision == "sent" else "failed",
                delivery_state="delivered" if decision == "sent" else "failed",
                event_id=f"notification:{notification_id}:attempt:{attempts}:{decision}",
                job_id=notification_id,
                session_id=row["session_id"],
                authority="notification_authority",
                payload={
                    "kind": str(row["kind"]),
                    "channel": str(row["channel"]),
                    "reason": result.reason,
                    "error": result.reason if decision == "failed" else "",
                    "attempts": attempts,
                    "source_job_id": row["job_id"],
                },
                occurred_at=moment,
            )
        self.flush_control_outbox()
        if decision == "sent":
            with suppress(Exception):
                from core.plugin_loader import emit_plugin_hook

                emit_plugin_hook(
                    "notification.sent",
                    {
                        "notification_id": notification_id,
                        "kind": str(row["kind"]),
                        "channel": str(row["channel"]),
                        "source_surface": str(row["source_surface"]),
                        "job_id": str(row["job_id"] or ""),
                    },
                )
        if self._result_observer is not None:
            with suppress(Exception):
                self._result_observer(_request_from_row(row), result)
        return result

    def _record(
        self,
        connection: sqlite3.Connection,
        *,
        notification_id: str,
        request: NotificationRequest,
        kind: str,
        summary: str,
        channel: str,
        urgency: str,
        dedupe_key: str,
        decision: str,
        reason: str,
        moment: float,
        deliver_after: float | None = None,
    ) -> NotificationResult:
        connection.execute(
            """
            INSERT INTO notifications(
                notification_id, kind, summary, channel, urgency, dedupe_key,
                source_surface, job_id, session_id, metadata_json, decision, reason,
                attempts, deliver_after, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                notification_id,
                kind,
                summary,
                channel,
                urgency,
                dedupe_key,
                _clean(request.source_surface, 32) or "system",
                request.job_id,
                request.session_id,
                json.dumps(request.metadata or {}, default=str)[:8_000],
                decision,
                _clean(reason, 1_000),
                deliver_after,
                moment,
                moment,
            ),
        )
        result = NotificationResult(
            notification_id=notification_id,
            decision=decision,
            reason=_clean(reason, 1_000),
            channel=channel,
            deliver_after=deliver_after,
        )
        if decision in {"deferred", "pending_approval"}:
            self._outbox.stage_event(
                connection,
                event_type="notice.queued",
                lifecycle_state=decision,
                delivery_state="pending",
                event_id=f"notification:{notification_id}:queued",
                job_id=notification_id,
                session_id=request.session_id,
                authority="notification_authority",
                payload={
                    "summary": summary,
                    "kind": kind,
                    "channel": channel,
                    "reason": result.reason,
                    "source_surface": request.source_surface,
                    "source_job_id": request.job_id,
                },
                occurred_at=moment,
            )
        connection.commit()
        self.flush_control_outbox()
        return result

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
                CREATE TABLE IF NOT EXISTS notifications (
                    notification_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    urgency TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    source_surface TEXT NOT NULL,
                    job_id TEXT,
                    session_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    deliver_after REAL,
                    delivered_at REAL,
                    approved_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS notifications_dedupe_idx
                    ON notifications(dedupe_key, created_at);
                CREATE INDEX IF NOT EXISTS notifications_due_idx
                    ON notifications(decision, deliver_after);
                CREATE INDEX IF NOT EXISTS notifications_channel_idx
                    ON notifications(channel, created_at);
                """
            )
        if os.name != "nt":
            with suppress(OSError):
                self.path.chmod(0o600)


def _request_from_row(row: sqlite3.Row) -> NotificationRequest:
    try:
        metadata = json.loads(str(row["metadata_json"] or "{}"))
    except json.JSONDecodeError:
        metadata = {}
    return NotificationRequest(
        kind=str(row["kind"]),
        summary=str(row["summary"]),
        channel=str(row["channel"]),
        urgency=str(row["urgency"]),
        dedupe_key=str(row["dedupe_key"]),
        source_surface=str(row["source_surface"]),
        job_id=row["job_id"],
        session_id=row["session_id"],
        metadata=metadata if isinstance(metadata, dict) else {},
    )


def _result_from_row(row: sqlite3.Row) -> NotificationResult:
    return NotificationResult(
        notification_id=str(row["notification_id"]),
        decision=str(row["decision"]),
        reason=str(row["reason"] or ""),
        channel=str(row["channel"]),
        attempts=int(row["attempts"] or 0),
        deliver_after=row["deliver_after"],
    )
