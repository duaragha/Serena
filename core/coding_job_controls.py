"""Conversational controls over an accepted coding job.

The audit's fourth cause was that there is no steering loop: the overlay could
show a job and hide it, and nothing else. Raghav could not ask what it was
doing, correct it, call it off, or pick it back up, so a wrong job ran to the
end and a right job that drifted stayed drifted.

The durable half of that already exists in the inbox: cancel, steer, and
resume are persisted controls and the supervisor honours them mid-attempt.
What was missing is the half he actually uses, which is his mouth. He says
"cancel that" and "also make it handle the empty case", not a UUID and a
subcommand, so this module turns a spoken reference into exactly one job and
refuses instead of guessing when it cannot.

Judgement about whether he asked for a control stays with Serena, the same way
starting work does. This layer only proves the request is grounded in a live
spoken turn and names one real job.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.work_authority import (
    DEFAULT_AUDIT_PATH,
    SPOKEN_PROTOCOLS,
    WorkAuthorityResult,
    _append_audit,
)

MAX_STEER_CHARS = 4_000

ACTIONS = ("status", "cancel", "steer", "resume")
CONTROL_PROTOCOLS = SPOKEN_PROTOCOLS | {"cli"}

# States the supervisor can still act on. Queued controls matter too: a job
# must not become unstoppable during the claim or persisted-resume windows.
RUNNING_STATES = frozenset({"working"})
ACTIVE_STATES = frozenset({"queued", "claimed", "resume_queued", "working"})
CANCELLABLE_STATES = ACTIVE_STATES
STEERABLE_STATES = ACTIVE_STATES
RESUMABLE_STATES = frozenset({"failed", "cancelled"})

# "that", "it", "the job" mean whichever one is live. They carry no project
# name, so they only resolve when exactly one job is eligible.
_PRONOUN = re.compile(
    r"^\s*(?:the\s+|that\s+|this\s+|my\s+|your\s+)*"
    r"(?:it|that|this|one|thing|job|work|task|coding|coding\s+job|"
    r"work\s+item|the\s+one)\s*$",
    re.IGNORECASE,
)
# "the last one" is a real preference for the newest, not an ambiguity.
_NEWEST = re.compile(
    r"^\s*(?:the\s+)?(?:last|latest|most\s+recent|newest|previous|current|"
    r"running|active)(?:\s+(?:one|job|work|task))?\s*$",
    re.IGNORECASE,
)
_ID = re.compile(r"^[0-9a-f]{4,}(?:-[0-9a-f]+)*$", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9]+")
_STOPWORD = frozenset(
    {
        "the", "a", "an", "my", "your", "that", "this", "it", "job", "work",
        "task", "coding", "on", "in", "for", "to", "of", "and", "please",
        "one", "thing", "project", "repo", "repository",
    }
)


class JobResolutionError(ValueError):
    """The spoken reference did not name exactly one actionable job."""


@dataclass(frozen=True, slots=True)
class JobControlResult:
    ok: bool
    action: str
    reason: str
    item_id: str = ""
    spoken: str = ""
    job: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _inbox(inbox: Any = None) -> Any:
    if inbox is not None:
        return inbox
    from core.voice_inbox import get_default_voice_inbox

    return get_default_voice_inbox()


def _eligible(job: Mapping[str, Any], action: str) -> bool:
    state = str(job.get("state") or "")
    if action == "status":
        return True
    if action == "cancel":
        return state in CANCELLABLE_STATES
    if action == "steer":
        return state in STEERABLE_STATES
    if action == "resume":
        return state in RESUMABLE_STATES and bool(str(job.get("session_id") or "").strip())
    return False


def _name(job: Mapping[str, Any]) -> str:
    project = _clean(job.get("project"))
    short = str(job.get("item_id") or "")[:8]
    return f"{project or 'unknown project'} ({short})" if short else project


def _ineligible_reason(job: Mapping[str, Any], action: str) -> str:
    state = str(job.get("state") or "") or "queued"
    if action in {"cancel", "steer"}:
        if state in RESUMABLE_STATES:
            return f"that job already stopped ({state}), so there is nothing to {action}"
        if state == "completed":
            return "that job already finished"
        return f"that job is {state}, not running, so there is nothing to {action}"
    if action == "resume":
        if state in RUNNING_STATES:
            return "that job is still running, so there is nothing to resume"
        if not str(job.get("session_id") or "").strip():
            return "that job never persisted a Codex session, so it cannot be resumed"
        return f"that job is {state} and cannot be resumed"
    return f"that job is {state}"


def _tokens(value: object) -> set[str]:
    return {word for word in _WORD.findall(str(value or "").casefold()) if word not in _STOPWORD}


def resolve_job(
    reference: object,
    *,
    action: str = "status",
    jobs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return the one job a spoken reference names, or refuse to guess."""

    action = str(action).strip().casefold() or "status"
    if action not in ACTIONS:
        raise JobResolutionError(f"unsupported coding job control: {action}")
    ordered = [dict(job) for job in jobs]
    if not ordered:
        raise JobResolutionError("there are no coding jobs on record")
    eligible = [job for job in ordered if _eligible(job, action)]
    text = _clean(reference)

    if text and _ID.match(text.replace(" ", "")):
        wanted = text.replace(" ", "").casefold()
        matches = [job for job in ordered if str(job["item_id"]).casefold().startswith(wanted)]
        if not matches:
            raise JobResolutionError(f"no coding job starts with {wanted[:12]}")
        if len(matches) > 1:
            raise JobResolutionError(f"more than one coding job starts with {wanted[:12]}")
        job = matches[0]
        if not _eligible(job, action):
            raise JobResolutionError(_ineligible_reason(job, action))
        return job

    if not text or _PRONOUN.match(text):
        if action == "status":
            active = [job for job in ordered if str(job.get("state") or "") in ACTIVE_STATES]
            if active:
                eligible = active
        if not eligible:
            raise JobResolutionError(_nothing_eligible(action, ordered))
        if len(eligible) > 1:
            names = ", ".join(_name(job) for job in eligible[:4])
            raise JobResolutionError(f"which coding job: {names}?")
        return eligible[0]

    if _NEWEST.match(text):
        if not eligible:
            raise JobResolutionError(_nothing_eligible(action, ordered))
        return eligible[0]

    wanted = _tokens(text)
    if not wanted:
        raise JobResolutionError("i could not tell which coding job that means")
    scored: list[tuple[int, dict[str, Any]]] = []
    for job in eligible:
        haystack = _tokens(job.get("project")) | _tokens(Path(str(job.get("project_root") or "")).name)
        score = 2 * len(wanted & haystack)
        score += len(wanted & _tokens(job.get("request")))
        if score:
            scored.append((score, job))
    if not scored:
        if any(_tokens(job.get("project")) & wanted for job in ordered):
            stale = next(job for job in ordered if _tokens(job.get("project")) & wanted)
            raise JobResolutionError(_ineligible_reason(stale, action))
        if eligible:
            # Something is running, it is just not the thing he named. Saying
            # "nothing is running" here would be a lie he could act on.
            names = ", ".join(_name(job) for job in eligible[:4])
            raise JobResolutionError(f"nothing by that name; what is running is {names}")
        raise JobResolutionError(_nothing_eligible(action, ordered))
    best = max(score for score, _job in scored)
    winners = [job for score, job in scored if score == best]
    if len(winners) > 1:
        names = ", ".join(_name(job) for job in winners[:4])
        raise JobResolutionError(f"which coding job: {names}?")
    return winners[0]


def _nothing_eligible(action: str, jobs: Sequence[Mapping[str, Any]]) -> str:
    if action in {"cancel", "steer"}:
        return "nothing is running right now"
    if action == "resume":
        return "nothing stopped is waiting to be picked back up"
    return "there are no coding jobs on record"


def spoken_status(snapshot: Mapping[str, Any]) -> str:
    """One or two plain sentences about a job, in Serena's register."""

    state = str(snapshot.get("state") or "queued")
    # Name the project from the validated root only. Pre-redesign rows have no
    # brief and no root, and saying a placeholder name out loud reads as if the
    # job knew where it was working when it did not.
    root = _clean(snapshot.get("project_root"))
    project = (Path(root).name if root else "") or "an unresolved project"
    short = str(snapshot.get("item_id") or "")[:8]
    model = snapshot.get("model") or {}
    progress = snapshot.get("progress") or {}
    evidence = snapshot.get("evidence") or {}
    review = snapshot.get("review") or {}
    changes = list(snapshot.get("changes") or [])
    tests = list(snapshot.get("tests") or [])
    proof = list(snapshot.get("live_proof") or [])

    reported = _clean(model.get("reported")) or _clean(model.get("requested"))
    effort = _clean(model.get("reported_effort")) or _clean(model.get("effort"))
    identity = f"{reported} at {effort}" if reported and effort else reported or "an unreported model"
    attempt = int(progress.get("attempt") or 0)

    head = f"job {short} in {project} is {state}"
    if attempt:
        head += f", attempt {attempt} on {identity}"
    elif reported:
        head += f", on {identity}"

    parts = [head + "."]
    if changes:
        shown = ", ".join(changes[:4])
        more = f" and {len(changes) - 4} more" if len(changes) > 4 else ""
        parts.append(f"changed {len(changes)} file{'s' if len(changes) != 1 else ''}: {shown}{more}.")
    if tests:
        failed = [entry for entry in tests if entry.get("exit_code") != 0]
        if failed:
            parts.append(f"{len(failed)} of {len(tests)} recorded test commands did not exit clean.")
        else:
            parts.append(f"{len(tests)} test command{'s' if len(tests) != 1 else ''} exited clean.")
    if proof:
        parts.append(f"{len(proof)} live proof command{'s' if len(proof) != 1 else ''} recorded.")
    errors = [str(value) for value in evidence.get("errors") or []]
    if errors:
        parts.append(f"evidence is incomplete: {errors[0]}")
    elif state == "completed" and evidence.get("complete"):
        parts.append("evidence is complete.")
    review_state = str(review.get("state") or "")
    if review_state and review_state not in {"not_decided", "skipped"}:
        verdict = review.get("approved")
        if verdict is True:
            parts.append("review passed.")
        elif verdict is False:
            parts.append(f"review pushed back: {_clean(review.get('reason'))[:200]}")
    error = _clean(progress.get("last_error"))
    if error:
        parts.append(f"last error: {error[:200]}")
    return " ".join(parts)


def job_status(
    reference: object = "",
    *,
    inbox: Any = None,
    limit: int = 20,
) -> JobControlResult:
    """Read one job's durable state. Read-only, so any surface may ask."""

    store = _inbox(inbox)
    jobs = store.recent_jobs(limit=limit)
    try:
        job = resolve_job(reference, action="status", jobs=jobs)
    except JobResolutionError as error:
        return JobControlResult(False, "status", str(error))
    item_id = str(job["item_id"])
    snapshot = store.overlay_snapshot(item_id)
    if snapshot is None:
        return JobControlResult(False, "status", "that job is no longer on record", item_id)
    return JobControlResult(
        True,
        "status",
        "read",
        item_id,
        spoken_status(snapshot),
        snapshot,
    )


def control_job(
    action: str,
    *,
    reference: object = "",
    text: str = "",
    origin: Mapping[str, object],
    inbox: Any = None,
    audit_path: Path = DEFAULT_AUDIT_PATH,
) -> JobControlResult:
    """Cancel, steer, or resume one job on his live spoken authority."""

    action = str(action).strip().casefold()
    steer_text = _clean(text)
    if action == "status":
        return job_status(reference, inbox=inbox)
    if action not in {"cancel", "steer", "resume"}:
        return JobControlResult(False, action, f"unsupported coding job control: {action}")

    spoken = _clean(origin.get("text"))
    protocol = str(origin.get("protocol") or "").strip().lower()
    denial = ""
    if not spoken:
        denial = "no originating spoken turn is bound to this tool call"
    elif protocol not in CONTROL_PROTOCOLS:
        denial = (
            "coding jobs are controlled only from a live spoken turn or the local CLI, "
            f"not a {protocol or 'unknown'} turn"
        )
    elif action == "steer" and not steer_text:
        denial = "steering needs the correction in words"
    elif action == "steer" and len(steer_text) > MAX_STEER_CHARS:
        denial = "that correction is too long to hand to the worker"
    if denial:
        result = JobControlResult(False, action, denial)
        _audit(result, origin=origin, audit_path=audit_path)
        return result

    store = _inbox(inbox)
    try:
        job = resolve_job(reference, action=action, jobs=store.recent_jobs())
    except JobResolutionError as error:
        result = JobControlResult(False, action, str(error))
        _audit(result, origin=origin, audit_path=audit_path)
        return result

    item_id = str(job["item_id"])
    try:
        if action == "cancel":
            store.request_cancel(item_id)
            reason = "cancellation requested"
            spoken_result = f"stopping job {item_id[:8]} in {_clean(job.get('project')) or 'that project'}."
        elif action == "steer":
            store.add_steering(item_id, steer_text)
            reason = "steering queued for the running session"
            spoken_result = (
                f"passed that to job {item_id[:8]}; it continues in the same Codex session."
            )
        else:
            store.request_resume(item_id)
            reason = "resume queued on the persisted Codex session"
            spoken_result = (
                f"picking job {item_id[:8]} back up in the same Codex session."
            )
    except ValueError as error:
        result = JobControlResult(False, action, _clean(error) or "the control was refused", item_id)
        _audit(result, origin=origin, audit_path=audit_path)
        return result

    result = JobControlResult(True, action, reason, item_id, spoken_result, dict(job))
    _audit(result, origin=origin, audit_path=audit_path)
    return result


def _audit(
    result: JobControlResult,
    *,
    origin: Mapping[str, object],
    audit_path: Path,
) -> None:
    """Reuse the work-authority ledger so one file holds every job decision."""

    _append_audit(
        WorkAuthorityResult(
            result.ok,
            f"{result.action}: {result.reason}",
            result.action,
            result.item_id,
        ),
        origin=origin,
        audit_path=audit_path,
    )
