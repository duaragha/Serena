"""A bounded scheduler for the small number of things Serena does on a clock.

The point of this module is what it refuses to do. It does not run shell
commands, import plugin code, or accept a callable from a manifest. A schedule
names one action from a fixed, reviewed registry, and the handler for that
action is ordinary Serena code with a test. That is the whole extensibility
story: plugins can ask for a schedule, they cannot become one.

Everything user-facing goes out through the notification authority, so quiet
hours, deduplication, and rate limits are enforced in exactly one place rather
than reimplemented per job.

Bounded means bounded: a fixed interval floor, a per-run action cap, a
consecutive-failure circuit breaker that disables a misbehaving schedule, and
durable history for every run.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_DB_PATH = Path.home() / ".local" / "state" / "serena" / "scheduler.sqlite3"

MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 30 * 86_400
MAX_ACTIONS_PER_TICK = 25
MAX_CONSECUTIVE_FAILURES = 5
ACTION_LEASE_SECONDS = 15 * 60
SCHEDULE_STATES = (
    "pending_approval",
    "active",
    "paused",
    "disabled",
    # A one-shot job that already ran, and a schedule Raghav removed. Both are
    # terminal and neither is deleted, so history stays readable.
    "completed",
    "removed",
)
TERMINAL_STATES = ("completed", "removed")

# Fan-out has to be bounded or one misconfigured chain becomes a fork bomb on a
# timer. A schedule may wake a small number of successors, each successor is
# woken at most once per tick, and a chain may not run deeper than this.
MAX_CHAIN_FANOUT = 8
MAX_CHAIN_DEPTH = 4

ActionHandler = Callable[[dict[str, Any]], "ActionOutcome"]


class SchedulerError(ValueError):
    """The schedule request was not valid."""


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    ok: bool
    detail: str = ""
    notify: dict[str, Any] | None = None
    # What this run hands to whatever it chains into. Bounded and JSON-only:
    # a chained output is data, never a callable and never code.
    output: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ScheduleRun:
    schedule_id: str
    action: str
    ok: bool
    detail: str
    ran_at: float


def _clean(value: object, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


def _interval(value: object) -> int:
    try:
        interval = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise SchedulerError("interval_seconds must be an integer") from exc
    if not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS:
        raise SchedulerError(
            f"interval_seconds must be between {MIN_INTERVAL_SECONDS} "
            f"and {MAX_INTERVAL_SECONDS}"
        )
    return interval


def _workdir(value: object) -> str | None:
    """A real, existing directory or nothing at all.

    A schedule that names a project runs *in* that project. Accepting a path
    that does not exist would mean discovering it at 3am inside a handler, so
    it is checked when the schedule is written instead.
    """

    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise SchedulerError("a schedule workdir must be an absolute path")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise SchedulerError(f"schedule workdir does not exist: {resolved}")
    return str(resolved)


def _chain_ids(value: object, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raise SchedulerError(f"{field} must be a list of schedule ids")
    ids: list[str] = []
    for entry in value:
        clean = _clean(entry, 64)
        if clean and clean not in ids:
            ids.append(clean)
    if len(ids) > MAX_CHAIN_FANOUT:
        raise SchedulerError(
            f"{field} may name at most {MAX_CHAIN_FANOUT} schedules"
        )
    return ids


class SerenaScheduler:
    """Durable, bounded schedule store and tick loop."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        handlers: dict[str, ActionHandler] | None = None,
        notifier: Any | None = None,
    ) -> None:
        configured = os.environ.get("SERENA_SCHEDULER_DB_PATH", "").strip()
        self.path = Path(path or configured or DEFAULT_DB_PATH).expanduser()
        self._handlers: dict[str, ActionHandler] = dict(handlers or {})
        self._notifier = notifier
        self._initialize()

    # -- registration -------------------------------------------------------

    def register_action(self, action: str, handler: ActionHandler) -> None:
        """Register a reviewed handler. Only registered actions can be scheduled."""

        name = _clean(action, 64)
        if not name:
            raise SchedulerError("an action needs a name")
        self._handlers[name] = handler

    @property
    def actions(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    def add_schedule(
        self,
        *,
        action: str,
        interval_seconds: int,
        actor: str,
        payload: dict[str, Any] | None = None,
        requires_approval: bool = True,
        owner: str = "serena",
        first_run_at: float | None = None,
        now: float | None = None,
        one_shot: bool = False,
        workdir: str | Path | None = None,
        chain_to: Sequence[str] = (),
        join_of: Sequence[str] = (),
        dedupe_key: str = "",
        dedupe_window_seconds: int = 0,
    ) -> dict[str, Any]:
        """Add one schedule. Unknown actions are refused, not stored for later."""

        moment = float(time.time() if now is None else now)
        name = _clean(action, 64)
        if name not in self._handlers:
            raise SchedulerError(
                f"unknown scheduler action {name!r}; only reviewed actions can be scheduled"
            )
        if not _clean(actor, 120):
            raise SchedulerError("adding a schedule requires a named actor")
        interval = _interval(interval_seconds)
        resolved_workdir = _workdir(workdir)
        fanout = _chain_ids(chain_to, "chain_to")
        join = _chain_ids(join_of, "join_of")
        schedule_id = str(uuid.uuid4())
        if schedule_id in fanout or schedule_id in join:  # pragma: no cover - uuid
            raise SchedulerError("a schedule cannot chain to itself")
        state = "pending_approval" if requires_approval else "active"
        next_run = float(first_run_at if first_run_at is not None else moment + interval)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO schedules(
                    schedule_id, action, payload_json, interval_seconds, state, owner,
                    actor, next_run_at, consecutive_failures, created_at, updated_at,
                    one_shot, workdir, chain_to_json, join_of_json, dedupe_key,
                    dedupe_window_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    schedule_id,
                    name,
                    json.dumps(payload or {}, default=str)[:16_000],
                    interval,
                    state,
                    _clean(owner, 64) or "serena",
                    _clean(actor, 120),
                    next_run,
                    moment,
                    moment,
                    1 if one_shot else 0,
                    resolved_workdir,
                    json.dumps(fanout),
                    json.dumps(join),
                    _clean(dedupe_key, 256),
                    max(0, int(dedupe_window_seconds or 0)),
                ),
            )
        return self.require(schedule_id)

    def edit(
        self,
        schedule_id: str,
        *,
        actor: str,
        interval_seconds: int | None = None,
        payload: dict[str, Any] | None = None,
        workdir: str | Path | None = None,
        chain_to: Sequence[str] | None = None,
        join_of: Sequence[str] | None = None,
        one_shot: bool | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Change a schedule's shape without changing what it is allowed to do.

        The action is deliberately not editable. Swapping the action of an
        approved schedule would launder an unreviewed action through an old
        approval, so a different action is a different schedule.
        """

        moment = float(time.time() if now is None else now)
        named_actor = _clean(actor, 120)
        if not named_actor:
            raise SchedulerError("editing a schedule requires a named actor")
        record = self.require(schedule_id)
        if record["state"] in TERMINAL_STATES:
            raise SchedulerError(f"a {record['state']} schedule cannot be edited")

        updates: list[str] = []
        params: list[object] = []
        if interval_seconds is not None:
            interval = _interval(interval_seconds)
            updates.append("interval_seconds = ?")
            params.append(interval)
        if payload is not None:
            updates.append("payload_json = ?")
            params.append(json.dumps(payload, default=str)[:16_000])
        if workdir is not None:
            updates.append("workdir = ?")
            params.append(_workdir(workdir))
        if chain_to is not None:
            fanout = _chain_ids(chain_to, "chain_to")
            if schedule_id in fanout:
                raise SchedulerError("a schedule cannot chain to itself")
            updates.append("chain_to_json = ?")
            params.append(json.dumps(fanout))
        if join_of is not None:
            join = _chain_ids(join_of, "join_of")
            if schedule_id in join:
                raise SchedulerError("a schedule cannot wait on itself")
            updates.append("join_of_json = ?")
            params.append(json.dumps(join))
        if one_shot is not None:
            updates.append("one_shot = ?")
            params.append(1 if one_shot else 0)
        if not updates:
            return record

        updates.append("updated_at = ?")
        params.append(moment)
        params.append(schedule_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"UPDATE schedules SET {', '.join(updates)} WHERE schedule_id = ?",
                tuple(params),
            )
            connection.execute(
                "INSERT INTO schedule_runs(run_id, schedule_id, action, ok, detail, ran_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (
                    str(uuid.uuid4()),
                    schedule_id,
                    record["action"],
                    f"edited by {named_actor}",
                    moment,
                ),
            )
        return self.require(schedule_id)

    def remove(
        self, schedule_id: str, *, actor: str, now: float | None = None
    ) -> dict[str, Any]:
        """Retire a schedule for good, keeping its history intact.

        Removal is a state, not a DELETE. Raghav should still be able to ask
        what a job did before he turned it off.
        """

        moment = float(time.time() if now is None else now)
        named_actor = _clean(actor, 120)
        if not named_actor:
            raise SchedulerError("removing a schedule requires a named actor")
        record = self.require(schedule_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE schedules SET state = 'removed', claim_token = NULL, "
                "claim_expires_at = NULL, updated_at = ? WHERE schedule_id = ?",
                (moment, schedule_id),
            )
            connection.execute(
                "INSERT INTO schedule_runs(run_id, schedule_id, action, ok, detail, ran_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (
                    str(uuid.uuid4()),
                    schedule_id,
                    record["action"],
                    f"removed by {named_actor}",
                    moment,
                ),
            )
        return self.require(schedule_id)

    def approve(self, schedule_id: str, *, actor: str, now: float | None = None) -> dict[str, Any]:
        moment = float(time.time() if now is None else now)
        if not _clean(actor, 120):
            raise SchedulerError("approving a schedule requires a named actor")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE schedules SET state = 'active', approved_by = ?, updated_at = ? "
                "WHERE schedule_id = ? AND state = 'pending_approval'",
                (_clean(actor, 120), moment, schedule_id),
            )
            if not cursor.rowcount:
                raise SchedulerError(f"no schedule awaiting approval: {schedule_id}")
        return self.require(schedule_id)

    def set_state(self, schedule_id: str, state: str) -> dict[str, Any]:
        if state not in SCHEDULE_STATES:
            raise SchedulerError(f"unknown schedule state {state!r}")
        if state in {"pending_approval", "active"}:
            raise SchedulerError(
                "pending or active state requires the explicit approve/resume transition"
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE schedules SET state = ?, updated_at = ? WHERE schedule_id = ?",
                (state, time.time(), schedule_id),
            )
            if not cursor.rowcount:
                raise KeyError(f"unknown schedule {schedule_id}")
        return self.require(schedule_id)

    def resume(
        self, schedule_id: str, *, actor: str, now: float | None = None
    ) -> dict[str, Any]:
        """Explicitly reactivate a paused/disabled schedule with an audit actor."""

        moment = float(time.time() if now is None else now)
        named_actor = _clean(actor, 120)
        if not named_actor:
            raise SchedulerError("resuming a schedule requires a named actor")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE schedules SET state = 'active', approved_by = COALESCE(approved_by, ?), "
                "consecutive_failures = 0, claim_token = NULL, claim_expires_at = NULL, "
                "updated_at = ? "
                "WHERE schedule_id = ? AND state IN ('paused', 'disabled')",
                (named_actor, moment, schedule_id),
            )
            if not cursor.rowcount:
                raise SchedulerError(f"no paused or disabled schedule to resume: {schedule_id}")
        return self.require(schedule_id)

    # -- execution ----------------------------------------------------------

    def due(self, *, now: float | None = None, limit: int = MAX_ACTIONS_PER_TICK) -> list[dict[str, Any]]:
        moment = float(time.time() if now is None else now)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM schedules WHERE state = 'active' AND next_run_at <= ? "
                "AND (claim_expires_at IS NULL OR claim_expires_at <= ?) "
                "ORDER BY next_run_at LIMIT ?",
                (moment, moment, min(MAX_ACTIONS_PER_TICK, max(1, int(limit)))),
            ).fetchall()
        ready = [_schedule_dict(row) for row in rows]
        return [item for item in ready if self._join_satisfied(item)]

    def _join_satisfied(self, schedule: dict[str, Any]) -> bool:
        """Fan-in: every named parent must have succeeded since our last run.

        This is a plain predicate rather than a coordinator, which is what
        makes it safe. Nothing is held open waiting, nothing blocks a worker,
        and a parent that never succeeds simply means this never becomes due.
        """

        parents = list(schedule.get("join_of") or [])
        if not parents:
            return True
        since = float(schedule.get("last_run_at") or 0.0)
        placeholders = ",".join("?" for _ in parents)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT schedule_id, MAX(last_success_at) AS success FROM schedules "
                f"WHERE schedule_id IN ({placeholders}) GROUP BY schedule_id",
                tuple(parents),
            ).fetchall()
        succeeded = {
            str(row["schedule_id"]): float(row["success"] or 0.0) for row in rows
        }
        return all(succeeded.get(parent, 0.0) > since for parent in parents)

    def tick(self, *, now: float | None = None, limit: int = MAX_ACTIONS_PER_TICK) -> list[ScheduleRun]:
        """Run every due schedule once, bounded by the per-tick cap.

        Chained successors are woken, not called. A fan-out marks its targets
        due and the *next* pass runs them, so a chain can never recurse inside
        one tick and the per-tick cap keeps meaning what it says.
        """

        moment = float(time.time() if now is None else now)
        runs: list[ScheduleRun] = []
        woken: set[str] = set()
        for schedule in self.due(now=moment, limit=limit):
            claimed = self._run_one(schedule, moment, woken=woken)
            if claimed is not None:
                runs.append(claimed)
        with suppress(Exception):
            from core.plugin_loader import emit_plugin_hook

            emit_plugin_hook(
                "schedule.tick",
                {
                    "run_count": len(runs),
                    "succeeded": sum(1 for run in runs if run.ok),
                    "failed": sum(1 for run in runs if not run.ok),
                },
            )
        return runs

    def drain(
        self,
        *,
        now: float | None = None,
        limit: int = MAX_ACTIONS_PER_TICK,
        max_passes: int = MAX_CHAIN_DEPTH,
    ) -> list[ScheduleRun]:
        """Tick repeatedly so a chain finishes, bounded by depth.

        The resident loop uses this so a fan-out completes promptly instead of
        one link per poll interval. It stops at `max_passes` no matter what, so
        a cycle costs a bounded number of runs rather than the machine.
        """

        moment = float(time.time() if now is None else now)
        runs: list[ScheduleRun] = []
        for _ in range(max(1, int(max_passes))):
            pass_runs = self.tick(now=moment, limit=limit)
            if not pass_runs:
                break
            runs.extend(pass_runs)
        return runs

    def run_now(self, schedule_id: str, *, now: float | None = None) -> ScheduleRun:
        moment = float(time.time() if now is None else now)
        schedule = self.require(schedule_id)
        if schedule["state"] != "active":
            raise SchedulerError("run-now is allowed only for an active schedule")
        result = self._run_one(schedule, moment, reschedule=False, require_due=False)
        if result is None:
            raise SchedulerError("schedule is already running")
        return result

    def _run_one(
        self,
        schedule: dict[str, Any],
        moment: float,
        *,
        reschedule: bool = True,
        require_due: bool = True,
        woken: set[str] | None = None,
    ) -> ScheduleRun | None:
        schedule_id = str(schedule["schedule_id"])
        claimed = self._claim(schedule_id, moment, require_due=require_due)
        if claimed is None:
            return None
        schedule, claim_token = claimed
        action = str(schedule["action"])

        duplicate = self._duplicate_run(schedule, moment)
        if duplicate is not None:
            outcome = ActionOutcome(True, duplicate)
        else:
            handler = self._handlers.get(action)
            if handler is None:
                outcome = ActionOutcome(False, f"no handler is registered for {action}")
            else:
                try:
                    outcome = handler(self._handler_payload(schedule))
                    if not isinstance(outcome, ActionOutcome):
                        outcome = ActionOutcome(
                            bool(outcome), "handler returned a non-outcome value"
                        )
                except Exception as exc:
                    outcome = ActionOutcome(False, _clean(exc) or "handler raised")

        if outcome.ok and outcome.notify:
            self._notify(schedule_id, action, outcome.notify)

        failures = 0 if outcome.ok else int(schedule.get("consecutive_failures") or 0) + 1
        state = str(schedule["state"])
        if failures >= MAX_CONSECUTIVE_FAILURES:
            # A schedule that keeps failing is a broken schedule, not a reason
            # to keep retrying it every interval forever.
            state = "disabled"
        one_shot = bool(schedule.get("one_shot"))
        if outcome.ok and one_shot:
            # It was asked for once. It happened. It does not linger as a timer.
            # This is deliberately independent of `reschedule`: running it early
            # by hand is still the one time it was asked for, so `run-now` must
            # retire it exactly like a due tick would.
            state = "completed"
        next_run = (
            moment + int(schedule["interval_seconds"])
            if reschedule
            else float(schedule["next_run_at"])
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE schedules SET next_run_at = ?, last_run_at = ?, "
                "consecutive_failures = ?, "
                "state = CASE WHEN state = 'active' THEN ? ELSE state END, "
                "last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END, "
                "last_output_json = CASE WHEN ? THEN ? ELSE last_output_json END, "
                "claim_token = NULL, "
                "claim_expires_at = NULL, updated_at = ? "
                "WHERE schedule_id = ? AND claim_token = ?",
                (
                    next_run,
                    moment,
                    failures,
                    state,
                    1 if outcome.ok else 0,
                    moment,
                    1 if outcome.ok else 0,
                    json.dumps(outcome.output or {}, default=str)[:16_000],
                    moment,
                    schedule_id,
                    claim_token,
                ),
            )
            if not cursor.rowcount:
                raise SchedulerError("scheduler execution lease was lost before recording result")
            connection.execute(
                "INSERT INTO schedule_runs("
                "run_id, schedule_id, action, ok, detail, ran_at, dedupe_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    schedule_id,
                    action,
                    1 if outcome.ok else 0,
                    _clean(outcome.detail, 1_000),
                    moment,
                    _clean(schedule.get("dedupe_key"), 256),
                ),
            )
        if outcome.ok:
            self._wake_chain(schedule, moment, woken, output=outcome.output or {})
        return ScheduleRun(
            schedule_id=schedule_id,
            action=action,
            ok=outcome.ok,
            detail=_clean(outcome.detail, 1_000),
            ran_at=moment,
        )

    def _handler_payload(self, schedule: dict[str, Any]) -> dict[str, Any]:
        """What the handler actually receives.

        The schedule's own payload, plus the two things a bounded job legitimately
        needs from its context: where to run, and what the run before it produced.
        Both are only present when configured, so a plain schedule still sees
        exactly the payload it was created with.
        """

        payload = dict(schedule.get("payload") or {})
        workdir = schedule.get("workdir")
        if workdir:
            payload["workdir"] = str(workdir)
        chain_input = schedule.get("chain_input")
        if chain_input:
            payload["chain_input"] = chain_input
        return payload

    def _duplicate_run(self, schedule: dict[str, Any], moment: float) -> str | None:
        """Skip a run that already happened inside its dedupe window."""

        key = _clean(schedule.get("dedupe_key"), 256)
        window = int(schedule.get("dedupe_window_seconds") or 0)
        if not key or window <= 0:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT ran_at FROM schedule_runs WHERE dedupe_key = ? AND ok = 1 "
                "AND ran_at >= ? ORDER BY ran_at DESC LIMIT 1",
                (key, moment - window),
            ).fetchone()
        if row is None:
            return None
        return f"skipped: an equivalent run already succeeded within {window}s"

    def _wake_chain(
        self,
        schedule: dict[str, Any],
        moment: float,
        woken: set[str] | None,
        *,
        output: dict[str, Any],
    ) -> None:
        """Hand this run's output to its successors and mark them due.

        Only active, approved successors are woken, and only once per tick. A
        chain cannot approve anything, cannot create a schedule, and cannot make
        a successor run an action it was not already reviewed for.
        """

        targets = list(schedule.get("chain_to") or [])
        if not targets:
            return
        source = str(schedule["schedule_id"])
        payload = json.dumps(
            {
                "from_schedule_id": source,
                "from_action": str(schedule.get("action") or ""),
                "output": output,
            },
            default=str,
        )[:16_000]
        for target in targets[:MAX_CHAIN_FANOUT]:
            if woken is not None:
                if target in woken:
                    continue
                woken.add(target)
            with suppress(Exception), self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE schedules SET next_run_at = ?, chain_input_json = ?, "
                    "updated_at = ? WHERE schedule_id = ? AND state = 'active'",
                    (moment, payload, moment, target),
                )

    def _claim(
        self, schedule_id: str, moment: float, *, require_due: bool
    ) -> tuple[dict[str, Any], str] | None:
        """Atomically fence one execution before its handler can cause effects."""

        token = str(uuid.uuid4())
        due_clause = " AND next_run_at <= ?" if require_due else ""
        params: list[object] = [token, moment + ACTION_LEASE_SECONDS, moment, schedule_id, moment]
        if require_due:
            params.append(moment)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE schedules SET claim_token = ?, claim_expires_at = ?, updated_at = ? "
                "WHERE schedule_id = ? AND state = 'active' "
                "AND (claim_expires_at IS NULL OR claim_expires_at <= ?)" + due_clause,
                tuple(params),
            )
            if not cursor.rowcount:
                return None
            row = connection.execute(
                "SELECT * FROM schedules WHERE schedule_id = ? AND claim_token = ?",
                (schedule_id, token),
            ).fetchone()
            if row is None:
                raise SchedulerError("scheduler execution lease could not be read back")
            return _schedule_dict(row), token

    def _notify(self, schedule_id: str, action: str, notify: dict[str, Any]) -> None:
        """Hand a scheduled notice to the one notification authority."""

        notifier = self._notifier
        if notifier is None:
            with suppress(Exception):
                from core.notification_authority import NotificationAuthority

                notifier = NotificationAuthority()
        if notifier is None:
            return
        with suppress(Exception):
            from core.notification_authority import NotificationRequest

            notifier.request(
                NotificationRequest(
                    kind=str(notify.get("kind") or f"schedule.{action}"),
                    summary=str(notify.get("summary") or ""),
                    channel=str(notify.get("channel") or "voice"),
                    urgency=str(notify.get("urgency") or "low"),
                    dedupe_key=str(notify.get("dedupe_key") or f"schedule:{schedule_id}"),
                    source_surface="system",
                )
            )

    # -- reads --------------------------------------------------------------

    def get(self, schedule_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM schedules WHERE schedule_id = ?", (schedule_id,)
            ).fetchone()
        return _schedule_dict(row) if row is not None else None

    def require(self, schedule_id: str) -> dict[str, Any]:
        found = self.get(schedule_id)
        if found is None:
            raise KeyError(f"unknown schedule {schedule_id}")
        return found

    def list(self, *, state: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[object] = []
        if state is not None:
            if state not in SCHEDULE_STATES:
                raise SchedulerError(f"unknown schedule state {state!r}")
            clauses.append("state = ?")
            params.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM schedules" + where + " ORDER BY next_run_at", tuple(params)
            ).fetchall()
        return [_schedule_dict(row) for row in rows]

    def history(self, schedule_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[object] = []
        if schedule_id:
            clauses.append("schedule_id = ?")
            params.append(schedule_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(min(500, max(1, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM schedule_runs" + where + " ORDER BY ran_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

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
                CREATE TABLE IF NOT EXISTS schedules (
                    schedule_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    interval_seconds INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    owner TEXT NOT NULL DEFAULT 'serena',
                    actor TEXT NOT NULL DEFAULT '',
                    approved_by TEXT,
                    next_run_at REAL NOT NULL,
                    last_run_at REAL,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    claim_token TEXT,
                    claim_expires_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS schedules_due_idx
                    ON schedules(state, next_run_at);
                CREATE TABLE IF NOT EXISTS schedule_runs (
                    run_id TEXT PRIMARY KEY,
                    schedule_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    ran_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS schedule_runs_idx
                    ON schedule_runs(schedule_id, ran_at);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(schedules)").fetchall()
            }
            # Additive migrations only. An existing scheduler database keeps
            # every row it had; the new columns arrive with defaults that mean
            # "behave exactly like before".
            for name, definition in (
                ("claim_token", "TEXT"),
                ("claim_expires_at", "REAL"),
                ("one_shot", "INTEGER NOT NULL DEFAULT 0"),
                ("workdir", "TEXT"),
                ("chain_to_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("join_of_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("chain_input_json", "TEXT"),
                ("last_output_json", "TEXT"),
                ("last_success_at", "REAL"),
                ("dedupe_key", "TEXT"),
                ("dedupe_window_seconds", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE schedules ADD COLUMN {name} {definition}"
                    )
            run_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(schedule_runs)").fetchall()
            }
            if "dedupe_key" not in run_columns:
                connection.execute("ALTER TABLE schedule_runs ADD COLUMN dedupe_key TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS schedule_runs_dedupe_idx "
                "ON schedule_runs(dedupe_key, ran_at)"
            )
        if os.name != "nt":
            with suppress(OSError):
                self.path.chmod(0o600)


def _json_field(row: sqlite3.Row, name: str, fallback: Any) -> Any:
    try:
        value = json.loads(str(row[name] or "null"))
    except (json.JSONDecodeError, IndexError, KeyError):
        return fallback
    return fallback if value is None else value


def _schedule_dict(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(str(row["payload_json"] or "{}"))
    except json.JSONDecodeError:
        payload = {}
    chain_to = _json_field(row, "chain_to_json", [])
    join_of = _json_field(row, "join_of_json", [])
    chain_input = _json_field(row, "chain_input_json", {})
    last_output = _json_field(row, "last_output_json", {})
    return {
        "schedule_id": str(row["schedule_id"]),
        "action": str(row["action"]),
        "payload": payload if isinstance(payload, dict) else {},
        "interval_seconds": int(row["interval_seconds"]),
        "state": str(row["state"]),
        "owner": str(row["owner"]),
        "actor": str(row["actor"] or ""),
        "approved_by": row["approved_by"],
        "next_run_at": float(row["next_run_at"]),
        "last_run_at": row["last_run_at"],
        "consecutive_failures": int(row["consecutive_failures"] or 0),
        "claim_token": row["claim_token"],
        "claim_expires_at": row["claim_expires_at"],
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "one_shot": bool(_row_value(row, "one_shot", 0)),
        "workdir": _row_value(row, "workdir", None),
        "chain_to": chain_to if isinstance(chain_to, list) else [],
        "join_of": join_of if isinstance(join_of, list) else [],
        "chain_input": chain_input if isinstance(chain_input, dict) else {},
        "last_output": last_output if isinstance(last_output, dict) else {},
        "last_success_at": _row_value(row, "last_success_at", None),
        "dedupe_key": _row_value(row, "dedupe_key", None),
        "dedupe_window_seconds": int(_row_value(row, "dedupe_window_seconds", 0) or 0),
    }


def _row_value(row: sqlite3.Row, name: str, fallback: Any) -> Any:
    try:
        return row[name]
    except (IndexError, KeyError):
        return fallback
