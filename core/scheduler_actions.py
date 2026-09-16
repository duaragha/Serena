"""The reviewed actions a Serena schedule is allowed to name.

This registry is the entire extensibility story for automation. A schedule
names one of these strings; it cannot supply a command, a path, or a callable.
Adding an action means adding a function here, with a test, in a reviewed
change. A plugin can ask for a schedule against an existing action; it cannot
become one.

The Fleet action may dispatch one ready task through the reviewed public API;
its authority comes from this code and the approved schedule, never a manifest
command. Other user-facing delivery still goes through notification authority.
"""

from __future__ import annotations

from typing import Any

from core.serena_scheduler import ActionOutcome

# Below this, telling him about outstanding work is noise rather than useful.
OWED_NOTICE_THRESHOLD = 1
# Only complain about something that has genuinely been sitting.
OWED_STALE_SECONDS = 3_600.0
# Reservations cannot be pruned without losing at-most-once dispatch. Stop
# accepting new identities at this bound until an operator archives the queue.
MAX_FLEET_DISPATCHES = 100_000
# Dispatched runs that may be open at once. Fleet itself has no run ceiling,
# and each run already fans out to up to four workers.
DEFAULT_MAX_ACTIVE_TASK_RUNS = 2
TERMINAL_RUN_STATES = frozenset({"completed", "failed", "cancelled"})
# Reconciliation work per tick: deliveries push to GitHub, so keep it small.
MAX_RECONCILE_PER_TICK = 3
DISPATCH_ORIGIN = "serena-task:"
DELIVERY_RULES = (
    "\n\n---\nDelivery rules for this dispatched task: you are working in a private "
    "checkout on branch serena/task-{task_id}. Leave your finished changes in this "
    "working tree. Do not push, open pull requests, merge, tag, release, or deploy; "
    "the dispatcher commits, pushes and opens the pull request after the run."
)


def _max_active_task_runs() -> int:
    import os

    raw = os.environ.get("SERENA_TASK_MAX_ACTIVE_RUNS", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_ACTIVE_TASK_RUNS
    except ValueError:
        value = DEFAULT_MAX_ACTIVE_TASK_RUNS
    return max(1, min(value, 8))



def _number(value: object, fallback: float) -> float:
    """A payload number, where an explicit 0 means 0 and not "unset"."""

    if value is None or isinstance(value, bool):
        return float(fallback)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float(fallback)


def sweep_obligations(payload: dict[str, Any]) -> ActionOutcome:
    """Retry what died mid-delivery and let hopeless things resolve honestly.

    This is the recurring counterpart to the sweep the resident loop does once
    at boot. Without it, an obligation that went stale hours after the last
    restart would sit open forever.
    """

    from core.control_plane import ControlPlaneStore
    from core.control_recovery import (
        DEFAULT_MAX_ATTEMPTS,
        DEFAULT_STALE_SECONDS,
        journal_redelivery_handlers,
        reconcile_obligations,
    )

    control = ControlPlaneStore()
    handlers = dict(journal_redelivery_handlers(control))
    stale_seconds = _number(payload.get("stale_seconds"), DEFAULT_STALE_SECONDS)
    max_attempts = int(_number(payload.get("max_attempts"), DEFAULT_MAX_ATTEMPTS))
    # If the notification authority cannot be reached, the sweep still runs for
    # every other surface, but it must not quietly claim it covered
    # notifications. The degradation is reported in the outcome rather than
    # swallowed, so "swept N outstanding" never overstates what was attempted.
    notifications_degraded = ""
    try:
        from core.notification_senders import default_authority

        authority = default_authority()

        def redeliver_notification(obligation: Any) -> bool:
            result = authority.redeliver(str(obligation.job_id or ""))
            return bool(result and result.sent)

        handlers["notification"] = redeliver_notification
    except Exception as error:
        notifications_degraded = f"{type(error).__name__}: {error}"
        handlers.pop("notification", None)

    report = reconcile_obligations(
        control,
        handlers=handlers,
        stale_seconds=stale_seconds,
        max_attempts=max_attempts,
    )
    detail = (
        f"swept {report.total} outstanding: {len(report.recovered)} retried, "
        f"{len(report.abandoned)} given up on"
    )
    if notifications_degraded:
        detail += f"; could not redeliver notifications ({notifications_degraded})"
    return ActionOutcome(
        True,
        detail,
        output={
            "recovered": len(report.recovered),
            "abandoned": len(report.abandoned),
            "skipped": len(report.skipped),
            "notifications_degraded": notifications_degraded,
        },
    )


def report_outstanding(payload: dict[str, Any]) -> ActionOutcome:
    """Tell him what Serena still owes, but only when it is worth saying.

    Silence is the common case and the correct one. This speaks only when
    something has actually been sitting unresolved, so it stays a signal rather
    than a daily status ping he learns to ignore.
    """

    import time

    from core.control_recovery import outstanding_debts

    # `or` would treat an explicit 0 as "not supplied", which is exactly the
    # value a caller uses to mean "report everything".
    threshold = int(_number(payload.get("threshold"), OWED_NOTICE_THRESHOLD))
    stale_after = _number(payload.get("stale_seconds"), OWED_STALE_SECONDS)
    now = time.time()

    debts = outstanding_debts()
    stale = [
        item
        for item in debts["items"]
        if now - float(item.get("created_at") or now) >= stale_after
    ]
    if len(stale) < threshold and not debts["unconfirmed"]:
        return ActionOutcome(True, "nothing outstanding worth interrupting him for")

    return ActionOutcome(
        True,
        debts["spoken"],
        notify={
            "kind": "serena.owed",
            "summary": debts["spoken"],
            "channel": str(payload.get("channel") or "voice"),
            "urgency": "low",
            # One notice per distinct shape of debt, so a persistent backlog
            # does not become a recurring nag.
            "dedupe_key": f"owed:{debts['open']}:{debts['unconfirmed']}",
        },
        output={"open": debts["open"], "unconfirmed": debts["unconfirmed"]},
    )


def flush_notifications(payload: dict[str, Any]) -> ActionOutcome:
    """Deliver notices whose quiet-hours or retry deadline has passed."""

    from core.notification_senders import default_authority

    results = default_authority().deliver_due()
    delivered = len([item for item in results if item.sent])
    return ActionOutcome(
        True,
        f"delivered {delivered} of {len(results)} due notices",
        output={"delivered": delivered, "attempted": len(results)},
    )


def publish_surface_events(payload: dict[str, Any]) -> ActionOutcome:
    """Push any committed-but-unpublished surface events onto the control plane."""

    from core.surface_journal import flush_all

    published = flush_all()
    return ActionOutcome(
        True, f"published {sum(published.values())} staged events", output=published
    )


def start_ready_fleet_task(payload: dict[str, Any]) -> ActionOutcome:
    """Dispatch at most one ready task, with a durable at-most-once fence.

    Use ``fleet.supervisor.start_run``: ``brain_fleet_tools.start_fleet`` is a
    spoken-turn tool and correctly requires live voice/desk grounding that a
    scheduler does not have. Provider exhaustion is already checked before
    ``AutomationRuntime`` drains the scheduler; do not duplicate that gate.

    The public start API has no idempotency key. Commit an intent before calling
    it, outside the task claim transaction. An expired lease or process crash
    must never authorize a second start. An uncertain intent is held for manual
    reconciliation; a bounded run lookup can recover a receipt, but absence
    from that lookup never permits retrying an existing intent. Reservations
    survive terminal runs and are bounded rather than silently expired.

    A reservation is the fence, so nothing that can merely fail may be done
    while holding one the task did not already have. Every fallible step runs
    before this task reserves an identity, and the reservation is re-checked
    under the write lock immediately before the start call. A transient run
    lookup is then a plain retry on a task that was never dispatched, instead
    of a permanent fence around work Fleet never saw.
    """

    import sqlite3
    from contextlib import closing
    from pathlib import Path
    from uuid import uuid4

    from core.coding_job_contract import resolve_repository_root
    from fleet.supervisor import list_runs, start_run
    from memory import store

    from core import agent_checkouts

    if payload:
        return ActionOutcome(False, "serena.fleet.start accepts no schedule payload")

    owner = f"scheduler:{uuid4().hex}"
    task = store.claim_next_task(owner)
    if task is None:
        return ActionOutcome(True, "no ready task")
    task_id = int(task["id"])
    token = task["lease_token"]
    origin = f"{DISPATCH_ORIGIN}{task_id}"
    output = {"task_id": task_id}

    def hold(detail: str, *, state: str = "blocked") -> ActionOutcome:
        released = store.release_task_claim(task_id, owner, token, state=state)
        return ActionOutcome(
            True, detail,
            output={**output, "state": state if released else "claim_changed"},
        )

    try:
        # A schedule cannot redirect the queue, repository, provider or command.
        # These inputs come only from the claimed task and reviewed defaults.
        cwd = resolve_repository_root(
            task["content"], project_hint=task.get("project_hint", "")
        )
    except ValueError as error:
        return hold(f"task needs repository triage: {error}", state="needs_triage")

    path = store.MEMORY_DIR / ".fleet-dispatch.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=5)) as db:
        db.execute("PRAGMA synchronous = FULL")
        db.execute(
            "CREATE TABLE IF NOT EXISTS dispatches ("
            "task_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL DEFAULT '')"
        )

        def reserved() -> tuple | None:
            return db.execute(
                "SELECT run_id FROM dispatches WHERE task_id = ?", (task_id,)
            ).fetchone()

        def uncertain() -> ActionOutcome:
            return hold("Fleet dispatch is uncertain; reconcile before retrying")

        def attach(found: str) -> ActionOutcome:
            """Save the receipt before touching the lease-owned task.

            Even if the claim expired during the start call, the next claimant
            reads this reservation and cannot open a second run.
            """

            try:
                with db:
                    db.execute(
                        "INSERT INTO dispatches(task_id, run_id) VALUES (?, ?) "
                        "ON CONFLICT(task_id) DO UPDATE SET run_id = excluded.run_id",
                        (task_id, found),
                    )
                marked = store.mark_task_running(task_id, owner, token, found)
            except Exception as error:
                return hold(f"Fleet start requires reconciliation: {type(error).__name__}")
            return ActionOutcome(
                True,
                "Fleet run recorded" if marked
                else "Fleet run recorded; task lease changed",
                output={**output, "run_id": found},
            )

        previous = reserved()
        if previous is not None and str(previous[0]):
            return attach(str(previous[0]))

        # The bounded lookup is the one fallible step here, so it runs before
        # this task takes a reservation. Its absence still proves nothing: the
        # history is capped, so it may recover a receipt but may never license
        # a retry of an intent that already exists.
        try:
            history = list_runs(limit=500)
            found = next(
                (str(run["run_id"]) for run in history
                 if run.get("origin_session_id") == origin),
                "",
            )
            active = sum(
                1 for run in history
                if str(run.get("origin_session_id") or "").startswith(DISPATCH_ORIGIN)
                and str(run.get("state") or "") not in TERMINAL_RUN_STATES
            )
        except Exception as error:
            if previous is not None:
                return uncertain()
            # Nothing was reserved and nothing was dispatched, so this is an
            # ordinary retry rather than something a human has to unpick.
            return hold(
                f"Fleet run history is unavailable: {type(error).__name__}", state="ready"
            )
        if previous is not None:
            return attach(found) if found else uncertain()
        limit = _max_active_task_runs()
        if not found and active >= limit:
            # Fleet has no run ceiling of its own; this is the admission gate.
            return hold(f"at capacity: {active} of {limit} task runs active", state="ready")

        with db:
            db.execute("BEGIN IMMEDIATE")
            # Re-read under the write lock: a claim that expired while a slower
            # dispatcher was mid-flight can have reserved this identity since.
            racer = reserved()
            if racer is None:
                count = db.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
                if count >= MAX_FLEET_DISPATCHES:
                    return hold("Fleet dispatch ledger is full; operator review required")
                db.execute(
                    "INSERT INTO dispatches(task_id, run_id) VALUES (?, ?)",
                    (task_id, found),
                )
        if racer is not None:
            return attach(str(racer[0])) if str(racer[0]) else uncertain()
        if found:
            return attach(found)

        try:
            checkout = agent_checkouts.prepare(Path(cwd), task_id)
        except agent_checkouts.CheckoutError as error:
            # Nothing reached Fleet, so the reservation can be withdrawn and a
            # human told why; retrying blindly would hit the same wall.
            with db:
                db.execute("DELETE FROM dispatches WHERE task_id = ? AND run_id = ''",
                           (task_id,))
            return hold(f"no private checkout: {error}")
        try:
            run = start_run(
                task=task["content"] + DELIVERY_RULES.format(task_id=task_id),
                activity="auto", provider_mode="auto",
                cwd=str(checkout.path), origin_session_id=origin,
            )
            run_id = str(run["run_id"] or "")
            if not run_id:
                raise ValueError("Fleet returned no run id")
        except Exception as error:
            return hold(f"Fleet start requires reconciliation: {type(error).__name__}")
        return attach(run_id)


def _notify_phone(text: str, key: str) -> bool:
    """Tell Raghav on his phone line, through the one notification authority."""

    from core.notification_senders import notify

    result = notify("task.update", text, channel="imessage", dedupe_key=key,
                    source_surface="dispatch", fallback_channel=None)
    return bool(result.sent)


def reconcile_fleet_tasks(payload: dict[str, Any]) -> ActionOutcome:
    """Close finished dispatched runs: deliver, record, and tell him.

    Also sends the single triage question for briefs that were too thin.
    Delivery is idempotent: pushing the same branch and finding its existing
    pull request is the retry path, so a crash between steps is harmless.
    """

    from core import agent_checkouts
    from fleet.supervisor import get_run
    from memory import store

    if payload:
        return ActionOutcome(False, "serena.fleet.reconcile accepts no schedule payload")
    closed: list[dict[str, Any]] = []
    for task in store.tasks_in_state("running"):
        if len(closed) >= MAX_RECONCILE_PER_TICK:
            break
        run_id = str(task.get("run_id") or "")
        if not run_id:
            continue
        try:
            run = get_run(run_id)
        except Exception:
            continue
        state = str((run or {}).get("state") or "")
        if state not in TERMINAL_RUN_STATES:
            continue
        task_id = int(task["id"])
        brief = str(task["content"])
        headline = " ".join(brief.split())[:80]
        record: dict[str, Any] = {"task_id": task_id, "run_state": state}
        checkout = None
        try:
            checkout = agent_checkouts.locate(run.get("cwd") or "")
        except agent_checkouts.CheckoutError as error:
            record["error"] = str(error)
        if state == "completed" and checkout is not None:
            try:
                delivery = agent_checkouts.deliver(
                    checkout, task_id=task_id, brief=brief, run_id=run_id)
            except agent_checkouts.CheckoutError as error:
                # Leave the task running; the next tick retries the delivery.
                record["error"] = f"delivery failed: {error}"
                closed.append(record)
                continue
            record.update(delivery=delivery.status, url=delivery.url)
            if delivery.status == "no_changes":
                result, message = "done: no changes needed", (
                    f"#{task_id} finished with no code changes ({headline}).")
            elif delivery.status == "merged":
                try:
                    shipped = agent_checkouts.ship(checkout)
                except agent_checkouts.CheckoutError as error:
                    shipped = f"ship step failed: {error}"
                record["shipped"] = shipped
                tail = f"; {shipped}" if shipped else ""
                result, message = f"merged: {delivery.url}{tail}", (
                    f"#{task_id} done and merged{tail}: {delivery.url}")
            else:
                note = f" ({delivery.detail})" if delivery.detail else ""
                result, message = f"pr: {delivery.url}", (
                    f"#{task_id} done, PR ready for you{note}: {delivery.url}")
            final = "done"
        else:
            reason = str(run.get("error") or state)[:200]
            result = f"{state}: {reason}"
            message = f"#{task_id} {state} ({headline}). {reason}"
            final = "blocked"
        if store.finish_task_run(task_id, run_id, final, result):
            record["notified"] = _notify_phone(message, f"task:{task_id}:{final}")
            if checkout is not None and final == "done":
                # A failed run keeps its worktree so the partial work can be read.
                agent_checkouts.cleanup(checkout)
        closed.append(record)

    asked = []
    for task in store.tasks_in_state("needs_triage"):
        if task.get("asked_at") or not str(task.get("source_id") or ""):
            continue
        if len(asked) >= MAX_RECONCILE_PER_TICK:
            break
        task_id = int(task["id"])
        headline = " ".join(str(task["content"]).split())[:120]
        question = (f"#{task_id} needs one detail before i hand it off: \"{headline}\". "
                    f"what exactly should change, and in which project? "
                    f"reply \"#{task_id} <details>\".")
        if _notify_phone(question, f"task:{task_id}:question"):
            store.mark_task_asked(task_id)
            asked.append(task_id)
    return ActionOutcome(
        True, f"closed {sum(1 for r in closed if 'notified' in r)} task(s), "
              f"asked {len(asked)} question(s)",
        output={"closed": closed, "asked": asked},
    )


def poll_phone_line(payload: dict[str, Any]) -> ActionOutcome:
    """Read new iMessage commands from Raghav's thread into the queue."""

    from core import phone_line
    from core.unified_hub import UnifiedHubError

    if payload:
        return ActionOutcome(False, "serena.phone.poll accepts no schedule payload")
    if not phone_line.available():
        return ActionOutcome(True, "phone line is not configured on this machine")
    try:
        report = phone_line.poll()
    except UnifiedHubError as error:
        return ActionOutcome(False, f"phone line unavailable: {error}")
    return ActionOutcome(
        True, f"read {report.seen} message(s), handled {len(report.commands)} command(s)",
        output={"commands": report.commands},
    )


# The whole registry. A schedule may name exactly one of these keys.
REVIEWED_ACTIONS = {
    "serena.obligations.sweep": sweep_obligations,
    "serena.obligations.report": report_outstanding,
    "serena.notifications.flush": flush_notifications,
    "serena.surfaces.publish": publish_surface_events,
    "serena.fleet.start": start_ready_fleet_task,
    "serena.fleet.reconcile": reconcile_fleet_tasks,
    "serena.phone.poll": poll_phone_line,
}


def register_all(scheduler: Any) -> Any:
    """Attach every reviewed action to a scheduler."""

    for name, handler in REVIEWED_ACTIONS.items():
        scheduler.register_action(name, handler)
    return scheduler
