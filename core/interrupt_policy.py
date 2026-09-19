"""The single place that answers "may she speak".

Every proactive path — notifications, check-ins, stuck nudges — goes through
`decide()` before touching the notification authority, and the authority
re-verifies caps, quiet hours, and typing holds on the way out. Nothing here
bypasses the authority; this gate only decides whether asking is even wise.

Breakpoints come from the ambient stream, not from guesses: an unlock, a
return from a real idle gap, an app switch after a long focus block, or a
commit/push. Mid-focus delivery waits; over-cap delivery batches into a
digest; repeatedly dismissed kinds are dropped with a logged reason.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.ambient_store import AmbientEvent
from core.notification_authority import (
    DIGEST_KIND,
    NotificationPolicy,
    NotificationRequest,
    PresenceState,
)
from core.sqlite_connection import connect_database

BREAKPOINT_RECENCY_SECONDS = 120.0
IDLE_GAP_SECONDS = 300.0
FOCUS_BLOCK_SECONDS = 1_500.0


@dataclass(frozen=True, slots=True)
class ProactiveItem:
    kind: str
    summary: str
    channel: str = "desktop"
    urgency: str = "normal"
    dedupe_key: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    action: str  # deliver | hold | drop (digest is decided by scan_and_release)
    reason: str


def decide(
    item: ProactiveItem,
    *,
    presence: PresenceState,
    policy: NotificationPolicy,
    now: float,
    is_suppressed: Callable[[str], bool] | None = None,
) -> PolicyDecision:
    """Deliver now, hold for a breakpoint, or drop.

    Batching into a digest is not a per-item verdict: `scan_and_release`
    decides that at a breakpoint, once it can see how crowded a channel is.
    """

    if (
        item.urgency != "critical"
        and is_suppressed is not None
        and is_suppressed(item.kind)
    ):
        return PolicyDecision("drop", f"{item.kind} dismissed repeatedly; suppressed")
    if item.urgency == "critical":
        return PolicyDecision("deliver", "critical urgency")
    if policy.in_quiet_hours(now):
        resume = policy.next_quiet_end(now)
        return PolicyDecision("hold", f"quiet hours; resumes at {int(resume)}")
    if presence.typing:
        return PolicyDecision("hold", "typing; held for breakpoint")
    if presence.activity_class == "focused":
        return PolicyDecision("hold", "focused; held for breakpoint")
    return PolicyDecision("deliver", "at a natural boundary")


def is_breakpoint(events: list[AmbientEvent], *, now: float) -> bool:
    """True when the trailing ambient stream shows a natural boundary."""

    windowed = sorted(
        (
            event
            for event in events
            if not event.agent_driven and event.started_at <= now
        ),
        key=lambda item: item.started_at,
    )
    if not windowed:
        return False
    recent = [
        event
        for event in windowed
        if now - event.started_at <= BREAKPOINT_RECENCY_SECONDS
    ]
    if any(event.kind in {"unlock", "vcs"} for event in recent):
        return True
    if _returned_from_idle(windowed, now):
        return True
    return _switched_after_focus(windowed, now)


def _returned_from_idle(windowed: list[AmbientEvent], now: float) -> bool:
    actives = [
        event for event in windowed
        if event.kind == "active" and now - event.started_at <= BREAKPOINT_RECENCY_SECONDS
    ]
    if not actives:
        return False
    latest_active = actives[-1].started_at
    idles = [
        event.started_at for event in windowed
        if event.kind == "idle" and event.started_at <= latest_active
    ]
    return bool(idles) and latest_active - idles[-1] >= IDLE_GAP_SECONDS


def _switched_after_focus(windowed: list[AmbientEvent], now: float) -> bool:
    foreground = [
        event for event in windowed if event.kind in {"window", "tab"}
    ]
    if len(foreground) < 2:
        return False
    last = foreground[-1]
    if now - last.started_at > BREAKPOINT_RECENCY_SECONDS:
        return False
    if last.app == foreground[-2].app:
        return False
    run_start = foreground[-2].started_at
    for event in reversed(foreground[:-2]):
        if event.app != foreground[-2].app:
            break
        run_start = event.started_at
    return last.started_at - run_start >= FOCUS_BLOCK_SECONDS


POLICY_SCHEMA_VERSION = 1
DEFAULT_POLICY_DB = Path.home() / ".local" / "state" / "serena" / "interrupt_policy.sqlite3"
DISMISSAL_WINDOW_SECONDS = 30 * 86_400
SUPPRESSION_SILENCE_SECONDS = 7 * 86_400
SUPPRESSION_STRIKES = 3
TYPING_RECENCY_SECONDS = 10.0
FEEDBACK_OUTCOMES = ("dismissed", "ignored", "acted")


class PolicyStore:
    """Dismissal feedback with a silence window, plus the visible log."""

    def __init__(self, path: Path | None = None) -> None:
        configured = os.environ.get("SERENA_POLICY_DB_PATH", "").strip()
        self.path = Path(path or configured or DEFAULT_POLICY_DB).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        with suppress(OSError):
            self.path.chmod(0o600)

    def record_feedback(self, kind: str, outcome: str, *, now: float) -> None:
        if outcome not in FEEDBACK_OUTCOMES:
            raise ValueError(f"unknown feedback outcome {outcome!r}")
        clean_kind = " ".join(str(kind or "").split())[:128]
        if not clean_kind:
            raise ValueError("feedback needs a kind")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO dismissals(kind, outcome, created_at) VALUES (?, ?, ?)",
                (clean_kind, outcome, float(now)),
            )

    def is_suppressed(self, kind: str, *, now: float) -> bool:
        """Three strikes in 30 days buys a week of silence for that kind."""

        moment = float(now)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT created_at FROM dismissals "
                "WHERE kind = ? AND outcome IN ('dismissed', 'ignored') "
                "AND created_at >= ? ORDER BY created_at DESC",
                (" ".join(str(kind or "").split())[:128],
                 moment - DISMISSAL_WINDOW_SECONDS),
            ).fetchall()
        if len(rows) < SUPPRESSION_STRIKES:
            return False
        latest = float(rows[0]["created_at"])
        return moment - latest < SUPPRESSION_SILENCE_SECONDS

    def log_decision(self, kind: str, action: str, reason: str, *, now: float) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO policy_log(kind, action, reason, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    " ".join(str(kind or "").split())[:128],
                    " ".join(str(action or "").split())[:32],
                    " ".join(str(reason or "").split())[:1_000],
                    float(now),
                ),
            )

    def recent_log(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT kind, action, reason, created_at FROM policy_log "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (min(500, max(1, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS dismissals (
                    dismissal_id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS dismissals_kind_idx
                ON dismissals(kind, created_at);
                CREATE TABLE IF NOT EXISTS policy_log (
                    log_id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                PRAGMA user_version=1;
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return connect_database(self.path, timeout=5)


def record_feedback(
    kind: str, outcome: str, *, now: float, store: PolicyStore | None = None
) -> None:
    (store or PolicyStore()).record_feedback(kind, outcome, now=now)


DISMISS_RECENCY_SECONDS = 86_400


def dismiss_latest_proactive(
    authority, *, now: float, store: PolicyStore | None = None
) -> str:
    """Record a dismissal against the latest proactive kind he was sent.

    The production feedback surface behind the phone line's `stop`: whatever
    proactive kind most recently reached him earns one strike, so three stops
    silence it for a week. Returns the kind, or "" when nothing recent
    qualifies (never raises; a failed lookup is not his problem).
    """

    moment = float(now)
    try:
        history = authority.history(limit=50)
    except Exception:
        return ""
    for row in history:
        try:
            proactive = bool(row.get("proactive"))
            created = float(row.get("created_at") or 0.0)
            kind = str(row.get("kind") or "")
            decision = str(row.get("decision") or "")
        except (TypeError, ValueError):
            continue
        if not proactive or kind == DIGEST_KIND:
            continue
        if decision != "sent":
            # Held, failed, or suppressed rows never reached him: striking
            # one would punish a kind for a nudge he never saw.
            continue
        if moment - created > DISMISS_RECENCY_SECONDS:
            continue
        try:
            record_feedback(kind, "dismissed", now=moment, store=store)
        except Exception:
            return ""
        return kind
    return ""


def presence_now(*, store=None, now: float | None = None) -> PresenceState:
    """Typing recency plus the classifier's activity class. Never raises."""

    from core.ambient_classify import classify
    from core.ambient_store import AmbientStore

    moment = time.time() if now is None else float(now)
    try:
        # Disk, not the in-memory ring: the ring lives in the daemon
        # process, while this usually runs in a scheduler or brain turn.
        events = (store or AmbientStore()).recent_from_disk(
            now=moment, limit=200, seconds=3_600.0)
    except Exception:
        return PresenceState()
    mine = [
        event for event in events
        if not event.agent_driven and event.kind in {"window", "tab", "active"}
    ]
    typing = bool(mine) and (moment - max(e.ended_at for e in mine)) <= TYPING_RECENCY_SECONDS
    try:
        activity_class = classify(events, now=moment).activity_class
    except Exception:
        activity_class = ""
    return PresenceState(typing=typing, activity_class=activity_class)


def scan_and_release(authority, events: list[AmbientEvent], *, now: float,
                     policy_store: PolicyStore | None = None) -> list[str]:
    """At a breakpoint: digest crowded channels, deliver the rest oldest-first.

    Returns a tag per release (`digest:<channel>` or a notification id) so
    the scheduler tick can report what moved. No breakpoint, quiet hours, or
    continued typing/focus means nothing moves and nothing is logged as sent.
    """

    moment = float(now)
    store = policy_store or PolicyStore()
    if not is_breakpoint(events, now=moment):
        return []
    released: list[str] = []
    held = authority.held_items(limit=100)
    by_channel: dict[str, list[dict]] = {}
    for row in held:
        by_channel.setdefault(str(row["channel"]), []).append(row)
    for channel in sorted(by_channel):
        group = by_channel[channel]
        if len(group) < 2:
            continue
        summaries = [str(item["summary"]) for item in group]
        digest_summary = (
            f"{len(group)} held {channel} updates: " + " / ".join(summaries)
        )[:800]
        result = authority.request(
            NotificationRequest(
                kind=DIGEST_KIND,
                summary=digest_summary,
                channel=channel,
                proactive=True,
                dedupe_key=f"digest:{channel}:{int(moment // 86_400)}",
            ),
            now=moment,
        )
        if result.sent:
            consumed = authority.consume_held_as_digest(
                [str(item["notification_id"]) for item in group],
                result.notification_id,
                now=moment,
            )
            store.log_decision(
                DIGEST_KIND, "digest",
                f"{consumed} {channel} items batched into {result.notification_id}",
                now=moment,
            )
            released.append(f"digest:{channel}")
        else:
            store.log_decision(
                DIGEST_KIND, "hold",
                f"{channel} digest {result.decision}: {result.reason}",
                now=moment,
            )
    for item in authority.release_at_breakpoint(now=moment):
        released.append(item.notification_id)
    return released
