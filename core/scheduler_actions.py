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

import re
from typing import Any

from core.serena_scheduler import ActionOutcome

# A spoken notice is read aloud, so the doctor's lead line has to stay short.
DOCTOR_SUMMARY_LIMIT = 220
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
# A run parked on a question uses no workers. It must not hold a dispatch slot,
# or one stuck run would stall the queue behind it indefinitely.
IDLE_RUN_STATES = TERMINAL_RUN_STATES | {"waiting_for_input"}
# Reconciliation work per tick: deliveries push to GitHub, so keep it small.
MAX_RECONCILE_PER_TICK = 3
DISPATCH_ORIGIN = "serena-task:"
# The scheduler adds these to every handler payload out of its own context --
# where to run, and what the previous link produced -- never from anything a
# caller supplied. An action that takes no configuration still has to tolerate
# them: without this, chaining one of them would fail it on every run and
# `MAX_CONSECUTIVE_FAILURES` would quietly disable the schedule.
RESERVED_PAYLOAD_KEYS = frozenset({"chain_input", "workdir"})


def _configuration(payload: dict[str, Any] | None) -> dict[str, Any]:
    """The part of a payload that is actually configuration, if any."""

    return {key: value for key, value in (payload or {}).items()
            if key not in RESERVED_PAYLOAD_KEYS}

DELIVERY_RULES = (
    "\n\n---\nDispatcher handoff for this task: you are working in a private "
    "checkout on branch serena/task-{task_id}, and that checkout is this run's base "
    "checkout. Leave your finished changes in its working tree. Do not push, open "
    "pull requests, merge, tag, release, or deploy; after the run the dispatcher "
    "commits the working tree, pushes the branch and opens the pull request, and "
    "any release follows that pull request. That dispatcher is \"root\", so a "
    "delivery requirement it owns rather than you is deferred with owner exactly "
    "\"root\" -- not a description like \"Fleet supervisor\", which Fleet cannot "
    "route to and rejects. Report "
    "your delivery[] entries exactly as below, copying each requirement string "
    "character for character (only the evidence text is yours to write):\n{answers}"
)

# A private checkout is a clone. It holds what git tracks and nothing else: no
# memory/, no .env, no node_modules, no local databases. A worker asked to
# "audit the task list" once found no task list, invented an unrelated change
# and reported success, which is worse than failing -- so say plainly what is
# absent, and that a brief depending on it must stop rather than substitute.
UNSEEN_STATE_RULES = (
    "\n\nWhat this checkout does not contain: it is a fresh clone, so only files "
    "git tracks are present. His task queue (memory/), environment files, "
    "node_modules, and local databases are NOT here, and neither is anything "
    "written by a running Serena. If this brief depends on something you cannot "
    "see, do not substitute different work and do not report success: say "
    "exactly what you needed and could not read, and stop.{queue}"
)

# Briefs that are about the queue itself, which a clone cannot show.
_QUEUE_WORDS = re.compile(
    r"\b(task list|tasks?|queue|backlog|todo|to-do|open work)\b", re.IGNORECASE)
MAX_ATTACHED_TASKS = 40


def _attached_task_list() -> str:
    """The open queue, for a brief that talks about it, since git cannot show it."""

    from memory import store

    lines: list[str] = []
    for state in ("running", "ready", "needs_triage", "blocked", "backlog"):
        try:
            rows = store.tasks_in_state(state)
        except Exception:
            continue
        for row in rows[:MAX_ATTACHED_TASKS]:
            text = " ".join(str(row.get("content") or "").split())[:160]
            lines.append(f"  #{row['id']} [{state}] {text}")
            if len(lines) >= MAX_ATTACHED_TASKS:
                break
        if len(lines) >= MAX_ATTACHED_TASKS:
            break
    if not lines:
        return ""
    return ("\n\nHis open tasks, attached because this checkout cannot show them "
            "(newest states first; this is the whole list you may reason about):\n"
            + "\n".join(lines))


def _unseen_state_rules(brief: str) -> str:
    queue = _attached_task_list() if _QUEUE_WORDS.search(brief or "") else ""
    return UNSEEN_STATE_RULES.format(queue=queue)


def _why(error: BaseException) -> str:
    """The failure's type and its message, bounded.

    Reporting only the type name turned "the shipped Fleet config no longer
    matches the model policy" into "requires reconciliation: ValueError",
    which says nothing to him and nothing to whoever debugs it next. The
    message is what makes the notice actionable, so it travels with the type.
    """

    return f"{type(error).__name__}: {' '.join(str(error).split())[:200] or 'no detail'}"


def _delivery_rules(task_id: int) -> str:
    """The handoff note, with Fleet's own requirement strings quoted verbatim.

    Fleet matches delivery answers by exact requirement text, and a worker
    told to paraphrase-free answer them still paraphrased once. Quoting the
    contract's strings removes the guesswork.
    """

    import json

    from fleet.contracts import _completion_contract

    requirements = _completion_contract("coding", "")["delivery_requirements"]
    answers = []
    for index, requirement in enumerate(requirements):
        if index == 0:
            answers.append({"requirement": requirement, "state": "verified",
                            "evidence": "<the changed paths in this checkout>"})
        else:
            answers.append({"requirement": requirement, "state": "not_applicable",
                            "reason": "This run touches no deployed or live surface; "
                                      "delivery is the dispatcher's pull request after the run."})
    return DELIVERY_RULES.format(task_id=task_id, answers=json.dumps(answers, indent=2))


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

    import hashlib
    import sqlite3
    from contextlib import closing
    from pathlib import Path
    from uuid import uuid4

    from core import agent_checkouts
    from core.coding_job_contract import resolve_repository_root
    from fleet.supervisor import list_runs, start_run
    from memory import store

    if _configuration(payload):
        return ActionOutcome(False, "serena.fleet.start accepts no schedule payload")

    owner = f"scheduler:{uuid4().hex}"
    task = store.claim_next_task(owner)
    if task is None:
        return ActionOutcome(True, "no ready task")
    task_id = int(task["id"])
    brief = str(task["content"])
    token = task["lease_token"]
    origin = f"{DISPATCH_ORIGIN}{task_id}"
    output = {"task_id": task_id}

    def hold(detail: str, *, state: str = "blocked") -> ActionOutcome:
        released = store.release_task_claim(task_id, owner, token, state=state)
        # "blocked" here means the dispatcher could not even open the run, on
        # something it cannot clear itself -- GitHub auth it cannot renew, a
        # repository it cannot reach. Left quiet, the task simply never starts
        # and the queue looks idle. needs_triage is not this: reconcile asks
        # him its own question about those.
        if released and state == "blocked" and _he_asked(task):
            stuck = hashlib.sha256(detail.encode("utf-8")).hexdigest()[:12]
            output["notified"] = _notify_once(
                f"#{task_id} can't even start ({' '.join(brief.split())[:60]}): {detail}",
                f"task:{task_id}:nostart:{stuck}",
                answers_request=True,
            )
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
                return hold(f"Fleet start requires reconciliation: {_why(error)}")
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
                and str(run.get("state") or "") not in IDLE_RUN_STATES
            )
        except Exception as error:
            if previous is not None:
                return uncertain()
            # Nothing was reserved and nothing was dispatched, so this is an
            # ordinary retry rather than something a human has to unpick.
            return hold(
                f"Fleet run history is unavailable: {_why(error)}", state="ready"
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
        if getattr(checkout, "stale_base", False):
            # The run is real work on a base that could not be refreshed. Say
            # so in the record rather than letting a stale branch look current.
            output["stale_base"] = True
        try:
            run = start_run(
                task=task["content"] + _delivery_rules(task_id)
                + _unseen_state_rules(brief),
                activity="auto", provider_mode="auto",
                cwd=str(checkout.path), origin_session_id=origin,
            )
            run_id = str(run["run_id"] or "")
            if not run_id:
                raise ValueError("Fleet returned no run id")
        except Exception as error:
            return hold(f"Fleet start requires reconciliation: {_why(error)}")
        return attach(run_id)


def _he_asked(task: dict[str, Any]) -> bool:
    """True when the task came from him, so telling him how it went is a reply.

    A queue write with nobody on the other end is a different thing: no one is
    waiting on it, and it can keep until morning like any other notice.
    """

    return str(task.get("source_id") or "").startswith(("imessage:", "webhook:"))


def _notify_phone(text: str, key: str, *, answers_request: bool = False) -> bool:
    """Tell Raghav on his phone line, through the one notification authority."""

    from core.notification_senders import notify

    result = notify("task.update", text, channel="imessage", dedupe_key=key,
                    source_surface="dispatch", fallback_channel=None,
                    answers_request=answers_request)
    return bool(result.sent)


def _ring_phone(text: str, key: str) -> bool:
    """Call him about it too, when his phone line is set up.

    The text above is the record; the call is only the nudge, so it skips
    quiet hours instead of queueing a stale call for the morning.
    """

    import time

    from core import phone_call
    from core.notification_senders import default_authority, notify

    if not phone_call.enabled() or default_authority().policy.in_quiet_hours(time.time()):
        return False
    result = notify("task.update", text, channel="call", dedupe_key=f"{key}:call",
                    source_surface="dispatch", fallback_channel=None)
    return bool(result.sent)


def _notify_once(text: str, key: str, *, answers_request: bool = False) -> bool:
    """Send a notice at most once ever, beyond the authority's hourly dedupe."""

    import sqlite3
    from contextlib import closing

    from memory import store

    path = store.MEMORY_DIR / ".fleet-dispatch.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=5)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS notices (key TEXT PRIMARY KEY)")
        if db.execute("SELECT 1 FROM notices WHERE key = ?", (key,)).fetchone():
            return False
        if not _notify_phone(text, key, answers_request=answers_request):
            return False
        with db:
            db.execute("INSERT OR IGNORE INTO notices(key) VALUES (?)", (key,))
    return True


def _spoken_summary(task_id: int, final: str, headline: str) -> str:
    """What she says when he picks up; links stay in the text."""

    if final == "done":
        return f"hey, task {task_id} is done: {headline}. details are in your messages."
    return f"hey, task {task_id} got stuck: {headline}. i texted you what happened."


_LABEL_FILLER = re.compile(
    r"^\s*(?:in|for|on)\s+[\w-]+\s*(?:\([^)]*\))?\s*[,:-]?\s*", re.IGNORECASE)


def _describe(brief: str) -> str:
    """Three to five words for what a task is doing, asked of her brain once.

    Falls back to the brief's own first words when the brain is unreachable,
    so a text is never held back waiting on a label.
    """

    text = " ".join(brief.split())
    try:
        from core import phone_intent

        endpoint = phone_intent._endpoint()
        if endpoint is not None:
            url, token = endpoint
            answer = phone_intent._post(url, {
                "text": ("Label this coding task in 3 to 5 lowercase words for a text "
                         "message, like \"liquid glass ui in chats\" or \"fix search "
                         "focus on mobile\". Reply with the words only, no project name, "
                         f"no punctuation.\n\nTask: {text[:1500]}"),
                "protocol": "plain",
            }, token)
            said = str((answer or {}).get("say") or "") if isinstance(answer, dict) else ""
            words = re.sub(r"[^\w\s&/+-]", "", said.splitlines()[0] if said else "").lower().split()
            if 2 <= len(words) <= 7:
                return " ".join(words[:6])
    except Exception:
        pass
    return " ".join(_LABEL_FILLER.sub("", text).lower().split()[:5]) or "task"


def _task_label(task: dict[str, Any]) -> str:
    """'In Unified (liquid glass ui in chats)': the project, then what it's doing.

    Worked out once per task and kept, so every text about it says the same thing.
    """

    import sqlite3
    from contextlib import closing
    from pathlib import Path

    from memory import store

    task_id = int(task["id"])
    path = store.MEMORY_DIR / ".fleet-dispatch.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=5)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS task_labels (task_id INTEGER PRIMARY KEY, label TEXT)")
        row = db.execute("SELECT label FROM task_labels WHERE task_id = ?", (task_id,)).fetchone()
        if row:
            return str(row[0])
        brief = str(task.get("content") or "")
        try:
            from core.coding_job_contract import resolve_repository_root

            project = Path(resolve_repository_root(
                brief, project_hint=task.get("project_hint") or "")).name
            project = project[:1].upper() + project[1:]
        except Exception:
            project = ""
        what = _describe(brief)
        label = f"In {project} ({what})" if project else f"({what})"
        with db:
            db.execute("INSERT OR REPLACE INTO task_labels(task_id, label) VALUES (?, ?)",
                       (task_id, label))
    return label


# What each Fleet phase means to him. The last phase has no text of its own:
# the "PR ready" / "merged" message that follows it already says it finished.
PHASE_LABELS = {"discover": "research", "execute": "code", "verify": "review"}


def _announce_finished_phases(task: dict, run_id: str, run: dict) -> list[str]:
    """Text him once as each phase of a dispatched run completes."""

    phases = [p for p in (run or {}).get("phases") or [] if isinstance(p, dict)]
    sent = []
    label = ""
    for number, phase in enumerate(phases, start=1):
        name = str(phase.get("name") or "")
        if phase.get("state") != "completed" or name not in PHASE_LABELS:
            continue
        label = label or _task_label(task)
        if _notify_once(
            f"#{task['id']} {PHASE_LABELS[name]} done ({number}/{len(phases)}): {label}",
            f"task:{task['id']}:phase:{run_id}:{name}",
            answers_request=_he_asked(task),
        ):
            sent.append(name)
    return sent


def reconcile_fleet_tasks(payload: dict[str, Any]) -> ActionOutcome:
    """Close finished dispatched runs: deliver, record, and tell him.

    Also sends the single triage question for briefs that were too thin.
    Delivery is idempotent: pushing the same branch and finding its existing
    pull request is the retry path, so a crash between steps is harmless.
    """

    import hashlib

    from core import agent_checkouts
    from fleet.supervisor import get_run
    from memory import store

    if _configuration(payload):
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
        if state == "waiting_for_input":
            # Fleet parked it on a question only a human can answer. Say so
            # once per distinct question; the task stays open for a retry.
            reason = " ".join(str(run.get("error") or "needs input").split())[:300]
            digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:12]
            if _notify_once(
                f"#{task['id']} is stuck waiting on input (fleet {run_id[:8]}): {reason}",
                f"task:{task['id']}:waiting:{digest}",
                answers_request=_he_asked(task),
            ):
                closed.append({"task_id": int(task["id"]), "run_state": state,
                               "waiting": True})
            continue
        _announce_finished_phases(task, run_id, run)
        if state not in TERMINAL_RUN_STATES:
            continue
        task_id = int(task["id"])
        brief = str(task["content"])
        headline = _task_label(task)
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
                # Retrying silently is how a finished job looks like a hung
                # one. Delivery fails on things only he can clear -- expired
                # GitHub auth, a protected branch -- so say it once per reason
                # instead of pushing every 60 seconds and never mentioning it.
                reason = " ".join(str(error).split())[:300]
                stuck = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:12]
                record["notified"] = _notify_once(
                    f"#{task_id} built, can't deliver: {headline}. {reason}",
                    f"task:{task_id}:delivery:{stuck}",
                    answers_request=_he_asked(task),
                )
                closed.append(record)
                continue
            record.update(delivery=delivery.status, url=delivery.url)
            if delivery.status == "no_changes":
                result, message = "done: no changes needed", (
                    f"#{task_id} done, no code changes needed: {headline}")
            elif delivery.status == "merged":
                try:
                    shipped = agent_checkouts.ship(checkout)
                except agent_checkouts.CheckoutError as error:
                    shipped = f"ship step failed: {error}"
                record["shipped"] = shipped
                tail = f"; {shipped}" if shipped else ""
                result, message = f"merged: {delivery.url}{tail}", (
                    f"#{task_id} merged (4/4): {headline}{tail}. {delivery.url}")
            else:
                note = f" ({delivery.detail})" if delivery.detail else ""
                result, message = f"pr: {delivery.url}", (
                    f"#{task_id} PR ready (4/4): {headline}{note}. {delivery.url}")
            final = "done"
        else:
            reason = str(run.get("error") or state)[:200]
            result = f"{state}: {reason}"
            message = f"#{task_id} {state}: {headline}. {reason}"
            final = "blocked"
        if store.finish_task_run(task_id, run_id, final, result):
            record["notified"] = _notify_phone(
                message, f"task:{task_id}:{final}", answers_request=_he_asked(task))
            record["called"] = _ring_phone(_spoken_summary(task_id, final, headline),
                                           f"task:{task_id}:{final}")
            if checkout is not None and final == "done":
                # A failed run keeps its worktree so the partial work can be read.
                agent_checkouts.cleanup(checkout)
        closed.append(record)

    asked = []
    for task in store.tasks_in_state("needs_triage"):
        # Anything someone filed -- his phone, a webhook, or a chat pane
        # queueing work for him -- gets its one question on his phone. A task
        # from a chat pane used to sit in triage with nobody told, which looked
        # exactly like Fleet ignoring him. Only sourceless internal writes stay quiet.
        if task.get("asked_at") or str(task.get("source_id") or "queue:").startswith("queue:"):
            continue
        if len(asked) >= MAX_RECONCILE_PER_TICK:
            break
        task_id = int(task["id"])
        headline = " ".join(str(task["content"]).split())[:120]
        question = (f"#{task_id} needs one detail before i hand it off: \"{headline}\". "
                    f"what exactly should change, and in which project? "
                    f"reply \"#{task_id} <details>\".")
        if _notify_phone(question, f"task:{task_id}:question", answers_request=True):
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

    if _configuration(payload):
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


# She waits before raising a stalled task, because the queue usually moves it
# without him, and then speaks at most once a shift. Initiative that repeats
# every poll is nagging, and he stops reading a line that nags.
NUDGE_ASKED_AGE_SECONDS = 45 * 60
NUDGE_INTERVAL_SECONDS = 6 * 3600


NUDGE_SPOKEN_DECISIONS = frozenset({"sent", "deferred", "pending_approval", "suppressed"})


def _nudge_decision(text: str, key: str) -> str:
    """Hand one nudge to the authority and report what it decided to do."""

    from core.notification_senders import notify

    result = notify("task.update", text, channel="imessage", dedupe_key=key,
                    source_surface="dispatch", fallback_channel=None)
    return str(result.decision)


def _nudge_state_path():
    from pathlib import Path

    return Path.home() / ".local" / "state" / "serena" / "phone-nudge.json"


def _nudge_text(task: dict[str, Any]) -> str:
    brief = " ".join(str(task.get("content") or "").split())[:140]
    task_id = task["id"]
    if task["state"] == "blocked":
        return (f"#{task_id} is still stuck and i can't move it: {brief}. "
                f"reply \"retry #{task_id}\" or tell me what to change.")
    return (f"#{task_id} is still waiting on you: {brief}. "
            f"reply \"#{task_id} <details>\" and i'll run it.")


def nudge_phone_line(payload: dict[str, Any]) -> ActionOutcome:
    """Text him first about the one task that cannot move without him.

    Every other notice is a reply, or a report about work that just finished.
    This is the one place she starts the conversation, so it is deliberately
    narrow: the oldest task that is blocked or whose triage question he never
    answered, one line, and never more often than NUDGE_INTERVAL_SECONDS.
    Delivery goes through the same authority as everything else, so quiet hours
    and the hourly limit still hold.
    """

    import json
    import time

    from core import phone_line
    from memory import store

    if _configuration(payload):
        return ActionOutcome(False, "serena.phone.nudge accepts no schedule payload")
    if not phone_line.available():
        return ActionOutcome(True, "phone line is not configured on this machine")
    now = time.time()
    path = _nudge_state_path()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    quiet_for = NUDGE_INTERVAL_SECONDS - (now - float(state.get("nudged_at") or 0))
    if quiet_for > 0:
        return ActionOutcome(True, f"spoke recently; quiet for {int(quiet_for / 60)}m more")

    candidate = None
    for task in store.tasks_in_state("blocked", "needs_triage"):
        if store._is_snoozed(task):
            continue
        if task["state"] == "needs_triage":
            # Unasked briefs are the reconciler's job; she only chases the
            # question she already sent and he left unanswered.
            try:
                asked = float(task.get("asked_at") or 0)
            except (TypeError, ValueError):
                asked = 0
            if not asked or now - asked < NUDGE_ASKED_AGE_SECONDS:
                continue
        candidate = task
        break
    if candidate is None:
        return ActionOutcome(True, "nothing is waiting on him")

    key = f"nudge:{candidate['id']}:{candidate['state']}"
    decision = _nudge_decision(_nudge_text(candidate), key)
    sent = decision == "sent"
    # The shift starts when the authority takes the nudge, not when it lands.
    # Deferred through quiet hours it still goes out in the morning; held for
    # approval or suppressed as a duplicate, it was already said. Only a nudge
    # that failed to reach him is unsaid, and only that is retried next pass.
    # Starting the clock on an immediate send alone meant quiet hours never
    # started it: she re-raised the same task every fifteen minutes all night
    # and he woke to the pile.
    if decision in NUDGE_SPOKEN_DECISIONS:
        state.update(nudged_at=now, task_id=candidate["id"], key=key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return ActionOutcome(
        True,
        f"nudged him about #{candidate['id']}" if sent
        else f"#{candidate['id']} is waiting, but the notice was held",
        output={"task_id": candidate["id"], "state": candidate["state"], "sent": sent},
    )


# His number only reaches iMessage while the SIM-swap registration holds, and
# Apple re-checks it on its own schedule. Swap well before that.
NUMBER_SWAP_REMINDER_DAYS = 42
LINE_DOWN_ALERT_SECONDS = 15 * 60


def check_phone_health(payload: dict[str, Any]) -> ActionOutcome:
    """Watch Serena's own iMessage server and his number's registration.

    Her server lives in a second macOS user that does not log in by itself
    after a reboot, so a dead server is the expected failure. It is reported
    through the hub self-thread, which does not depend on her server.
    """

    import json
    import time
    from pathlib import Path

    from core import bluebubbles_line, phone_line

    if _configuration(payload):
        return ActionOutcome(False, "serena.phone.health accepts no schedule payload")
    if not bluebubbles_line.enabled():
        return ActionOutcome(True, "Serena's own iMessage line is not configured here")
    state_path = Path.home() / ".local" / "state" / "serena" / "phone-health.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    now = time.time()
    output: dict[str, Any] = {}
    up = bluebubbles_line.ping()
    output["server_up"] = up
    if up:
        if state.get("down_since"):
            phone_line.send("i'm back on my own line.", key=f"line-up-{int(now)}")
        state["down_since"] = 0
        state["down_alerted"] = False
    else:
        state["down_since"] = state.get("down_since") or now
        if not state.get("down_alerted") and now - state["down_since"] >= LINE_DOWN_ALERT_SECONDS:
            sent = phone_line.send_fallback(
                "serena: my own imessage server is down (probably the mac vm restarted). "
                "log into the Serena user on the BlueBubbles Mac VM to bring it back.",
                key=f"line-down-{int(state['down_since'])}")
            state["down_alerted"] = bool(sent)
            output["alerted"] = bool(sent)

    number = str(bluebubbles_line.settings().get("watch_number") or "")
    if up and number and now - float(state.get("number_checked_at") or 0) >= 24 * 3600:
        registered = bluebubbles_line.imessage_available(number)
        output["number_registered"] = registered
        if registered is not None:
            state["number_checked_at"] = now
            if registered:
                state["number_ok_since"] = state.get("number_ok_since") or now
                state["number_alerted"] = False
            elif not state.get("number_alerted"):
                if phone_line.send(
                        "your number fell off imessage. put the sim in the xr for 5 minutes "
                        "(wi-fi on), then move it back to the s23.",
                        key=f"number-dropped-{int(now // 86400)}"):
                    state["number_alerted"] = True
                state["number_ok_since"] = 0
    swapped_at = float(state.get("number_swapped_at") or 0)
    if swapped_at and now - swapped_at >= NUMBER_SWAP_REMINDER_DAYS * 86400 and not state.get(
            "swap_reminded"):
        if phone_line.send(
                "time for the sim refresh: sim into the xr for 5 minutes when you're home, "
                "then back to the s23. text me \"swapped\" after.",
                key=f"swap-due-{int(swapped_at)}"):
            state["swap_reminded"] = True
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return ActionOutcome(True, "server up" if up else "server down", output=output)


# The whole registry. A schedule may name exactly one of these keys.
from core.knowledge_maintenance import scheduled_pass as maintain_knowledge


def run_doctor(payload: dict[str, Any]) -> ActionOutcome:
    """Run the system doctor and tell him only when the answer changes.

    core.doctor existed for a month and nothing ran it. It was written after
    `fleet serve` spent two days on stale code, and then it sat as a module
    somebody had to remember to call -- so on 2026-09-18 the PC runtime was
    thirty-five commits behind with a model policy that refused every coding
    brief, and the tool that says exactly that in one line went unread.

    A check nobody runs is not a check. This is how it runs.
    """

    from core import doctor
    from core.machine_context import machine_name

    # `channel` is the one thing worth configuring per machine; anything else
    # in the payload is a schedule trying to narrow what gets checked, which is
    # how a doctor stops covering the thing that breaks.
    unknown = set(_configuration(payload)) - {"channel"}
    if unknown:
        return ActionOutcome(
            False, f"serena.doctor takes no {', '.join(sorted(unknown))}")

    where = machine_name()
    report = doctor.run()
    broken = report.failures + report.warnings
    output = {
        "ok": report.ok,
        "failures": [finding.name for finding in report.failures],
        "warnings": [finding.name for finding in report.warnings],
    }
    if not broken:
        return ActionOutcome(True, "nothing broken", output=output)
    if not report.failures:
        # A warning is drift worth recording, not worth a text. "HEAD is
        # level with origin/master but nothing has fetched for 41 hours" is
        # true of every deploy checkout and means nothing to him; it stays in
        # `chats doctor` and the action's output, and never reaches his phone.
        return ActionOutcome(
            True, f"{len(report.warnings)} warning(s), nothing broken", output=output)

    broken = report.failures
    lead = broken[0]
    extra = f" (+{len(broken) - 1} more)" if len(broken) > 1 else ""
    # Name the machine: this runs on both, and "the repo is behind" means
    # something different depending on which one is saying it.
    summary = f"{where}: {lead.name}: {lead.detail}"[:DOCTOR_SUMMARY_LIMIT] + extra
    return ActionOutcome(
        True,
        summary,
        notify={
            "kind": "serena.doctor",
            "summary": summary,
            # Telegram by default: a machine that has gone wrong is worth
            # hearing about wherever he is, not only if he happens to be
            # sitting in front of the one that broke.
            "channel": str(payload.get("channel") or "telegram"),
            # A failure stops work; a warning is a drift he should know about
            # before it becomes one. Neither is worth waking him for.
            "urgency": "normal",
            # One notice per distinct shape of breakage, so a problem that
            # persists for a day does not become an hourly nag. A new or fixed
            # check changes the shape, and he hears about it then.
            "dedupe_key": f"doctor:{where}:" + ",".join(
                sorted(finding.name for finding in broken)),
        },
        output=output,
    )


def journal_nightly(payload: dict[str, Any]) -> ActionOutcome:
    """Draft his journal once a day at JOURNAL_SEND_AT, and catch up missed days.

    The scheduler runs on intervals and drifts, so this runs every few minutes
    and decides for itself. From JOURNAL_SEND_AT until midnight it drafts,
    texts and (with questions) rings about today; a tick that lands just after
    midnight still sends that day, until JOURNAL_GRACE_UNTIL_HOUR. He chose
    11:45pm knowing it is inside quiet hours, so this one send is exempt.

    A day the PC slept through entirely is still drafted the next morning --
    once quiet hours are over, by text only -- so no day is left blank.
    """

    if payload:
        return ActionOutcome(False, "serena.journal.nightly accepts no schedule payload")
    import time
    from datetime import datetime, timedelta

    from core.journal import nightly, store
    from core.notification_senders import default_authority

    now = datetime.now(nightly.TZ)
    today = now.date()
    send_at = now.replace(hour=JOURNAL_SEND_AT[0], minute=JOURNAL_SEND_AT[1],
                          second=0, microsecond=0)

    def unsent(day) -> bool:
        record = store.load_day(day.isoformat())
        return not (record and record.get("sent_at"))

    due = None
    if now >= send_at:
        due = today
    elif now.hour < JOURNAL_GRACE_UNTIL_HOUR:
        due = today - timedelta(days=1)
    if due is not None:
        if not unsent(due):
            return ActionOutcome(True, "nothing due")
        try:
            outcome = nightly.run_nightly(due)
        except Exception as error:
            return ActionOutcome(False, f"journal for {due} failed: {_why(error)}")
        return ActionOutcome(True, f"journal for {due}: {outcome}", output=outcome)

    yesterday = today - timedelta(days=1)
    if not default_authority().policy.in_quiet_hours(time.time()) and unsent(yesterday):
        try:
            nightly.build(yesterday)
            outcome = nightly.send(yesterday, call=False)
        except Exception as error:
            return ActionOutcome(False, f"catch-up journal for {yesterday} failed: {_why(error)}")
        return ActionOutcome(True, f"caught up journal for {yesterday}: {outcome}", output=outcome)
    return ActionOutcome(True, "nothing due")


# His choice: 11:45pm, so the day is actually over when she writes it up. That
# is inside quiet hours, which is why the send above does not consult them.
JOURNAL_SEND_AT = (23, 45)
# A tick that lands just past midnight still sends the day that just ended.
JOURNAL_GRACE_UNTIL_HOUR = 1


REVIEWED_ACTIONS = {
    'serena.knowledge.maintenance': maintain_knowledge,
    "serena.obligations.sweep": sweep_obligations,
    "serena.obligations.report": report_outstanding,
    "serena.notifications.flush": flush_notifications,
    "serena.surfaces.publish": publish_surface_events,
    "serena.fleet.start": start_ready_fleet_task,
    "serena.fleet.reconcile": reconcile_fleet_tasks,
    "serena.phone.poll": poll_phone_line,
    "serena.phone.nudge": nudge_phone_line,
    "serena.phone.health": check_phone_health,
    "serena.doctor": run_doctor,
    "serena.journal.nightly": journal_nightly,
}


def register_all(scheduler: Any) -> Any:
    """Attach every reviewed action to a scheduler."""

    for name, handler in REVIEWED_ACTIONS.items():
        scheduler.register_action(name, handler)
    return scheduler
