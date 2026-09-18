"""Route pending confirmations to Raghav, wherever he is.

`core.action_authority` owns the tier machine and the audit chain, but nobody
listens to it: a tier-3 action can only be approved from an interactive
terminal. The broker scans `pending_confirmations()`, mints one single-use
4-digit nonce per confirmation, and pushes it on exactly one channel — voice
when he is in a call, otherwise iMessage. The web UI lists everything pending.

Nonces, not "reply yes": a bare yes on a channel that also carries normal
conversation is an accident waiting to happen. A code bound to one
confirmation is unambiguous, single-use and expiring.

Tier 4 (secret) stays typed: voice and text can never resolve it, only chat,
ui or cli. The broker refuses such attempts before they reach the authority,
and the authority enforces the same rule again at resolution.
"""

from __future__ import annotations

import hmac
import os
import secrets
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from core.action_authority import (
    DEFAULT_CONFIRMATION_SECONDS,
    TIER_SECRET,
    TYPED_SOURCES,
    Confirmation,
)

DEFAULT_DB_PATH = Path.home() / ".local" / "state" / "serena" / "approvals.sqlite3"
DEFAULT_CALL_MARKER_PATH = (
    Path.home() / ".local" / "state" / "serena" / "call-active.json")
# A call marker older than this is a crashed call, not a live one.
CALL_MARKER_FRESH_SECONDS = 120.0
NONCE_DIGITS = 4
NONCE_TTL_SECONDS = DEFAULT_CONFIRMATION_SECONDS
MAX_MINT_ATTEMPTS = 50
KIND = "approval.request"

SURFACES = ("voice", "text", "chat", "ui", "cli")


class ApprovalError(ValueError):
    """The approval reply was wrong, stale, reused, or not allowed."""


def call_marker_path() -> Path:
    configured = os.environ.get("SERENA_CALL_ACTIVE_PATH", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_CALL_MARKER_PATH


def call_active(*, now: float | None = None) -> bool:
    """True when a call session recently marked itself live."""

    import json

    moment = time.time() if now is None else float(now)
    try:
        state = json.loads(call_marker_path().read_text(encoding="utf-8"))
        updated = float(state.get("updated_at") or 0)
    except (OSError, ValueError, TypeError):
        return False
    return 0 <= moment - updated < CALL_MARKER_FRESH_SECONDS


def mark_call_active(call_id: str, *, now: float | None = None) -> None:
    """Note that a call is live. Presence must never break a call."""

    import json
    from contextlib import suppress

    moment = time.time() if now is None else float(now)
    path = call_marker_path()
    with suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"call_id": str(call_id or ""),
                                    "updated_at": moment}),
                        encoding="utf-8")


def clear_call_active(call_id: str = "") -> None:
    """Note that a call ended. Only clears its own marker."""

    import json
    from contextlib import suppress

    path = call_marker_path()
    with suppress(OSError):
        if not call_id:
            path.unlink(missing_ok=True)
            return
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if str(state.get("call_id") or "") == str(call_id):
            with suppress(OSError):
                path.unlink()


def choose_channel(confirmation: Confirmation, *, in_call: bool) -> str:
    """Exactly one push channel per confirmation: voice in a call, else text."""

    _ = confirmation
    return "voice" if in_call else "imessage"


_SPOKEN_APPROVAL = None


def match_spoken_approval(text: str) -> tuple[bool, str] | None:
    """(approved, nonce) for "approve 4821" / "no 4821", else None.

    Anchored and strict: anything ambiguous is conversation, never consent.
    """

    import re

    global _SPOKEN_APPROVAL
    if _SPOKEN_APPROVAL is None:
        _SPOKEN_APPROVAL = re.compile(
            r"^\s*(?P<verb>approve|yes|deny|no)\s+"
            r"(?P<nonce>\d{4})\s*[.!]?\s*$", re.IGNORECASE)
    match = _SPOKEN_APPROVAL.match(text or "")
    if match is None:
        return None
    return (match.group("verb").lower() in ("approve", "yes"),
            match.group("nonce"))


def interpret_spoken_reply(text: str, *, broker: ApprovalBroker | None = None,
                           now: float | None = None) -> str | None:
    """Resolve a spoken approval, returning the exact sentence to say back.

    None means "not approval-shaped": the turn stays ordinary conversation.
    A well-formed but wrong code is answered aloud ("unknown approval
    code."), never obeyed — that is information, not consent.
    """

    match = match_spoken_approval(text)
    if match is None:
        return None
    approved, nonce = match
    if broker is None:
        broker = ApprovalBroker()
    try:
        record = broker.answer_nonce(nonce, approved=approved,
                                     surface="voice", now=now)
    except ApprovalError as error:
        return f"{error}."
    verb = "approved" if approved else "denied"
    return f"{verb}: {record.capability}."


def pending_voice_announcement(*, broker: ApprovalBroker | None = None,
                               now: float | None = None,
                               limit: int = 2) -> str | None:
    """What she should say on a call about approvals waiting for him.

    The scan's voice push only reaches the desktop bridge, not call audio —
    so the call session asks every turn and speaks the answer itself. Tier 4
    is never announced with a code: voice cannot clear it, and reading the
    secret's existence aloud is still a leak shaped like helpfulness.
    """

    if broker is None:
        broker = ApprovalBroker()
    moment = time.time() if now is None else float(now)
    lines = []
    for confirmation in broker.authority.pending_confirmations(now=moment):
        if confirmation.tier >= TIER_SECRET:
            continue
        nonce = broker.live_nonce(confirmation.confirmation_id, now=moment)
        if nonce is None:
            continue
        what = f"{confirmation.capability} — {confirmation.target}".strip(" —")
        lines.append(f"quick approval needed: {what}. "
                     f"say approve {nonce} or deny {nonce}.")
        if len(lines) >= max(1, limit):
            break
    return " ".join(lines) if lines else None


def prompt_text(confirmation: Confirmation, nonce: str, *,
                channel: str = "imessage") -> str:
    """The ask in plain words: capability and target, never credentials.

    Tier 4 prompts carry no code on untyped channels: there is nothing to
    type back, because voice and text can never clear them.
    """

    _ = channel
    what = f"{confirmation.capability} — {confirmation.target}".strip(" —")
    if confirmation.tier >= TIER_SECRET:
        return (f"serena needs a yes in the app (this one can't clear by "
                f"text or voice): {what}.")
    return (f"serena needs a yes: {what}. reply \"yes {nonce}\" to approve "
            f"or \"no {nonce}\" to deny.")


class ApprovalBroker:
    """Mint nonces, push asks, and resolve answers with surface recorded."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        authority: Any | None = None,
        notifications: Any | None = None,
        in_call: Callable[[], bool] | None = None,
    ) -> None:
        configured = os.environ.get("SERENA_APPROVALS_DB_PATH", "").strip()
        self.path = Path(path or configured or DEFAULT_DB_PATH).expanduser()
        self._authority = authority
        self._notifications = notifications
        self._in_call = in_call
        self._initialize()

    @property
    def authority(self) -> Any:
        if self._authority is None:
            from core.action_authority import default_authority

            self._authority = default_authority()
        return self._authority

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                    CREATE TABLE IF NOT EXISTS approval_nonces (
                        nonce TEXT PRIMARY KEY,
                        confirmation_id TEXT NOT NULL,
                        issued_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        used_at REAL,
                        notified_at REAL,
                        channel TEXT NOT NULL DEFAULT ''
                    )
                    """)

    def _prune(self, moment: float) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "DELETE FROM approval_nonces WHERE expires_at < ?",
                (moment - 3600.0,))

    def _live_nonce(self, confirmation_id: str, moment: float) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT nonce FROM approval_nonces WHERE confirmation_id=? "
                "AND used_at IS NULL AND expires_at > ? ORDER BY issued_at "
                "DESC LIMIT 1",
                (confirmation_id, moment),
            ).fetchone()
        return str(row["nonce"]) if row is not None else None

    def live_nonce(self, confirmation_id: str,
                   *, now: float | None = None) -> str | None:
        """The live code for one confirmation, if it has one right now."""

        moment = time.time() if now is None else float(now)
        return self._live_nonce(confirmation_id, moment)

    def mint_nonce(self, confirmation: Confirmation,
                   *, now: float | None = None) -> str:
        """One code per confirmation, unique among every live code."""

        moment = time.time() if now is None else float(now)
        live = self._live_nonce(confirmation.confirmation_id, moment)
        if live is not None:
            return live
        expires_at = min(confirmation.expires_at,
                         moment + NONCE_TTL_SECONDS)
        with closing(self._connect()) as connection:
            for _ in range(MAX_MINT_ATTEMPTS):
                nonce = f"{secrets.randbelow(10 ** NONCE_DIGITS):04d}"
                taken = connection.execute(
                    "SELECT 1 FROM approval_nonces WHERE nonce=? "
                    "AND used_at IS NULL AND expires_at > ?",
                    (nonce, moment),
                ).fetchone()
                if taken is not None:
                    continue
                # Plain INSERT, never OR REPLACE: two minters can pass the
                # SELECT together, and the loser must draw again — not steal
                # the winner's live row. A used or long-dead row still owns
                # its value until the prune below clears it.
                try:
                    with connection:
                        connection.execute(
                            "INSERT INTO approval_nonces(nonce, "
                            "confirmation_id, issued_at, expires_at) VALUES "
                            "(?, ?, ?, ?)",
                            (nonce, confirmation.confirmation_id, moment,
                             expires_at),
                        )
                except sqlite3.IntegrityError:
                    continue
                self._prune(moment)
                return nonce
        raise ApprovalError("could not mint a unique approval code")

    def _mark_used(self, nonce: str, moment: float) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE approval_nonces SET used_at=? WHERE nonce=?",
                (moment, nonce))

    def _mark_notified(self, nonce: str, channel: str, moment: float) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE approval_nonces SET notified_at=?, channel=? "
                "WHERE nonce=?",
                (moment, channel, nonce))

    def _notified(self, nonce: str) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT notified_at FROM approval_nonces WHERE nonce=?",
                (nonce,)).fetchone()
        return row is not None and row["notified_at"] is not None

    def _refuse(self, reason: str, *, confirmation_id: str = "",
                surface: str = "", actor: str = "") -> ApprovalError:
        self.authority.audit("approval.refused", {
            "confirmation_id": confirmation_id, "surface": surface,
            "actor": actor, "reason": reason})
        return ApprovalError(reason)

    def _lookup(self, nonce: str, moment: float, *,
                surface: str = "", actor: str = "") -> sqlite3.Row:
        code = "".join(str(nonce or "").split())
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM approval_nonces").fetchall()
        for row in rows:
            if not hmac.compare_digest(str(row["nonce"]), code):
                continue
            if row["used_at"] is not None:
                raise self._refuse("that approval code was already used",
                                   confirmation_id=str(
                                       row["confirmation_id"]),
                                   surface=surface, actor=actor)
            if float(row["expires_at"]) <= moment:
                raise self._refuse("that approval code expired",
                                   confirmation_id=str(
                                       row["confirmation_id"]),
                                   surface=surface, actor=actor)
            return row
        raise self._refuse("unknown approval code", surface=surface,
                           actor=actor)

    def verify_nonce(self, nonce: str, *, now: float | None = None) -> str:
        """The confirmation id one live code answers, or an ApprovalError."""

        moment = time.time() if now is None else float(now)
        return str(self._lookup(nonce, moment)["confirmation_id"])

    def answer_nonce(self, nonce: str, *, approved: bool, surface: str,
                     actor: str = "raghav",
                     now: float | None = None) -> Confirmation:
        """Resolve one confirmation by code, once, with the surface recorded.

        A wrong, expired or reused code is refused and audited, and the
        pending confirmation stays pending. Tier 4 never clears here from
        voice or text.
        """

        from core.action_authority import ActionAuthorityError

        moment = time.time() if now is None else float(now)
        if surface not in SURFACES:
            raise self._refuse(f"unknown answering surface {surface!r}",
                               surface=surface, actor=actor)
        row = self._lookup(nonce, moment, surface=surface, actor=actor)
        confirmation_id = str(row["confirmation_id"])
        code = str(row["nonce"])
        confirmation = self.authority.confirmation(confirmation_id)
        if confirmation is None or confirmation.state != "pending":
            self._mark_used(code, moment)
            raise self._refuse("that approval is already answered",
                               confirmation_id=confirmation_id,
                               surface=surface, actor=actor)
        if (confirmation.tier >= TIER_SECRET
                and surface not in TYPED_SOURCES):
            raise self._refuse(
                f"tier {confirmation.tier} needs a typed surface "
                "(chat, ui or cli), not voice or text",
                confirmation_id=confirmation_id, surface=surface,
                actor=actor)
        try:
            record = self.authority.resolve_confirmation(
                confirmation_id, approved=approved, resolved_by=actor,
                surface=surface, now=moment)
        except ActionAuthorityError as error:
            self._mark_used(code, moment)
            raise self._refuse(str(error), confirmation_id=confirmation_id,
                               surface=surface, actor=actor) from error
        self._mark_used(code, moment)
        return record

    def answer_confirmation(self, confirmation_id: str, *, approved: bool,
                            surface: str, actor: str = "raghav",
                            now: float | None = None) -> Confirmation:
        """Resolve one confirmation directly (typed surfaces: web UI, cli).

        Any live code for it dies with the answer, so a later text can never
        double-answer.
        """

        moment = time.time() if now is None else float(now)
        if surface not in TYPED_SOURCES:
            raise self._refuse(
                f"direct answers need a typed surface, not {surface}",
                confirmation_id=confirmation_id, surface=surface,
                actor=actor)
        record = self.authority.resolve_confirmation(
            confirmation_id, approved=approved, resolved_by=actor,
            surface=surface, now=moment)
        live = self._live_nonce(record.confirmation_id, moment)
        if live is not None:
            self._mark_used(live, moment)
        return record

    def scan_channel(self, confirmation: Confirmation) -> str:
        """The one push channel for a confirmation, right now."""

        in_call = (self._in_call() if self._in_call is not None
                   else call_active())
        return choose_channel(confirmation, in_call=in_call)

    def scan(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """Push every unannounced pending confirmation, once, on one channel."""

        from core.notification_senders import notify

        moment = time.time() if now is None else float(now)
        in_call = self._in_call() if self._in_call is not None else call_active()
        report: list[dict[str, Any]] = []
        for confirmation in self.authority.pending_confirmations(now=moment):
            nonce = self.mint_nonce(confirmation, now=moment)
            channel = choose_channel(confirmation, in_call=in_call)
            entry: dict[str, Any] = {
                "confirmation_id": confirmation.confirmation_id,
                "nonce": nonce, "channel": channel,
                "tier": confirmation.tier, "notified": False}
            if not self._notified(nonce):
                text = prompt_text(confirmation, nonce, channel=channel)
                result = notify(
                    KIND, text, channel=channel, urgency="normal",
                    dedupe_key=f"approval:{confirmation.confirmation_id}",
                    source_surface="system", fallback_channel=None,
                    authority=self._notifications)
                if result.sent or result.decision in ("deferred",
                                                      "pending_approval"):
                    self._mark_notified(nonce, channel, moment)
                    entry["notified"] = True
                entry["decision"] = result.decision
            report.append(entry)
        self._prune(moment)
        return report
