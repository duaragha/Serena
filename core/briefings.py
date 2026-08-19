"""What Serena actually says about what is owed, and when she is allowed to.

Two decisions live here and they are deliberately kept apart. Whether there is
anything worth saying is a content question, and it is answered here against the
commitment store. Whether Raghav may be interrupted is a policy question, and it
is answered by `core.notification_authority`, which already owns quiet hours,
rate limits, deduplication, and durable retry. This module imports that policy
rather than reimplementing it, so there is exactly one definition of "it is 3am,
do not".

The generator is boring on purpose. It reads local sqlite, sorts, and formats
text. No model call, no network, no provider. That is what makes a briefing
survive a total cloud outage: in degraded or offline mode this code path is
unchanged, and the only difference is that the briefing says which mode it was
built in instead of quietly implying everything is normal.

Nothing here sends. `deliver` requires an authority to be handed in, with no
default, because a module that can reach the real Telegram bot by accident is
one import away from texting him during a test run. The scheduler actions at the
bottom return a `notify` payload and let the scheduler's existing bounded path
hand it to the authority, which is the same route every other Serena job takes.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from core.commitments import Commitment, CommitmentStore
from core.daypart import spoken_clock
from core.notification_authority import NotificationPolicy, NotificationRequest

BRIEFING_KINDS = ("morning", "evening", "pre_event")

# How far ahead each briefing looks. Morning covers the day; evening covers
# tonight plus tomorrow so "what's coming" is answered before he stops working.
MORNING_HORIZON_SECONDS = 86_400
EVENING_HORIZON_SECONDS = 36 * 3_600
# Past this many items a briefing stops being a briefing and becomes a list he
# tunes out. The rest stay in the store and surface tomorrow.
MAX_SPOKEN_ITEMS = 4
# Two briefings inside this window is nagging, not diligence.
DEFAULT_MIN_GAP_SECONDS = 3 * 3_600

CONTINUITY_NOTES = {
    "degraded": "running on the local model right now, so this is the short version",
    "offline": "no model available right now, this is straight off your list",
}


@dataclass(frozen=True, slots=True)
class Briefing:
    """One assembled thing to say, plus everything needed to decide whether to."""

    kind: str
    generated_at: float
    spoken: str
    lines: tuple[str, ...] = ()
    commitment_ids: tuple[str, ...] = ()
    urgency: str = "low"
    dedupe_key: str = ""
    mode: str = "full"

    @property
    def empty(self) -> bool:
        return not self.commitment_ids and not self.lines

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["lines"] = list(self.lines)
        value["commitment_ids"] = list(self.commitment_ids)
        value["empty"] = self.empty
        return value

    def notify_payload(self, *, channel: str = "voice") -> dict[str, Any]:
        return {
            "kind": f"serena.briefing.{self.kind}",
            "summary": self.spoken,
            "channel": channel,
            "urgency": self.urgency,
            "dedupe_key": self.dedupe_key,
        }


@dataclass(frozen=True, slots=True)
class InterruptionDecision:
    interrupt: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class InterruptionRules:
    """Content-level rules, wrapped around the one real notification policy.

    `policy` is the same `NotificationPolicy` the authority enforces, held here
    only so a caller can ask "would this be held back?" before spending work
    assembling a briefing. It is advisory. The authority still decides at send
    time, and it is the decision that counts.
    """

    policy: NotificationPolicy = field(default_factory=NotificationPolicy)
    min_gap_seconds: float = DEFAULT_MIN_GAP_SECONDS
    # An overdue critical commitment is worth waking someone for. Nothing else is.
    escalate_overdue_critical: bool = True

    def evaluate(
        self,
        briefing: Briefing,
        *,
        now: float | None = None,
        last_briefing_at: float | None = None,
    ) -> InterruptionDecision:
        moment = float(time.time() if now is None else now)
        if briefing.empty:
            return InterruptionDecision(False, "nothing worth saying")
        if (
            last_briefing_at is not None
            and moment - float(last_briefing_at) < self.min_gap_seconds
            and briefing.urgency != "critical"
        ):
            return InterruptionDecision(
                False,
                f"a briefing already went out inside the last "
                f"{int(self.min_gap_seconds // 3_600)}h",
            )
        if briefing.urgency == "critical":
            return InterruptionDecision(True, "something overdue and critical")
        if self.policy.in_quiet_hours(moment):
            return InterruptionDecision(
                False, "quiet hours; the authority will hold this until morning"
            )
        return InterruptionDecision(True, "worth telling him")


# ---- phrasing -------------------------------------------------------------


def _when(commitment: Commitment, now: float) -> str:
    if commitment.due_at is None:
        return "no date on it"
    due = float(commitment.due_at)
    delta = due - now
    moment = datetime.fromtimestamp(due)
    if delta < 0:
        return f"overdue since {_ago(-delta)}"
    if delta < 3_600:
        return f"in {max(1, int(delta // 60))} min"
    if delta < 12 * 3_600:
        return f"at {spoken_clock(moment)}"
    if delta < 48 * 3_600:
        return f"tomorrow {spoken_clock(moment)}"
    return moment.strftime("%a %d %b")


def _ago(seconds: float) -> str:
    if seconds < 3_600:
        return f"{max(1, int(seconds // 60))} min"
    if seconds < 48 * 3_600:
        return f"{int(seconds // 3_600)}h"
    return f"{int(seconds // 86_400)}d"


def _phrase(commitment: Commitment, now: float) -> str:
    return f"{commitment.title.lower()}, {_when(commitment, now)}"


def _urgency(items: Sequence[Commitment], now: float) -> str:
    """Only a genuinely critical overdue thing earns a quiet-hours override."""

    for item in items:
        if item.priority == "critical" and item.is_overdue(now):
            return "critical"
    if any(item.is_overdue(now) for item in items):
        return "normal"
    return "low"


def _ranked(items: Iterable[Commitment], now: float) -> list[Commitment]:
    """Overdue first, then by priority, then by how soon it lands."""

    return sorted(
        items,
        key=lambda item: (
            0 if item.is_overdue(now) else 1,
            -item.priority_rank,
            float(item.due_at) if item.due_at is not None else float("inf"),
        ),
    )


def _mode_note(mode: str) -> str:
    return CONTINUITY_NOTES.get(str(mode or "full"), "")


# ---- generation -----------------------------------------------------------


def build_morning_briefing(
    store: CommitmentStore,
    *,
    now: float | None = None,
    horizon_seconds: float = MORNING_HORIZON_SECONDS,
    mode: str = "full",
) -> Briefing:
    """What today actually holds, overdue things first."""

    moment = float(time.time() if now is None else now)
    items = _ranked(store.due(now=moment, horizon_seconds=horizon_seconds), moment)
    overdue = [item for item in items if item.is_overdue(moment)]
    lines = tuple(_phrase(item, moment) for item in items[:MAX_SPOKEN_ITEMS])

    if not items:
        spoken = "nothing on the books today, you're clear"
    else:
        head = f"{len(items)} thing{'s' if len(items) != 1 else ''} today"
        detail = "; ".join(lines)
        spoken = f"{head}: {detail}"
        if overdue:
            spoken += f". {len(overdue)} already overdue"
        if len(items) > MAX_SPOKEN_ITEMS:
            spoken += f". {len(items) - MAX_SPOKEN_ITEMS} more behind those"
    note = _mode_note(mode)
    if note:
        spoken += f". {note}"

    return Briefing(
        kind="morning",
        generated_at=moment,
        spoken=spoken,
        lines=lines,
        commitment_ids=tuple(item.commitment_id for item in items),
        urgency=_urgency(items, moment),
        dedupe_key=f"briefing:morning:{_day_key(moment)}",
        mode=mode,
    )


def build_evening_briefing(
    store: CommitmentStore,
    *,
    now: float | None = None,
    horizon_seconds: float = EVENING_HORIZON_SECONDS,
    mode: str = "full",
) -> Briefing:
    """What got closed today, and what is waiting tomorrow."""

    moment = float(time.time() if now is None else now)
    day_start = _start_of_day(moment)
    closed = [
        item
        for item in store.list(state="completed")
        if item.completed_at is not None and float(item.completed_at) >= day_start
    ]
    ahead = _ranked(store.due(now=moment, horizon_seconds=horizon_seconds), moment)
    still_overdue = [item for item in ahead if item.is_overdue(moment)]
    lines = tuple(_phrase(item, moment) for item in ahead[:MAX_SPOKEN_ITEMS])

    parts: list[str] = []
    if closed:
        parts.append(f"you closed {len(closed)} today")
    if ahead:
        parts.append(f"next up: {'; '.join(lines)}")
    if still_overdue:
        parts.append(
            f"{len(still_overdue)} still overdue"
        )
    spoken = ". ".join(parts) if parts else "nothing closed and nothing pending, quiet day"
    note = _mode_note(mode)
    if note:
        spoken += f". {note}"

    return Briefing(
        kind="evening",
        generated_at=moment,
        spoken=spoken,
        lines=lines,
        commitment_ids=tuple(item.commitment_id for item in ahead)
        + tuple(item.commitment_id for item in closed),
        urgency=_urgency(ahead, moment),
        dedupe_key=f"briefing:evening:{_day_key(moment)}",
        mode=mode,
    )


def build_pre_event_briefing(
    commitment: Commitment,
    *,
    now: float | None = None,
    mode: str = "full",
) -> Briefing:
    """The heads-up before one specific thing lands."""

    moment = float(time.time() if now is None else now)
    spoken = f"{commitment.title.lower()}, {_when(commitment, moment)}"
    if commitment.detail and commitment.detail.strip() != commitment.title.strip():
        spoken += f". {commitment.detail.strip().splitlines()[0].lower()}"
    note = _mode_note(mode)
    if note:
        spoken += f". {note}"
    return Briefing(
        kind="pre_event",
        generated_at=moment,
        spoken=spoken,
        lines=(_phrase(commitment, moment),),
        commitment_ids=(commitment.commitment_id,),
        urgency="critical" if commitment.priority == "critical" else "normal",
        # Keyed per commitment, so one thing gets one heads-up no matter how
        # often the scheduler ticks.
        dedupe_key=f"briefing:pre_event:{commitment.commitment_id}",
        mode=mode,
    )


def pending_pre_event_briefings(
    store: CommitmentStore,
    *,
    now: float | None = None,
    mode: str = "full",
) -> list[Briefing]:
    """Everything entering its lead window that has not been spoken about yet."""

    moment = float(time.time() if now is None else now)
    ready: list[Briefing] = []
    for item in store.list(open_only=True):
        if item.due_at is None or item.dismissed_at is not None:
            continue
        if item.is_snoozed(moment):
            continue
        due = float(item.due_at)
        if due < moment:
            continue  # overdue belongs to the morning briefing, not a heads-up
        if due - item.lead_seconds > moment:
            continue
        if item.last_briefed_at is not None and float(item.last_briefed_at) >= due - item.lead_seconds:
            continue
        ready.append(build_pre_event_briefing(item, now=moment, mode=mode))
    return ready


def _start_of_day(moment: float) -> float:
    current = datetime.fromtimestamp(moment)
    return current.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _day_key(moment: float) -> str:
    return datetime.fromtimestamp(moment).strftime("%Y-%m-%d")


# ---- delivery -------------------------------------------------------------


def deliver(
    briefing: Briefing,
    *,
    authority: Any,
    channel: str = "voice",
    rules: InterruptionRules | None = None,
    now: float | None = None,
    last_briefing_at: float | None = None,
    store: CommitmentStore | None = None,
) -> tuple[InterruptionDecision, Any | None]:
    """Ask to say one briefing out loud.

    `authority` has no default and that is a safety property, not an oversight.
    Every caller has to name the authority it is sending through, so a test can
    hand in one built with fake senders and there is no import path that reaches
    the real voice bridge or Telegram bot by accident.

    When `store` is given, the commitments in this briefing are marked as spoken
    about only if the authority actually reports `sent`. Marking them any
    earlier records something as said that a suppression, a deferral, or a dead
    channel meant he never heard, and because `last_briefed_at` is the flag that
    stops a briefing repeating, that lost notice would never come back.
    """

    if authority is None:
        raise ValueError("delivering a briefing requires an explicit notification authority")
    moment = float(time.time() if now is None else now)
    decision = (rules or InterruptionRules()).evaluate(
        briefing, now=moment, last_briefing_at=last_briefing_at
    )
    if not decision.interrupt:
        return decision, None
    payload = briefing.notify_payload(channel=channel)
    result = authority.request(
        NotificationRequest(
            kind=payload["kind"],
            summary=payload["summary"],
            channel=payload["channel"],
            urgency=payload["urgency"],
            dedupe_key=payload["dedupe_key"],
            source_surface="system",
        ),
        now=moment,
    )
    if store is not None and _was_sent(result):
        for commitment_id in briefing.commitment_ids:
            store.mark_briefed(commitment_id, now=moment)
    return decision, result


def _was_sent(result: Any) -> bool:
    """Only a positive `sent` counts. Anything unreadable is treated as not sent."""

    if result is None:
        return False
    sent = getattr(result, "sent", None)
    if sent is not None:
        return bool(sent)
    return str(getattr(result, "decision", "") or "") == "sent"


# ---- scheduler actions ----------------------------------------------------

# These are additive on purpose. `core.scheduler_actions.REVIEWED_ACTIONS` stays
# exactly the closed set it already was, including its test that asserts the set
# by name; a caller that wants commitments on a timer registers these on top.
# Adding a key to that shared dict would have been a smaller diff and a worse
# idea: it is another unit's file and another unit's test.
BRIEFING_ACTIONS = (
    "serena.commitments.ingest",
    "serena.commitments.activate",
    "serena.briefing.morning",
    "serena.briefing.evening",
    "serena.briefing.pre_event",
)


def register_briefing_actions(
    scheduler: Any,
    *,
    store: CommitmentStore | None = None,
    sources: Iterable[Any] | None = None,
    rules: InterruptionRules | None = None,
    mode_reader: Any | None = None,
    clock: Callable[[], float] | None = None,
    authority: Any | None = None,
) -> Any:
    """Attach the commitment and briefing actions to a scheduler.

    With no `authority`, handlers return a `notify` payload rather than sending
    anything themselves, so delivery goes down the scheduler's existing path
    into the one notification authority. Quiet hours and rate limits therefore
    apply to a briefing exactly as they apply to every other scheduled notice.

    Pass `authority` when the caller wants a briefing recorded as spoken. That
    path delivers through `deliver()`, which can see whether the notice was
    genuinely `sent` and only then marks the commitments briefed. It is the same
    single notification authority either way; the difference is whether anyone
    is in a position to hear the answer.

    `clock` exists because every other function in this module takes `now` and
    these handlers are the one place that would otherwise reach for the wall
    clock. Without the seam, whether a scheduled briefing notifies depends on
    what time it happens to be when the suite runs, which is a coin flip
    dressed up as a test.
    """

    from core.commitment_sources import ingest
    from core.serena_scheduler import ActionOutcome

    resolved_rules = rules or InterruptionRules()
    source_list = list(sources) if sources is not None else None
    now = clock if clock is not None else time.time

    def _store() -> CommitmentStore:
        return store if store is not None else CommitmentStore()

    def _mode() -> str:
        if mode_reader is None:
            return "full"
        try:
            return str(mode_reader() or "full")
        except Exception:
            return "full"

    def ingest_action(payload: dict[str, Any]) -> ActionOutcome:
        report = ingest(_store(), source_list, actor="serena", now=now())
        detail = (
            f"{len(report.created)} new, {len(report.corrected)} corrected, "
            f"{len(report.unchanged)} unchanged"
        )
        if report.unavailable:
            detail += "; unavailable: " + ", ".join(sorted(report.unavailable))
        return ActionOutcome(True, detail, output=report.to_dict())

    def activate_action(payload: dict[str, Any]) -> ActionOutcome:
        promoted = _store().activate_due(now=now())
        return ActionOutcome(
            True,
            f"{len(promoted)} commitment{'s' if len(promoted) != 1 else ''} went active",
            output={"activated": [item.commitment_id for item in promoted]},
        )

    def _surface(
        briefing: Briefing,
        current: CommitmentStore,
        moment: float,
        channel: str,
        extra: dict[str, Any] | None = None,
    ) -> ActionOutcome:
        """Hand one briefing onward, marking it spoken only on proof it was.

        Two shapes, because the scheduler's own delivery path cannot report
        back: `SerenaScheduler._notify` suppresses every exception and discards
        the authority's result, so a handler that returns a `notify` payload
        learns nothing about whether he heard it. With an `authority` supplied
        this delivers directly and can see a real `sent`. Without one it still
        returns the payload for the scheduler to deliver, but records nothing,
        because a briefing that may never have arrived must stay eligible to be
        said again. The authority's own dedupe key absorbs the repeat.
        """

        output = {"briefing": briefing.to_dict(), **(extra or {})}
        if authority is None:
            return ActionOutcome(
                True,
                briefing.spoken,
                notify=briefing.notify_payload(channel=channel),
                output={**output, "marked_briefed": False, "delivery": "scheduler"},
            )
        _decision, result = deliver(
            briefing,
            authority=authority,
            channel=channel,
            rules=resolved_rules,
            now=moment,
            store=current,
        )
        sent = _was_sent(result)
        return ActionOutcome(
            True,
            briefing.spoken,
            output={
                **output,
                "marked_briefed": sent,
                "delivery": getattr(result, "decision", "unknown"),
            },
        )

    def _briefing_action(kind: str):
        def handler(payload: dict[str, Any]) -> ActionOutcome:
            current = _store()
            moment = now()
            builder = (
                build_morning_briefing if kind == "morning" else build_evening_briefing
            )
            briefing = builder(current, now=moment, mode=_mode())
            decision = resolved_rules.evaluate(briefing, now=moment)
            if not decision.interrupt:
                return ActionOutcome(
                    True, decision.reason, output=briefing.to_dict()
                )
            return _surface(
                briefing,
                current,
                moment,
                str(payload.get("channel") or "voice"),
            )

        return handler

    def pre_event_action(payload: dict[str, Any]) -> ActionOutcome:
        current = _store()
        moment = now()
        pending = pending_pre_event_briefings(current, now=moment, mode=_mode())
        if not pending:
            return ActionOutcome(True, "nothing lands soon enough to mention")
        # One heads-up per tick. Two events landing together is a briefing, and
        # firing several separate notices for it is exactly the nagging the
        # notification authority exists to prevent.
        return _surface(
            pending[0],
            current,
            moment,
            str(payload.get("channel") or "voice"),
            {"pending": len(pending)},
        )

    scheduler.register_action("serena.commitments.ingest", ingest_action)
    scheduler.register_action("serena.commitments.activate", activate_action)
    scheduler.register_action("serena.briefing.morning", _briefing_action("morning"))
    scheduler.register_action("serena.briefing.evening", _briefing_action("evening"))
    scheduler.register_action("serena.briefing.pre_event", pre_event_action)
    return scheduler


__all__ = [
    "BRIEFING_ACTIONS",
    "BRIEFING_KINDS",
    "Briefing",
    "InterruptionDecision",
    "InterruptionRules",
    "build_evening_briefing",
    "build_morning_briefing",
    "build_pre_event_briefing",
    "deliver",
    "pending_pre_event_briefings",
    "register_briefing_actions",
]
