"""The authenticated front door for things outside Serena that want in.

`core.webhook_signing` proved a request came from something holding the shared
secret. That is necessary and nowhere near sufficient: a verified request is
still an instruction from outside Serena's authority boundary, and the whole
point of this layer is that a valid signature buys you *one specific reviewed
route*, not general reach.

The refusals are the feature:

- No secret configured means every request is rejected. There is no
  develop-mode bypass and no default secret.
- A route must be registered in code, by name, with a handler that has a test.
  An unknown route is refused before the body is even parsed as an instruction.
- A valid signature is consumed exactly once. A replayed capture is rejected
  even inside the freshness window.
- A route may be marked as requiring approval, in which case a perfectly valid
  request is recorded and held, and nothing happens until Raghav releases it.
- Everything, accepted or refused, lands in a durable audit table with the
  reason. A rejected request is evidence, not a dropped packet.

Nothing here opens a socket. The caller owns the HTTP server and hands the
body, headers, and path in, which keeps this testable without standing up a
web server and keeps the mounting decision out of this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.webhook_signing import (
    DEFAULT_TOLERANCE_SECONDS,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WebhookReplayStore,
    verify_headers,
)

DEFAULT_DB_PATH = Path.home() / ".local" / "state" / "serena" / "webhook-ingress.sqlite3"
MAX_BODY_BYTES = 256 * 1024
MAX_DETAIL_CHARS = 1_000

DECISIONS = ("accepted", "held", "rejected")

RouteHandler = Callable[[dict[str, Any], "WebhookRequest"], "RouteOutcome"]


class WebhookIngressError(ValueError):
    """The ingress was configured or called incorrectly."""


@dataclass(frozen=True, slots=True)
class WebhookRequest:
    route: str
    body: bytes
    headers: dict[str, str]
    received_at: float


@dataclass(frozen=True, slots=True)
class RouteOutcome:
    ok: bool
    detail: str = ""
    notify: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Route:
    name: str
    handler: RouteHandler
    requires_approval: bool = False
    description: str = ""


@dataclass(frozen=True, slots=True)
class IngressResult:
    delivery_id: str
    decision: str
    reason: str
    route: str = ""
    status: int = 200
    detail: str = ""

    @property
    def accepted(self) -> bool:
        return self.decision == "accepted"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean(value: object, limit: int = MAX_DETAIL_CHARS) -> str:
    return " ".join(str(value or "").split())[:limit]


# A held delivery keeps its routing headers so approval can replay it faithfully,
# but never the ones that authenticate it. The signature was already verified and
# consumed exactly once; keeping it at rest would be storing a credential to buy
# nothing, since it can never be replayed again anyway.
UNREPLAYABLE_HEADERS = frozenset(
    {
        SIGNATURE_HEADER.lower(),
        TIMESTAMP_HEADER.lower(),
        "authorization",
        "cookie",
        "proxy-authorization",
        "set-cookie",
        "x-api-key",
    }
)


def _replayable_headers(headers: dict[str, str] | None) -> dict[str, str]:
    return {
        str(name).lower(): str(value)[:1_000]
        for name, value in (headers or {}).items()
        if str(name).lower() not in UNREPLAYABLE_HEADERS
    }


def configured_secret() -> str:
    """The shared secret, or an empty string when none is configured."""

    raw = os.environ.get("SERENA_WEBHOOK_SECRET", "").strip()
    if raw:
        return raw
    path = os.environ.get("SERENA_WEBHOOK_SECRET_FILE", "").strip()
    if path:
        with suppress(OSError):
            return Path(path).expanduser().read_text(encoding="utf-8").strip()
    return ""


class WebhookIngress:
    """Verify, route, and durably record one inbound request at a time."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        secret: str | None = None,
        routes: dict[str, Route] | None = None,
        replay_store: WebhookReplayStore | None = None,
        tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
        authority: Any | None = None,
    ) -> None:
        configured = os.environ.get("SERENA_WEBHOOK_DB_PATH", "").strip()
        self.path = Path(path or configured or DEFAULT_DB_PATH).expanduser()
        self._secret = secret
        self._routes: dict[str, Route] = dict(routes or {})
        self._replay_store = replay_store
        self.tolerance_seconds = int(tolerance_seconds)
        self._authority = authority
        self._initialize()

    # -- registration -------------------------------------------------------

    def register(
        self,
        name: str,
        handler: RouteHandler,
        *,
        requires_approval: bool = False,
        description: str = "",
    ) -> None:
        """Add one reviewed route. This is the only way a route can exist."""

        route = _clean(name, 64)
        if not route:
            raise WebhookIngressError("a webhook route needs a name")
        if not callable(handler):
            raise WebhookIngressError("a webhook route needs a callable handler")
        self._routes[route] = Route(
            name=route,
            handler=handler,
            requires_approval=bool(requires_approval),
            description=_clean(description, 200),
        )

    @property
    def routes(self) -> tuple[str, ...]:
        return tuple(sorted(self._routes))

    def secret(self) -> str:
        return self._secret if self._secret is not None else configured_secret()

    # -- the front door -----------------------------------------------------

    def handle(
        self,
        route: str,
        body: object,
        headers: dict[str, Any] | None = None,
        *,
        now: float | None = None,
    ) -> IngressResult:
        """Decide what happens to one inbound request, and record it."""

        moment = float(time.time() if now is None else now)
        delivery_id = str(uuid.uuid4())
        name = _clean(route, 64)
        raw = body if isinstance(body, bytes) else str(body or "").encode("utf-8")
        lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}

        if len(raw) > MAX_BODY_BYTES:
            return self._record(
                delivery_id, name, raw, moment,
                decision="rejected", reason="body is too large", status=413,
            )

        secret = self.secret()
        if not secret:
            # Fail closed. An ingress with no secret is not an open ingress.
            return self._record(
                delivery_id, name, raw, moment,
                decision="rejected",
                reason="no webhook secret is configured; the ingress is closed",
                status=503,
            )

        registered = self._routes.get(name)
        if registered is None:
            return self._record(
                delivery_id, name, raw, moment,
                decision="rejected",
                reason=f"unknown webhook route {name!r}",
                status=404,
            )

        verified, why = verify_headers(
            raw,
            secret,
            lowered,
            tolerance_seconds=self.tolerance_seconds,
            now=moment,
            replay_store=self._replay_store or WebhookReplayStore(),
        )
        if not verified:
            return self._record(
                delivery_id, name, raw, moment,
                decision="rejected", reason=why, status=401,
            )

        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return self._record(
                delivery_id, name, raw, moment,
                decision="rejected",
                reason=f"body is not valid JSON: {error}",
                status=400,
            )
        if not isinstance(parsed, dict):
            return self._record(
                delivery_id, name, raw, moment,
                decision="rejected", reason="body must be a JSON object", status=400,
            )

        if registered.requires_approval:
            return self._record(
                delivery_id, name, raw, moment,
                decision="held",
                reason="this route requires Raghav's approval before it runs",
                status=202,
                payload=parsed,
                headers=lowered,
            )

        return self._dispatch(
            delivery_id, registered, parsed, raw, lowered, moment
        )

    def approve(
        self, delivery_id: str, *, actor: str, now: float | None = None
    ) -> IngressResult:
        """Release one held delivery. Its signature was already verified."""

        moment = float(time.time() if now is None else now)
        named = _clean(actor, 120)
        if not named:
            raise WebhookIngressError("approving a webhook delivery requires an actor")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM webhook_deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown webhook delivery {delivery_id}")
        if str(row["decision"]) != "held":
            return _result_from_row(row)
        registered = self._routes.get(str(row["route"]))
        if registered is None:
            return self._finish(
                delivery_id,
                decision="rejected",
                reason="the route no longer exists",
                status=404,
                moment=moment,
            )
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except json.JSONDecodeError:
            payload = {}
        # Replay what actually arrived. Dispatching an approved delivery with an
        # empty body would hand the handler a different request from the one
        # Raghav looked at and approved.
        raw = bytes(row["body_raw"] or b"")
        try:
            stored_headers = json.loads(str(row["headers_json"] or "{}"))
        except json.JSONDecodeError:
            stored_headers = {}
        headers = (
            {str(name): str(value) for name, value in stored_headers.items()}
            if isinstance(stored_headers, dict)
            else {}
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE webhook_deliveries SET approved_by = ?, approved_at = ? "
                "WHERE delivery_id = ?",
                (named, moment, delivery_id),
            )
        return self._dispatch(
            delivery_id,
            registered,
            payload if isinstance(payload, dict) else {},
            raw,
            headers,
            moment,
            already_recorded=True,
        )

    def pending(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.history(decision="held", limit=limit)

    def history(
        self,
        *,
        route: str | None = None,
        decision: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[object] = []
        if route is not None:
            clauses.append("route = ?")
            params.append(_clean(route, 64))
        if decision is not None:
            if decision not in DECISIONS:
                raise WebhookIngressError(f"unknown decision {decision!r}")
            clauses.append("decision = ?")
            params.append(decision)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(min(500, max(1, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM webhook_deliveries" + where
                + " ORDER BY received_at DESC, rowid DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- internals ----------------------------------------------------------

    def _dispatch(
        self,
        delivery_id: str,
        route: Route,
        payload: dict[str, Any],
        raw: bytes,
        headers: dict[str, str],
        moment: float,
        *,
        already_recorded: bool = False,
    ) -> IngressResult:
        request = WebhookRequest(
            route=route.name, body=raw, headers=headers, received_at=moment
        )
        try:
            outcome = route.handler(payload, request)
            if not isinstance(outcome, RouteOutcome):
                outcome = RouteOutcome(bool(outcome), "handler returned a non-outcome value")
        except Exception as error:
            outcome = RouteOutcome(False, f"{type(error).__name__}: {error}")

        decision = "accepted" if outcome.ok else "rejected"
        status = 200 if outcome.ok else 500
        if outcome.ok and outcome.notify:
            self._notify(route.name, outcome.notify)
        if already_recorded:
            return self._finish(
                delivery_id,
                decision=decision,
                reason=_clean(outcome.detail) or decision,
                status=status,
                moment=moment,
            )
        return self._record(
            delivery_id, route.name, raw, moment,
            decision=decision,
            reason=_clean(outcome.detail) or decision,
            status=status,
            payload=payload,
        )

    def _notify(self, route: str, notify: dict[str, Any]) -> None:
        """A webhook may ask to tell Raghav, through the one authority."""

        with suppress(Exception):
            authority = self._authority
            if authority is None:
                from core.notification_senders import default_authority

                authority = default_authority()
            from core.notification_authority import NotificationRequest

            authority.request(
                NotificationRequest(
                    kind=str(notify.get("kind") or f"webhook.{route}"),
                    summary=str(notify.get("summary") or ""),
                    channel=str(notify.get("channel") or "voice"),
                    urgency=str(notify.get("urgency") or "normal"),
                    dedupe_key=str(notify.get("dedupe_key") or ""),
                    source_surface="system",
                )
            )

    def _record(
        self,
        delivery_id: str,
        route: str,
        raw: bytes,
        moment: float,
        *,
        decision: str,
        reason: str,
        status: int,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> IngressResult:
        digest = hashlib.sha256(raw).hexdigest() if raw else ""
        held = decision == "held"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO webhook_deliveries(
                    delivery_id, route, decision, reason, status, body_sha256,
                    body_bytes, payload_json, body_raw, headers_json,
                    received_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    route,
                    decision,
                    _clean(reason),
                    int(status),
                    digest,
                    len(raw),
                    # Only a held delivery keeps its body, because it has to be
                    # replayable on approval. Everything else keeps the digest
                    # only, so the audit trail is not a copy of every payload
                    # anyone ever posted at Serena.
                    json.dumps(payload or {}, default=str)[:16_000] if held else "{}",
                    # The exact bytes and routing headers, for the same reason: a
                    # handler that reads a header or re-hashes the body must see
                    # what actually arrived, not a re-encoding of it.
                    (raw if held else None),
                    json.dumps(_replayable_headers(headers), default=str)[:8_000]
                    if held
                    else "{}",
                    moment,
                    moment,
                ),
            )
        return IngressResult(
            delivery_id=delivery_id,
            decision=decision,
            reason=_clean(reason),
            route=route,
            status=int(status),
        )

    def _finish(
        self,
        delivery_id: str,
        *,
        decision: str,
        reason: str,
        status: int,
        moment: float,
    ) -> IngressResult:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                # A resolved delivery no longer needs to be replayable, so the
                # retained body and headers are dropped with the payload. The
                # audit row and its digest stay.
                "UPDATE webhook_deliveries SET decision = ?, reason = ?, status = ?, "
                "payload_json = '{}', body_raw = NULL, headers_json = '{}', "
                "updated_at = ? WHERE delivery_id = ?",
                (decision, _clean(reason), int(status), moment, delivery_id),
            )
            row = connection.execute(
                "SELECT * FROM webhook_deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
        return _result_from_row(row)

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
                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    route TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    status INTEGER NOT NULL DEFAULT 200,
                    body_sha256 TEXT NOT NULL DEFAULT '',
                    body_bytes INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    approved_by TEXT,
                    approved_at REAL,
                    received_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS webhook_deliveries_route_idx
                    ON webhook_deliveries(route, received_at);
                CREATE INDEX IF NOT EXISTS webhook_deliveries_decision_idx
                    ON webhook_deliveries(decision, received_at);
                """
            )
            # Added after the first release: a held delivery has to be replayable
            # exactly as it arrived, so it keeps its original bytes and routing
            # headers. Widened in place so an existing audit trail survives.
            existing = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(webhook_deliveries)")
            }
            if "body_raw" not in existing:
                connection.execute("ALTER TABLE webhook_deliveries ADD COLUMN body_raw BLOB")
            if "headers_json" not in existing:
                connection.execute(
                    "ALTER TABLE webhook_deliveries ADD COLUMN headers_json TEXT "
                    "NOT NULL DEFAULT '{}'"
                )
        if os.name != "nt":
            with suppress(OSError):
                self.path.chmod(0o600)


def _result_from_row(row: sqlite3.Row) -> IngressResult:
    return IngressResult(
        delivery_id=str(row["delivery_id"]),
        decision=str(row["decision"]),
        reason=str(row["reason"] or ""),
        route=str(row["route"]),
        status=int(row["status"] or 200),
    )


# ---- the reviewed routes --------------------------------------------------


def route_ping(payload: dict[str, Any], request: WebhookRequest) -> RouteOutcome:
    """A liveness route that proves signing works end to end and nothing else."""

    return RouteOutcome(True, "pong")


def route_notify(payload: dict[str, Any], request: WebhookRequest) -> RouteOutcome:
    """Let a trusted external system ask Serena to tell Raghav something.

    It asks; it does not decide. The summary still goes through the
    notification authority, so quiet hours, the hourly limit, and the dedupe
    window all apply exactly as they would to anything Serena raised herself.
    """

    summary = _clean(payload.get("summary") or payload.get("text"), 2_000)
    if not summary:
        return RouteOutcome(False, "a notify webhook needs a summary")
    return RouteOutcome(
        True,
        "queued through the notification authority",
        notify={
            "kind": _clean(payload.get("kind"), 128) or "webhook.notify",
            "summary": summary,
            "channel": _clean(payload.get("channel"), 32) or "voice",
            # An outside caller may not declare its own message critical. That
            # is the one flag that skips quiet hours, so it stays Serena's.
            "urgency": "normal",
            "dedupe_key": _clean(payload.get("dedupe_key"), 256),
        },
    )


def default_ingress(**kwargs: Any) -> WebhookIngress:
    """An ingress with the reviewed routes already mounted."""

    ingress = WebhookIngress(**kwargs)
    ingress.register("ping", route_ping, description="liveness check")
    ingress.register(
        "notify",
        route_notify,
        # Something outside Serena getting to interrupt Raghav is exactly the
        # kind of reach that should wait for him to say yes.
        requires_approval=True,
        description="ask Serena to pass a message along",
    )
    return ingress


__all__ = [
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "IngressResult",
    "Route",
    "RouteOutcome",
    "WebhookIngress",
    "WebhookIngressError",
    "WebhookRequest",
    "configured_secret",
    "default_ingress",
]
