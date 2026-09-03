"""Restart recovery for the things Serena still owes Raghav.

Fleet already retried its own unannounced terminal notices on service boot, but
that logic only knew about Fleet, and only about the newest run. Anything else
that died mid-delivery, a spoken job result, a queued notice, a memory proposal
waiting on him, simply vanished.

The obligation ledger already records those as open. This module reads them
after a restart and hands each one back to the surface that owns redelivery.
Native journals stay authoritative: this decides *what* is still outstanding,
never *how* to deliver it, and it never marks anything fulfilled on its own.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from typing import Any

from core.control_plane import ControlPlaneStore, Obligation

# A restarted process should not fight over an obligation that another live
# worker is still mid-flight on. Only things untouched for this long are
# considered abandoned.
DEFAULT_STALE_SECONDS = 120.0
# Past this, retrying is no longer honest recovery, it is a loop.
DEFAULT_MAX_ATTEMPTS = 5

RedeliveryHandler = Callable[[Obligation], bool]


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    obligation_id: str
    surface: str
    kind: str
    action: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    recovered: list[RecoveryOutcome] = field(default_factory=list)
    abandoned: list[RecoveryOutcome] = field(default_factory=list)
    skipped: list[RecoveryOutcome] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.recovered) + len(self.abandoned) + len(self.skipped)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recovered": [item.to_dict() for item in self.recovered],
            "abandoned": [item.to_dict() for item in self.abandoned],
            "skipped": [item.to_dict() for item in self.skipped],
            "total": self.total,
        }


def reconcile_obligations(
    store: ControlPlaneStore | None = None,
    *,
    handlers: dict[str, RedeliveryHandler] | None = None,
    stale_seconds: float = DEFAULT_STALE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    now: float | None = None,
) -> RecoveryReport:
    """Hand every stale open obligation back to its surface, once.

    A handler returns True when it genuinely re-dispatched the delivery. It does
    not get to declare the obligation fulfilled; only the surface's real
    delivery event closes it. That keeps the ledger honest across a crash.
    """

    control = store or ControlPlaneStore()
    registry = dict(handlers or {})
    moment = time.time() if now is None else float(now)
    recovered: list[RecoveryOutcome] = []
    abandoned: list[RecoveryOutcome] = []
    skipped: list[RecoveryOutcome] = []

    for obligation in control.recoverable_obligations(older_than=stale_seconds, now=moment):
        if obligation.attempts >= max_attempts:
            control.abandon_obligation(
                obligation.obligation_id,
                reason=(
                    f"gave up after {obligation.attempts} delivery attempts; "
                    "Serena cannot confirm Raghav received this"
                ),
            )
            abandoned.append(
                RecoveryOutcome(
                    obligation_id=obligation.obligation_id,
                    surface=obligation.surface,
                    kind=obligation.kind,
                    action="abandoned",
                    detail=f"attempt budget of {max_attempts} exhausted",
                )
            )
            continue
        handler = registry.get(obligation.surface)
        if handler is None:
            skipped.append(
                RecoveryOutcome(
                    obligation_id=obligation.obligation_id,
                    surface=obligation.surface,
                    kind=obligation.kind,
                    action="skipped",
                    detail="no redelivery handler is registered for this surface",
                )
            )
            continue
        try:
            redelivered = bool(handler(obligation))
        except Exception as error:  # a broken handler must not stall the sweep
            skipped.append(
                RecoveryOutcome(
                    obligation_id=obligation.obligation_id,
                    surface=obligation.surface,
                    kind=obligation.kind,
                    action="skipped",
                    detail=f"redelivery handler raised: {error}"[:500],
                )
            )
            continue
        outcome = RecoveryOutcome(
            obligation_id=obligation.obligation_id,
            surface=obligation.surface,
            kind=obligation.kind,
            action="redelivered" if redelivered else "skipped",
            detail="" if redelivered else "handler declined to redeliver",
        )
        (recovered if redelivered else skipped).append(outcome)
    return RecoveryReport(recovered=recovered, abandoned=abandoned, skipped=skipped)


def outstanding_summary(store: ControlPlaneStore | None = None) -> dict[str, Any]:
    """What Serena currently owes, grouped for status surfaces."""

    control = store or ControlPlaneStore()
    open_items = control.obligations(state="open", limit=1_000)
    by_surface: dict[str, int] = {}
    for item in open_items:
        by_surface[item.surface] = by_surface.get(item.surface, 0) + 1
    return {
        "open": len(open_items),
        "by_surface": dict(sorted(by_surface.items())),
        "oldest_created_at": min((item.created_at for item in open_items), default=None),
        "items": [
            {
                "obligation_id": item.obligation_id,
                "surface": item.surface,
                "kind": item.kind,
                "summary": item.summary,
                "attempts": item.attempts,
                "last_error": item.last_error,
                "created_at": item.created_at,
            }
            for item in sorted(open_items, key=lambda entry: entry.created_at)[:50]
        ],
    }


# What each surface's outstanding promise sounds like when Serena says it out
# loud. Keyed by obligation kind so a new rule has to name its own wording
# rather than silently inheriting something vague.
_OWED_PHRASING = {
    "final_result_delivery": "tell you how a Fleet run ended",
    "spoken_job_result_delivery": "tell you how a spoken coding job ended",
    "memory_proposal_review": "get a decision from you on a proposed memory change",
    "notification_delivery": "get a notice to you",
    "tool_result_delivery": "return a tool result",
    "chat_reply_delivery": "finish answering a turn",
}


def _ago(seconds: float) -> str:
    if seconds < 90:
        return "just now"
    if seconds < 5_400:
        return f"{int(seconds // 60)} minutes ago"
    if seconds < 172_800:
        return f"{int(seconds // 3_600)} hours ago"
    return f"{int(seconds // 86_400)} days ago"


def outstanding_debts(
    store: ControlPlaneStore | None = None,
    *,
    limit: int = 8,
    now: float | None = None,
) -> dict[str, Any]:
    """What Serena still owes Raghav, in language she can actually say.

    `outstanding_summary` is the machine view. This is the one a person reads:
    it groups by what the promise *is*, counts the ambiguous ones separately
    because "I don't know if you got it" is a different admission from "I still
    owe you this", and produces one spoken line.
    """

    control = store or ControlPlaneStore()
    moment = time.time() if now is None else float(now)
    open_items = control.obligations(state="open", limit=1_000)
    unsure = control.obligations(state="ambiguous", limit=1_000)

    entries: list[dict[str, Any]] = []
    for item in sorted(open_items, key=lambda entry: entry.created_at):
        entries.append(
            {
                "obligation_id": item.obligation_id,
                "surface": item.surface,
                "owes": _OWED_PHRASING.get(item.kind, item.kind.replace("_", " ")),
                "summary": item.summary,
                "attempts": item.attempts,
                "last_error": item.last_error,
                "waiting_for": _ago(max(0.0, moment - item.created_at)),
                "created_at": item.created_at,
            }
        )

    stuck = [item for item in entries if item["attempts"] > 0]
    if not entries and not unsure:
        spoken = "nothing outstanding, we're square"
    else:
        parts: list[str] = []
        if entries:
            head = entries[0]
            parts.append(
                f"{len(entries)} thing{'s' if len(entries) != 1 else ''} still open; "
                f"oldest is to {head['owes']}, waiting since {head['waiting_for']}"
            )
        if stuck:
            parts.append(f"{len(stuck)} already failed at least one delivery")
        if unsure:
            parts.append(
                f"{len(unsure)} I can't confirm you ever got"
            )
        spoken = ". ".join(parts)

    return {
        "open": len(entries),
        "stuck": len(stuck),
        "unconfirmed": len(unsure),
        "spoken": spoken,
        "items": entries[: max(1, int(limit))],
        "unconfirmed_items": [
            {
                "obligation_id": item.obligation_id,
                "surface": item.surface,
                "owes": _OWED_PHRASING.get(item.kind, item.kind.replace("_", " ")),
                "summary": item.summary,
                "reason": item.last_error,
            }
            for item in sorted(unsure, key=lambda entry: entry.created_at)[
                : max(1, int(limit))
            ]
        ],
    }


def journal_redelivery_handlers(
    store: ControlPlaneStore | None = None,
    *,
    surfaces: Sequence[str] = ("voice", "chat", "tool", "memory"),
) -> dict[str, RedeliveryHandler]:
    """Recovery handlers for every surface that publishes through a journal.

    Fleet and the notification authority know how to genuinely re-send their
    own work, so they register real handlers elsewhere. The journalled surfaces
    are different: a voice turn or a tool call that died with the process
    cannot be re-run safely from a sweep, because re-running it would repeat a
    side effect Raghav may already have seen.

    So this does the honest thing instead of the impressive thing. It counts
    the interruption as a real failed attempt, which keeps the retry budget
    moving and preserves the evidence, and lets `reconcile_obligations`
    eventually resolve it `ambiguous`. Serena ends up able to say "I don't know
    if you got that", which is true, rather than replaying a turn at him.

    The failure is written straight to the store being reconciled, not through
    a surface journal. A journal is bound to its own file and its own control
    plane, and a sweep recovering *this* plane must not record its evidence
    into a different one.
    """

    from core.control_plane import OBLIGATION_RULES

    rules = {rule.surface: rule for rule in OBLIGATION_RULES}

    def make(surface: str) -> RedeliveryHandler:
        rule = rules.get(surface)

        def handler(obligation: Obligation) -> bool:
            control = store or ControlPlaneStore()
            if rule is not None and rule.fail_on and obligation.job_id:
                with suppress(Exception):
                    control.append_event(
                        surface=surface,
                        event_type=rule.fail_on[0],
                        lifecycle_state="interrupted",
                        delivery_state="uncertain",
                        # Keyed on the attempt count already recorded, so one
                        # sweep counts once and a repeated sweep at the same
                        # count is idempotent.
                        event_id=(
                            f"{surface}:{obligation.job_id}:recovery:"
                            f"{obligation.attempts}"
                        ),
                        job_id=obligation.job_id,
                        session_id=obligation.session_id,
                        authority="control_recovery",
                        payload={
                            "error": (
                                "the process restarted before this finished; "
                                "Serena cannot confirm it reached Raghav"
                            ),
                        },
                    )
            # Never claims a redelivery happened: nothing was re-sent.
            return False

        return handler

    return {surface: make(surface) for surface in surfaces}
