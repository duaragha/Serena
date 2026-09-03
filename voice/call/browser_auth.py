"""Short-lived, one-use browser authorization for call WebSockets.

Browsers cannot attach an Authorization header to a WebSocket handshake. A
normal authenticated POST mints an opaque, HttpOnly cookie which is consumed
by the next same-origin call socket. The bearer token never enters a URL or a
cookie, and native clients keep using the Authorization header directly.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass

CALL_SOCKET_COOKIE = "serena_call_socket"
CALL_SOCKET_COOKIE_PATH = "/ws/call"
CALL_SOCKET_TICKET_TTL_SECONDS = 45
_MAX_OUTSTANDING_TICKETS = 64


@dataclass(frozen=True, slots=True)
class _Ticket:
    digest: bytes
    expires_at: float


class BrowserCallTickets:
    """Bounded in-memory store for one-use WebSocket authorization tickets."""

    def __init__(
        self,
        *,
        ttl_seconds: int = CALL_SOCKET_TICKET_TTL_SECONDS,
        max_outstanding: int = _MAX_OUTSTANDING_TICKETS,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("ticket ttl must be positive")
        if max_outstanding < 1:
            raise ValueError("ticket capacity must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_outstanding = max_outstanding
        self._tickets: dict[str, _Ticket] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _digest(ticket: str) -> bytes:
        return hashlib.sha256(ticket.encode("ascii")).digest()

    def issue(self, *, now: float | None = None) -> str:
        issued_at = time.monotonic() if now is None else now
        ticket = secrets.token_urlsafe(32)
        key = ticket[:16]
        record = _Ticket(
            digest=self._digest(ticket),
            expires_at=issued_at + self._ttl_seconds,
        )
        with self._lock:
            self._expire_locked(issued_at)
            if len(self._tickets) >= self._max_outstanding:
                oldest = min(
                    self._tickets,
                    key=lambda item: self._tickets[item].expires_at,
                )
                self._tickets.pop(oldest, None)
            self._tickets[key] = record
        return ticket

    def consume(self, ticket: str, *, now: float | None = None) -> bool:
        if not ticket or len(ticket) > 256:
            return False
        consumed_at = time.monotonic() if now is None else now
        key = ticket[:16]
        with self._lock:
            self._expire_locked(consumed_at)
            record = self._tickets.pop(key, None)
        if record is None or record.expires_at < consumed_at:
            return False
        return hmac.compare_digest(record.digest, self._digest(ticket))

    def _expire_locked(self, now: float) -> None:
        expired = [
            key for key, record in self._tickets.items() if record.expires_at < now
        ]
        for key in expired:
            self._tickets.pop(key, None)


browser_call_tickets = BrowserCallTickets()
