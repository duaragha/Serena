"""Signed webhook foundation for Serena.

Deliberately small. Serena does not need a webhook framework, she needs to be
able to prove that an inbound request really came from something she trusts,
and to sign her own outbound deliveries the same way. Both directions use one
HMAC-SHA256 scheme over an explicit signing string, with a replay window.

No new dependency: `hmac` and `hashlib` are stdlib. Nothing here imports a
third-party HTTP client, and nothing here sends anything. Delivery is the
notification authority's job; this module only signs and verifies.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SIGNATURE_VERSION = "v1"
SIGNATURE_HEADER = "X-Serena-Signature"
TIMESTAMP_HEADER = "X-Serena-Timestamp"
DEFAULT_TOLERANCE_SECONDS = 300
MAX_BODY_BYTES = 1 * 1024 * 1024
MIN_SECRET_CHARS = 16
DEFAULT_REPLAY_DB_PATH = (
    Path.home() / ".local" / "state" / "serena" / "webhook-replays.sqlite3"
)

_SIGNATURE = re.compile(r"^(?P<version>v[0-9]+)=(?P<digest>[0-9a-f]{64})$")


class WebhookSignatureError(ValueError):
    """The payload could not be signed or verified safely."""


@dataclass(frozen=True, slots=True)
class SignedPayload:
    timestamp: int
    signature: str

    def headers(self) -> dict[str, str]:
        return {SIGNATURE_HEADER: self.signature, TIMESTAMP_HEADER: str(self.timestamp)}


class WebhookReplayStore:
    """Durably consume valid signatures once at the HTTP receiver boundary."""

    def __init__(self, path: Path | None = None) -> None:
        configured = os.environ.get("SERENA_WEBHOOK_REPLAY_DB_PATH", "").strip()
        self.path = Path(path or configured or DEFAULT_REPLAY_DB_PATH).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS webhook_replays ("
                "replay_key TEXT PRIMARY KEY, accepted_at REAL NOT NULL, "
                "expires_at REAL NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS webhook_replays_expiry_idx "
                "ON webhook_replays(expires_at)"
            )
        if os.name != "nt":
            with suppress(OSError):
                self.path.chmod(0o600)

    def consume(
        self,
        *,
        signature: str,
        timestamp: int,
        now: float,
        tolerance_seconds: int,
    ) -> bool:
        replay_key = hashlib.sha256(
            f"{timestamp}:{signature}".encode("ascii", errors="strict")
        ).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM webhook_replays WHERE expires_at < ?", (now,))
            try:
                connection.execute(
                    "INSERT INTO webhook_replays(replay_key, accepted_at, expires_at) "
                    "VALUES (?, ?, ?)",
                    (replay_key, now, now + max(1, int(tolerance_seconds))),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection


def _as_bytes(body: object) -> bytes:
    if isinstance(body, bytes):
        payload = body
    elif isinstance(body, str):
        payload = body.encode("utf-8")
    else:
        raise WebhookSignatureError("webhook body must be bytes or str")
    if len(payload) > MAX_BODY_BYTES:
        raise WebhookSignatureError("webhook body is too large to sign")
    return payload


def _secret_bytes(secret: object) -> bytes:
    raw = secret if isinstance(secret, bytes) else str(secret or "").encode("utf-8")
    if len(raw) < MIN_SECRET_CHARS:
        # A short shared secret is the usual way this kind of scheme quietly
        # becomes decorative, so it is refused rather than accepted.
        raise WebhookSignatureError("webhook signing secret is too short")
    return raw


def signing_string(timestamp: int, body: bytes) -> bytes:
    """The exact bytes that get signed.

    The timestamp is inside the signature, so an attacker cannot replay a
    captured body under a fresh timestamp to slip past the freshness window.
    """

    return f"{SIGNATURE_VERSION}:{int(timestamp)}:".encode("ascii") + body


def sign(body: object, secret: object, *, timestamp: int | None = None) -> SignedPayload:
    """Sign one webhook body and return the headers to send with it."""

    payload = _as_bytes(body)
    key = _secret_bytes(secret)
    moment = int(time.time() if timestamp is None else timestamp)
    digest = hmac.new(key, signing_string(moment, payload), hashlib.sha256).hexdigest()
    return SignedPayload(timestamp=moment, signature=f"{SIGNATURE_VERSION}={digest}")


def verify(
    body: object,
    secret: object,
    *,
    signature: str,
    timestamp: object,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> tuple[bool, str]:
    """Verify an inbound webhook. Returns (ok, reason).

    Fails closed on every ambiguity: unparseable signature, wrong version,
    non-numeric timestamp, stale or future-dated request, or digest mismatch.
    """

    try:
        payload = _as_bytes(body)
        key = _secret_bytes(secret)
    except WebhookSignatureError as exc:
        return False, str(exc)

    match = _SIGNATURE.match(str(signature or "").strip())
    if not match:
        return False, "signature header is missing or malformed"
    if match.group("version") != SIGNATURE_VERSION:
        return False, "unsupported signature version"

    try:
        moment = int(str(timestamp).strip())
    except (TypeError, ValueError):
        return False, "timestamp header is missing or not an integer"

    current = float(time.time() if now is None else now)
    tolerance = max(1, int(tolerance_seconds))
    drift = current - moment
    if drift > tolerance:
        return False, "signature timestamp is outside the replay window"
    if drift < -tolerance:
        return False, "signature timestamp is in the future"

    expected = hmac.new(key, signing_string(moment, payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, match.group("digest")):
        return False, "signature did not match"
    return True, "verified"


def verify_headers(
    body: object,
    secret: object,
    headers: dict[str, Any],
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
    replay_store: WebhookReplayStore | None = None,
) -> tuple[bool, str]:
    """Verify and durably consume a request exactly once at the receiver boundary."""

    lowered = {str(key).lower(): value for key, value in (headers or {}).items()}
    signature = str(lowered.get(SIGNATURE_HEADER.lower()) or "")
    timestamp = lowered.get(TIMESTAMP_HEADER.lower())
    accepted, reason = verify(
        body,
        secret,
        signature=signature,
        timestamp=timestamp,
        tolerance_seconds=tolerance_seconds,
        now=now,
    )
    if not accepted:
        return accepted, reason
    moment = float(time.time() if now is None else now)
    try:
        parsed_timestamp = int(str(timestamp).strip())
        store = replay_store or WebhookReplayStore()
        if not store.consume(
            signature=signature,
            timestamp=parsed_timestamp,
            now=moment,
            tolerance_seconds=tolerance_seconds,
        ):
            return False, "webhook signature was already consumed"
    except Exception as exc:
        return False, f"webhook replay protection unavailable: {exc}"
    return True, "verified"
