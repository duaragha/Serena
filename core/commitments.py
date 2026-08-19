"""Durable commitments: the things Serena has agreed are owed, and by whom.

A memory says what is true. A commitment says what still has to happen, when,
and who carries it. Those are different objects with different lifecycles, so
this is a separate store rather than another record type bolted onto memory.
Markdown tasks stay readable and stay where they are; this reads them as one
input among several instead of replacing them.

What this refuses to do matters as much as what it does. It never sends
anything: it has no notifier, no channel, and no transport. Surfacing is
`core.briefings`, and delivery is the one notification authority, so quiet
hours and rate limits cannot be re-litigated here. It never deletes: a
correction is an append, and an abandoned commitment stays inspectable with the
reason it died. It never invents a state transition: the legal moves are a
fixed table, and an illegal one raises instead of quietly doing something
adjacent.

The correction log is the point of the whole design. Serena will get due dates,
owners, and priorities wrong when she reads them out of a calendar file or a
half-sentence he said out loud. What makes that recoverable is that every field
she set carries who set it, what it was before, and why, so he can look at a
wrong commitment and see exactly where the wrong idea entered.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_DB_PATH = Path.home() / ".local" / "state" / "serena" / "commitments.sqlite3"

# proposed:  Serena read it somewhere and thinks it is a commitment. Not his yet.
# accepted:  he said yes, or a standing rule accepted it. It is real but not live.
# active:    its due window is open; this is what "what do I owe right now" means.
# completed: done, terminal.
# abandoned: deliberately dropped, terminal, and it keeps its reason.
STATES = ("proposed", "accepted", "active", "completed", "abandoned")
TERMINAL_STATES = frozenset({"completed", "abandoned"})

# Only these moves exist. Anything else is a bug in the caller, not a state to
# be improvised at runtime.
_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"accepted", "active", "abandoned"}),
    "accepted": frozenset({"active", "completed", "abandoned"}),
    "active": frozenset({"completed", "abandoned", "accepted"}),
    "completed": frozenset(),
    "abandoned": frozenset(),
}

# Same vocabulary the shared Serena policy already uses for risk, so a caller
# never has to translate between two priority scales.
PRIORITIES = ("low", "normal", "high", "critical")
_PRIORITY_RANK = {name: index for index, name in enumerate(PRIORITIES)}

RECURRENCE_FREQUENCIES = ("daily", "weekdays", "weekly", "monthly", "yearly")

MAX_TITLE_CHARS = 300
MAX_DETAIL_CHARS = 4_000
MAX_REASON_CHARS = 1_000
MAX_ROWS = 1_000
# A pre-event briefing that fires three days out is not a heads-up, it is noise.
DEFAULT_LEAD_SECONDS = 1_800


class CommitmentError(ValueError):
    """The commitment request was not valid."""


@dataclass(frozen=True, slots=True)
class Recurrence:
    """How often this comes back, in the small vocabulary that covers real life."""

    frequency: str
    interval: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"frequency": self.frequency, "interval": self.interval}

    @classmethod
    def parse(cls, value: object) -> Recurrence | None:
        if value is None or value == "":
            return None
        if isinstance(value, Recurrence):
            data: dict[str, Any] = value.to_dict()
        elif isinstance(value, str):
            data = {"frequency": value}
        elif isinstance(value, dict):
            data = dict(value)
        else:
            raise CommitmentError("recurrence must be a string, mapping, or Recurrence")
        frequency = str(data.get("frequency") or "").strip().lower()
        if frequency in {"", "none", "once"}:
            return None
        if frequency not in RECURRENCE_FREQUENCIES:
            raise CommitmentError(
                f"unknown recurrence frequency {frequency!r}; "
                f"expected one of {', '.join(RECURRENCE_FREQUENCIES)}"
            )
        # `or 1` would read an explicit 0 as "unset" and quietly turn "never"
        # into "every day", which is the wrong direction to guess in.
        raw_interval = data.get("interval")
        if raw_interval is None:
            raw_interval = 1
        try:
            interval = int(raw_interval)
        except (TypeError, ValueError) as exc:
            raise CommitmentError("recurrence interval must be an integer") from exc
        if not 1 <= interval <= 365:
            raise CommitmentError("recurrence interval must be between 1 and 365")
        return cls(frequency=frequency, interval=interval)


def next_occurrence(due_at: float, recurrence: Recurrence | None) -> float | None:
    """The next due timestamp after ``due_at``, or None when it does not repeat.

    Calendar months and years are not fixed numbers of seconds, so this walks
    real dates. A monthly commitment on the 31st lands on the last day of a
    shorter month rather than silently skipping it.
    """

    if recurrence is None:
        return None
    moment = datetime.fromtimestamp(float(due_at))
    step = max(1, int(recurrence.interval))
    if recurrence.frequency == "daily":
        return (moment + timedelta(days=step)).timestamp()
    if recurrence.frequency == "weekly":
        return (moment + timedelta(weeks=step)).timestamp()
    if recurrence.frequency == "weekdays":
        candidate = moment
        for _ in range(step):
            candidate += timedelta(days=1)
            while candidate.weekday() >= 5:  # saturday, sunday
                candidate += timedelta(days=1)
        return candidate.timestamp()
    if recurrence.frequency == "monthly":
        return _add_months(moment, step).timestamp()
    if recurrence.frequency == "yearly":
        return _add_months(moment, 12 * step).timestamp()
    return None  # pragma: no cover - guarded by Recurrence.parse


def _add_months(moment: datetime, months: int) -> datetime:
    total = moment.month - 1 + months
    year = moment.year + total // 12
    month = total % 12 + 1
    day = min(moment.day, _days_in_month(year, month))
    return moment.replace(year=year, month=month, day=day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (datetime(year, month + 1, 1) - timedelta(days=1)).day


@dataclass(frozen=True, slots=True)
class Commitment:
    commitment_id: str
    title: str
    detail: str
    owner: str
    priority: str
    source: str
    source_ref: str
    state: str
    due_at: float | None
    recurrence: Recurrence | None
    lead_seconds: int
    snooze_until: float | None
    dismissed_at: float | None
    # The ws-3 state-graph entity this is about, when one is known. Free text on
    # purpose: the graph is a separate authority and may not be populated yet,
    # so a commitment must not require it to exist.
    subject_entity_id: str
    follow_up_of: str
    created_at: float
    updated_at: float
    completed_at: float | None
    abandoned_reason: str
    last_briefed_at: float | None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["recurrence"] = self.recurrence.to_dict() if self.recurrence else None
        return value

    @property
    def priority_rank(self) -> int:
        return _PRIORITY_RANK.get(self.priority, 1)

    def is_open(self) -> bool:
        return self.state not in TERMINAL_STATES

    def is_snoozed(self, now: float) -> bool:
        return self.snooze_until is not None and float(self.snooze_until) > float(now)

    def is_overdue(self, now: float) -> bool:
        return (
            self.is_open()
            and self.due_at is not None
            and float(self.due_at) < float(now)
            and not self.is_snoozed(now)
        )


@dataclass(frozen=True, slots=True)
class Correction:
    """One recorded change to a commitment, with who and why."""

    correction_id: str
    commitment_id: str
    field: str
    old_value: str
    new_value: str
    actor: str
    reason: str
    at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean(value: object, limit: int = MAX_TITLE_CHARS) -> str:
    return " ".join(str(value or "").split())[:limit]


def _timestamp(value: object, field: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CommitmentError(f"{field} must be a timestamp") from exc


def _priority(value: object) -> str:
    clean = _clean(value, 16).lower() or "normal"
    if clean not in PRIORITIES:
        raise CommitmentError(
            f"unknown priority {clean!r}; expected one of {', '.join(PRIORITIES)}"
        )
    return clean


class CommitmentStore:
    """Durable, correctable, append-audited commitments."""

    def __init__(self, path: Path | None = None) -> None:
        configured = os.environ.get("SERENA_COMMITMENTS_DB_PATH", "").strip()
        self.path = Path(path or configured or DEFAULT_DB_PATH).expanduser()
        self._initialize()

    # -- writes -------------------------------------------------------------

    def propose(
        self,
        *,
        title: str,
        actor: str,
        source: str,
        detail: str = "",
        owner: str = "raghav",
        priority: str = "normal",
        source_ref: str = "",
        due_at: object = None,
        recurrence: object = None,
        lead_seconds: int = DEFAULT_LEAD_SECONDS,
        subject_entity_id: str = "",
        follow_up_of: str = "",
        state: str = "proposed",
        now: float | None = None,
    ) -> Commitment:
        """Record a commitment Serena believes exists.

        Default state is `proposed` because most of these come from reading a
        calendar file or a passing sentence, and something Serena inferred is
        not yet something he agreed to. A caller with real authority, meaning he
        actually said it, may pass `state="accepted"`.
        """

        moment = float(time.time() if now is None else now)
        clean_title = _clean(title)
        if not clean_title:
            raise CommitmentError("a commitment needs a title")
        clean_actor = _clean(actor, 120)
        if not clean_actor:
            raise CommitmentError("recording a commitment requires a named actor")
        clean_source = _clean(source, 64)
        if not clean_source:
            raise CommitmentError("a commitment needs a source")
        if state not in {"proposed", "accepted"}:
            raise CommitmentError("a new commitment starts proposed or accepted")
        due = _timestamp(due_at, "due_at")
        repeat = Recurrence.parse(recurrence)
        if repeat is not None and due is None:
            raise CommitmentError("a recurring commitment needs a due date to repeat from")
        try:
            lead = int(lead_seconds)
        except (TypeError, ValueError) as exc:
            raise CommitmentError("lead_seconds must be an integer") from exc
        if not 0 <= lead <= 7 * 86_400:
            raise CommitmentError("lead_seconds must be between 0 and 7 days")

        commitment_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO commitments(
                    commitment_id, title, detail, owner, priority, source, source_ref,
                    state, due_at, recurrence_json, lead_seconds, snooze_until,
                    dismissed_at, subject_entity_id, follow_up_of, created_at,
                    updated_at, completed_at, abandoned_reason, last_briefed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, NULL, '', NULL)
                """,
                (
                    commitment_id,
                    clean_title,
                    _clean(detail, MAX_DETAIL_CHARS),
                    _clean(owner, 64) or "raghav",
                    _priority(priority),
                    clean_source,
                    _clean(source_ref, 256),
                    state,
                    due,
                    json.dumps(repeat.to_dict()) if repeat else None,
                    lead,
                    _clean(subject_entity_id, 128),
                    _clean(follow_up_of, 64),
                    moment,
                    moment,
                ),
            )
            self._append_correction(
                connection,
                commitment_id=commitment_id,
                field="state",
                old_value="",
                new_value=state,
                actor=clean_actor,
                reason=f"recorded from {clean_source}",
                at=moment,
            )
        return self.require(commitment_id)

    def transition(
        self,
        commitment_id: str,
        target: str,
        *,
        actor: str,
        reason: str = "",
        now: float | None = None,
    ) -> Commitment:
        """Move one commitment through the fixed state table.

        A completed recurring commitment does not resurrect itself in place. It
        stays completed as the historical record of that occurrence, and the
        caller gets a fresh commitment for the next one, so history stays
        readable rather than being overwritten every week.
        """

        moment = float(time.time() if now is None else now)
        clean_actor = _clean(actor, 120)
        if not clean_actor:
            raise CommitmentError("changing a commitment requires a named actor")
        if target not in STATES:
            raise CommitmentError(f"unknown commitment state {target!r}")
        current = self.require(commitment_id)
        if target not in _TRANSITIONS[current.state]:
            raise CommitmentError(
                f"a {current.state} commitment cannot become {target}"
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE commitments SET state = ?, updated_at = ?, "
                "completed_at = CASE WHEN ? THEN ? ELSE completed_at END, "
                "abandoned_reason = CASE WHEN ? THEN ? ELSE abandoned_reason END "
                "WHERE commitment_id = ?",
                (
                    target,
                    moment,
                    1 if target == "completed" else 0,
                    moment,
                    1 if target == "abandoned" else 0,
                    _clean(reason, MAX_REASON_CHARS),
                    commitment_id,
                ),
            )
            self._append_correction(
                connection,
                commitment_id=commitment_id,
                field="state",
                old_value=current.state,
                new_value=target,
                actor=clean_actor,
                reason=_clean(reason, MAX_REASON_CHARS),
                at=moment,
            )
        return self.require(commitment_id)

    def accept(self, commitment_id: str, *, actor: str, now: float | None = None) -> Commitment:
        return self.transition(
            commitment_id, "accepted", actor=actor, reason="accepted", now=now
        )

    def complete(
        self,
        commitment_id: str,
        *,
        actor: str,
        reason: str = "",
        now: float | None = None,
    ) -> tuple[Commitment, Commitment | None]:
        """Finish this occurrence, and schedule the next one when it repeats."""

        moment = float(time.time() if now is None else now)
        current = self.require(commitment_id)
        done = self.transition(
            commitment_id, "completed", actor=actor, reason=reason, now=moment
        )
        following = self._spawn_next(current, actor=actor, now=moment)
        return done, following

    def abandon(
        self,
        commitment_id: str,
        *,
        actor: str,
        reason: str,
        now: float | None = None,
    ) -> Commitment:
        clean_reason = _clean(reason, MAX_REASON_CHARS)
        if not clean_reason:
            raise CommitmentError("abandoning a commitment requires a reason")
        return self.transition(
            commitment_id, "abandoned", actor=actor, reason=clean_reason, now=now
        )

    def snooze(
        self,
        commitment_id: str,
        *,
        actor: str,
        until: object = None,
        seconds: float | None = None,
        reason: str = "",
        now: float | None = None,
    ) -> Commitment:
        """Go quiet about this until a stated time. The commitment still stands.

        Snooze is deliberately not a state. He is not saying the thing is done
        or dropped, he is saying stop talking about it for a while, and blurring
        those together is how an assistant loses track of what is actually owed.
        """

        moment = float(time.time() if now is None else now)
        current = self.require(commitment_id)
        if not current.is_open():
            raise CommitmentError(f"a {current.state} commitment cannot be snoozed")
        target = _timestamp(until, "until")
        if target is None:
            if seconds is None:
                raise CommitmentError("snooze needs either an until time or seconds")
            target = moment + float(seconds)
        if target <= moment:
            raise CommitmentError("snooze must point at a future time")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE commitments SET snooze_until = ?, updated_at = ? "
                "WHERE commitment_id = ?",
                (target, moment, commitment_id),
            )
            self._append_correction(
                connection,
                commitment_id=commitment_id,
                field="snooze_until",
                old_value=_render_time(current.snooze_until),
                new_value=_render_time(target),
                actor=_clean(actor, 120) or "serena",
                reason=_clean(reason, MAX_REASON_CHARS) or "snoozed",
                at=moment,
            )
        return self.require(commitment_id)

    def dismiss(
        self,
        commitment_id: str,
        *,
        actor: str,
        reason: str = "",
        now: float | None = None,
    ) -> tuple[Commitment, Commitment | None]:
        """Drop this occurrence without claiming it was done.

        For something that repeats, the honest reading of "not this one" is that
        the series continues, so the next occurrence is created and this one
        stops surfacing. For a one-off it stays open and inspectable but goes
        quiet, because dismissing a reminder is not the same as deciding the
        obligation no longer exists.
        """

        moment = float(time.time() if now is None else now)
        current = self.require(commitment_id)
        if not current.is_open():
            raise CommitmentError(f"a {current.state} commitment cannot be dismissed")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE commitments SET dismissed_at = ?, updated_at = ? "
                "WHERE commitment_id = ?",
                (moment, moment, commitment_id),
            )
            self._append_correction(
                connection,
                commitment_id=commitment_id,
                field="dismissed_at",
                old_value=_render_time(current.dismissed_at),
                new_value=_render_time(moment),
                actor=_clean(actor, 120) or "raghav",
                reason=_clean(reason, MAX_REASON_CHARS) or "dismissed",
                at=moment,
            )
        following = self._spawn_next(current, actor=actor, now=moment)
        return self.require(commitment_id), following

    def follow_up(
        self,
        commitment_id: str,
        *,
        title: str,
        actor: str,
        due_at: object = None,
        priority: str | None = None,
        detail: str = "",
        now: float | None = None,
    ) -> Commitment:
        """Spawn the next thing this commitment turned out to need."""

        parent = self.require(commitment_id)
        return self.propose(
            title=title,
            actor=actor,
            source="follow_up",
            source_ref=parent.commitment_id,
            detail=detail,
            owner=parent.owner,
            priority=priority or parent.priority,
            due_at=due_at,
            lead_seconds=parent.lead_seconds,
            subject_entity_id=parent.subject_entity_id,
            follow_up_of=parent.commitment_id,
            state="accepted",
            now=now,
        )

    def correct(
        self,
        commitment_id: str,
        *,
        actor: str,
        reason: str = "",
        now: float | None = None,
        **fields: object,
    ) -> Commitment:
        """Change the facts Serena got wrong, keeping what she thought before.

        This is the write side of the correction surface. Every accepted field
        lands in the audit log with its previous value, so a wrong due date is
        traceable to the moment and the actor that introduced it.
        """

        moment = float(time.time() if now is None else now)
        clean_actor = _clean(actor, 120)
        if not clean_actor:
            raise CommitmentError("correcting a commitment requires a named actor")
        current = self.require(commitment_id)
        if not current.is_open():
            raise CommitmentError(f"a {current.state} commitment cannot be corrected")

        editable = {
            "title": lambda value: _clean(value) or _raise("a commitment needs a title"),
            "detail": lambda value: _clean(value, MAX_DETAIL_CHARS),
            "owner": lambda value: _clean(value, 64) or "raghav",
            "priority": _priority,
            "due_at": lambda value: _timestamp(value, "due_at"),
            "subject_entity_id": lambda value: _clean(value, 128),
        }
        unknown = set(fields) - set(editable) - {"recurrence", "lead_seconds"}
        if unknown:
            raise CommitmentError(
                f"cannot correct {', '.join(sorted(unknown))} on a commitment"
            )

        updates: list[tuple[str, object, str, str]] = []
        for name, convert in editable.items():
            if name not in fields:
                continue
            new_value = convert(fields[name])
            old_value = getattr(current, name)
            if new_value == old_value:
                continue
            rendered_old = (
                _render_time(old_value) if name == "due_at" else str(old_value or "")
            )
            rendered_new = (
                _render_time(new_value) if name == "due_at" else str(new_value or "")
            )
            updates.append((name, new_value, rendered_old, rendered_new))
        if "recurrence" in fields:
            repeat = Recurrence.parse(fields["recurrence"])
            if repeat != current.recurrence:
                updates.append(
                    (
                        "recurrence_json",
                        json.dumps(repeat.to_dict()) if repeat else None,
                        json.dumps(current.recurrence.to_dict())
                        if current.recurrence
                        else "",
                        json.dumps(repeat.to_dict()) if repeat else "",
                    )
                )
        if "lead_seconds" in fields:
            try:
                lead = int(fields["lead_seconds"])  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise CommitmentError("lead_seconds must be an integer") from exc
            if not 0 <= lead <= 7 * 86_400:
                raise CommitmentError("lead_seconds must be between 0 and 7 days")
            if lead != current.lead_seconds:
                updates.append(
                    ("lead_seconds", lead, str(current.lead_seconds), str(lead))
                )

        # `propose` refuses a recurrence with no due date to repeat from, and a
        # correction has to obey the same rule or the invariant only holds for
        # commitments nobody ever edited. Both halves are checked together
        # because either field alone can break the pair: adding a recurrence to
        # a commitment with no date, or clearing the date out from under an
        # existing recurrence. `next_occurrence` would otherwise be asked to
        # step forward from nothing.
        resulting_due = (
            _timestamp(fields["due_at"], "due_at") if "due_at" in fields else current.due_at
        )
        resulting_recurrence = (
            Recurrence.parse(fields["recurrence"])
            if "recurrence" in fields
            else current.recurrence
        )
        if resulting_recurrence is not None and resulting_due is None:
            raise CommitmentError(
                "a recurring commitment needs a due date to repeat from; set due_at "
                "in the same correction or clear the recurrence"
            )

        if not updates:
            return current

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            assignments = ", ".join(f"{column} = ?" for column, _, _, _ in updates)
            connection.execute(
                f"UPDATE commitments SET {assignments}, updated_at = ? "
                "WHERE commitment_id = ?",
                (*[value for _, value, _, _ in updates], moment, commitment_id),
            )
            for column, _value, rendered_old, rendered_new in updates:
                self._append_correction(
                    connection,
                    commitment_id=commitment_id,
                    field="recurrence" if column == "recurrence_json" else column,
                    old_value=rendered_old,
                    new_value=rendered_new,
                    actor=clean_actor,
                    reason=_clean(reason, MAX_REASON_CHARS) or "corrected",
                    at=moment,
                )
        return self.require(commitment_id)

    def mark_briefed(
        self, commitment_id: str, *, now: float | None = None
    ) -> None:
        """Remember this was already spoken about, so a briefing stops repeating."""

        moment = float(time.time() if now is None else now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE commitments SET last_briefed_at = ? WHERE commitment_id = ?",
                (moment, commitment_id),
            )

    def activate_due(self, *, now: float | None = None, actor: str = "serena") -> list[Commitment]:
        """Promote accepted commitments whose due window has opened.

        Only `accepted` moves. A `proposed` commitment going live on a timer
        would mean Serena promoted her own guess into an obligation without him
        ever agreeing to it.
        """

        moment = float(time.time() if now is None else now)
        promoted: list[Commitment] = []
        for item in self.list(state="accepted"):
            if item.due_at is None:
                continue
            if float(item.due_at) - item.lead_seconds > moment:
                continue
            promoted.append(
                self.transition(
                    item.commitment_id,
                    "active",
                    actor=actor,
                    reason="due window opened",
                    now=moment,
                )
            )
        return promoted

    def _spawn_next(
        self, current: Commitment, *, actor: str, now: float
    ) -> Commitment | None:
        if current.recurrence is None or current.due_at is None:
            return None
        following = next_occurrence(float(current.due_at), current.recurrence)
        if following is None:  # pragma: no cover - guarded by Recurrence.parse
            return None
        # A commitment that was missed for several cycles should come back at the
        # next real future date, not immediately fire a backlog of stale ones.
        while following <= now:
            stepped = next_occurrence(following, current.recurrence)
            if stepped is None or stepped <= following:  # pragma: no cover - defensive
                break
            following = stepped
        return self.propose(
            title=current.title,
            actor=actor,
            source=current.source,
            source_ref=current.source_ref,
            detail=current.detail,
            owner=current.owner,
            priority=current.priority,
            due_at=following,
            recurrence=current.recurrence,
            lead_seconds=current.lead_seconds,
            subject_entity_id=current.subject_entity_id,
            follow_up_of=current.commitment_id,
            state="accepted",
            now=now,
        )

    # -- reads --------------------------------------------------------------

    def get(self, commitment_id: str) -> Commitment | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM commitments WHERE commitment_id = ?", (commitment_id,)
            ).fetchone()
        return _commitment(row) if row is not None else None

    def require(self, commitment_id: str) -> Commitment:
        found = self.get(commitment_id)
        if found is None:
            raise KeyError(f"unknown commitment {commitment_id}")
        return found

    def list(
        self,
        *,
        state: str | None = None,
        owner: str | None = None,
        source: str | None = None,
        open_only: bool = False,
        limit: int = MAX_ROWS,
    ) -> list[Commitment]:
        clauses: list[str] = []
        params: list[object] = []
        if state is not None:
            if state not in STATES:
                raise CommitmentError(f"unknown commitment state {state!r}")
            clauses.append("state = ?")
            params.append(state)
        if open_only:
            clauses.append("state NOT IN ('completed', 'abandoned')")
        if owner is not None:
            clauses.append("owner = ?")
            params.append(_clean(owner, 64))
        if source is not None:
            clauses.append("source = ?")
            params.append(_clean(source, 64))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(min(MAX_ROWS, max(1, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM commitments" + where
                + " ORDER BY due_at IS NULL, due_at, created_at LIMIT ?",
                tuple(params),
            ).fetchall()
        return [_commitment(row) for row in rows]

    def find_by_source(self, source: str, source_ref: str) -> Commitment | None:
        """The idempotency key for ingestion: one source row, one commitment."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM commitments WHERE source = ? AND source_ref = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (_clean(source, 64), _clean(source_ref, 256)),
            ).fetchone()
        return _commitment(row) if row is not None else None

    def due(
        self,
        *,
        now: float | None = None,
        horizon_seconds: float = 86_400,
        include_snoozed: bool = False,
        include_dismissed: bool = False,
    ) -> list[Commitment]:
        """Open commitments whose due date lands inside the horizon, plus overdue."""

        moment = float(time.time() if now is None else now)
        ceiling = moment + float(horizon_seconds)
        selected: list[Commitment] = []
        for item in self.list(open_only=True):
            if item.due_at is None or float(item.due_at) > ceiling:
                continue
            if not include_snoozed and item.is_snoozed(moment):
                continue
            if not include_dismissed and item.dismissed_at is not None:
                continue
            selected.append(item)
        return sorted(selected, key=lambda item: (float(item.due_at or 0.0), -item.priority_rank))

    def overdue(self, *, now: float | None = None) -> list[Commitment]:
        moment = float(time.time() if now is None else now)
        return [
            item
            for item in self.list(open_only=True)
            if item.is_overdue(moment) and item.dismissed_at is None
        ]

    def corrections(self, commitment_id: str | None = None, limit: int = 200) -> list[Correction]:
        """The audit trail behind the correction surface."""

        clauses: list[str] = []
        params: list[object] = []
        if commitment_id:
            clauses.append("commitment_id = ?")
            params.append(commitment_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(min(MAX_ROWS, max(1, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM commitment_corrections" + where
                + " ORDER BY at DESC, rowid DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [
            Correction(
                correction_id=str(row["correction_id"]),
                commitment_id=str(row["commitment_id"]),
                field=str(row["field"]),
                old_value=str(row["old_value"] or ""),
                new_value=str(row["new_value"] or ""),
                actor=str(row["actor"]),
                reason=str(row["reason"] or ""),
                at=float(row["at"]),
            )
            for row in rows
        ]

    def inspect(self, commitment_id: str) -> dict[str, Any]:
        """One commitment plus the full history of how it got that way."""

        item = self.require(commitment_id)
        return {
            "commitment": item.to_dict(),
            "corrections": [entry.to_dict() for entry in self.corrections(commitment_id)],
        }

    # -- internals ----------------------------------------------------------

    def _append_correction(
        self,
        connection: sqlite3.Connection,
        *,
        commitment_id: str,
        field: str,
        old_value: str,
        new_value: str,
        actor: str,
        reason: str,
        at: float,
    ) -> None:
        connection.execute(
            "INSERT INTO commitment_corrections("
            "correction_id, commitment_id, field, old_value, new_value, actor, reason, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                commitment_id,
                _clean(field, 64),
                _clean(old_value, MAX_DETAIL_CHARS),
                _clean(new_value, MAX_DETAIL_CHARS),
                _clean(actor, 120),
                _clean(reason, MAX_REASON_CHARS),
                float(at),
            ),
        )

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
                CREATE TABLE IF NOT EXISTS commitments (
                    commitment_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    owner TEXT NOT NULL DEFAULT 'raghav',
                    priority TEXT NOT NULL DEFAULT 'normal',
                    source TEXT NOT NULL,
                    source_ref TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    due_at REAL,
                    recurrence_json TEXT,
                    lead_seconds INTEGER NOT NULL DEFAULT 1800,
                    snooze_until REAL,
                    dismissed_at REAL,
                    subject_entity_id TEXT NOT NULL DEFAULT '',
                    follow_up_of TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL,
                    abandoned_reason TEXT NOT NULL DEFAULT '',
                    last_briefed_at REAL
                );
                CREATE INDEX IF NOT EXISTS commitments_state_idx
                    ON commitments(state, due_at);
                CREATE INDEX IF NOT EXISTS commitments_source_idx
                    ON commitments(source, source_ref);
                CREATE TABLE IF NOT EXISTS commitment_corrections (
                    correction_id TEXT PRIMARY KEY,
                    commitment_id TEXT NOT NULL,
                    field TEXT NOT NULL,
                    old_value TEXT NOT NULL DEFAULT '',
                    new_value TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS commitment_corrections_idx
                    ON commitment_corrections(commitment_id, at);
                CREATE TABLE IF NOT EXISTS commitment_schema (
                    version INTEGER NOT NULL
                );
                """
            )
            # Additive migrations only, matching the scheduler's rule: an older
            # database keeps every row it had and new columns arrive with
            # defaults that mean "behave exactly like before".
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(commitments)").fetchall()
            }
            for name, definition in (
                ("subject_entity_id", "TEXT NOT NULL DEFAULT ''"),
                ("follow_up_of", "TEXT NOT NULL DEFAULT ''"),
                ("dismissed_at", "REAL"),
                ("last_briefed_at", "REAL"),
                ("lead_seconds", "INTEGER NOT NULL DEFAULT 1800"),
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE commitments ADD COLUMN {name} {definition}"
                    )
            row = connection.execute("SELECT version FROM commitment_schema").fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO commitment_schema(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif int(row["version"]) != SCHEMA_VERSION:
                connection.execute(
                    "UPDATE commitment_schema SET version = ?", (SCHEMA_VERSION,)
                )
        if os.name != "nt":
            with suppress(OSError):
                self.path.chmod(0o600)


def _raise(message: str) -> str:
    raise CommitmentError(message)


def _render_time(value: object) -> str:
    if value is None or value == "":
        return ""
    try:
        return datetime.fromtimestamp(float(value)).isoformat(timespec="minutes")  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value)


def _commitment(row: sqlite3.Row) -> Commitment:
    raw_recurrence = row["recurrence_json"]
    recurrence: Recurrence | None = None
    if raw_recurrence:
        with suppress(json.JSONDecodeError, CommitmentError):
            recurrence = Recurrence.parse(json.loads(str(raw_recurrence)))
    return Commitment(
        commitment_id=str(row["commitment_id"]),
        title=str(row["title"]),
        detail=str(row["detail"] or ""),
        owner=str(row["owner"]),
        priority=str(row["priority"]),
        source=str(row["source"]),
        source_ref=str(row["source_ref"] or ""),
        state=str(row["state"]),
        due_at=row["due_at"],
        recurrence=recurrence,
        lead_seconds=int(row["lead_seconds"] or DEFAULT_LEAD_SECONDS),
        snooze_until=row["snooze_until"],
        dismissed_at=row["dismissed_at"],
        subject_entity_id=str(row["subject_entity_id"] or ""),
        follow_up_of=str(row["follow_up_of"] or ""),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        completed_at=row["completed_at"],
        abandoned_reason=str(row["abandoned_reason"] or ""),
        last_briefed_at=row["last_briefed_at"],
    )
