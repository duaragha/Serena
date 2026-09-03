"""Capability-brokered Fleet access for Serena's resident brain."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from mcp.types import ToolAnnotations

from core.brain_laptop_tools import current_turn, recent_turn_texts
from core.coding_job_contract import (
    RepositoryResolutionError,
    resolve_repository_root,
)
from core.fleet_supervisor import (
    get_run,
    list_runs,
    retry_run,
    start_run,
    steer_run,
    stop_run,
)

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_BROKERED_ACTION = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)

_ACTIVE_STATES = frozenset({"queued", "running", "stopping"})
_RETRYABLE_STATES = frozenset({"failed", "cancelled"})
_PRONOUN = re.compile(
    r"^\s*(?:the\s+|that\s+|this\s+|my\s+|your\s+)*"
    r"(?:it|that|this|one|run|fleet|fleet\s+run|task|job)\s*$",
    re.IGNORECASE,
)
_LATEST = re.compile(
    r"^\s*(?:the\s+)?(?:last|latest|newest|most\s+recent|current|active|running)"
    r"(?:\s+(?:one|run|fleet|task|job))?\s*$",
    re.IGNORECASE,
)
_ID = re.compile(r"^[0-9a-f]{4,}(?:-[0-9a-f]+)*$", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "for",
        "fleet",
        "in",
        "it",
        "job",
        "my",
        "of",
        "on",
        "one",
        "please",
        "run",
        "task",
        "that",
        "the",
        "this",
        "to",
        "your",
    }
)
# Naming Fleet in the live turn is the whole contract. There used to be a
# verb whitelist (run|start|launch|use) on top of it, and on 2026-08-12 it
# refused "create a fleet to look into this and fix it", a turn no human
# could read as anything but a launch order, while "how is the fleet run
# going" would have sailed through on the word "run". Verb lists refuse the
# real instruction and pass the accidental one; her judgement decides intent.
_FLEET_SIGNAL = re.compile(r"\bfleet\b", re.IGNORECASE)
_SPOKEN_PROTOCOLS = frozenset({"voice", "desk"})


class FleetResolutionError(ValueError):
    """A natural reference did not resolve to exactly one Fleet run."""


_START_FLEET_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string"},
        "activity": {"type": "string"},
        "project": {"type": "string"},
        "provider_mode": {"type": "string"},
        "worker_count": {"type": "integer", "minimum": 1, "maximum": 4},
    },
    "required": ["task", "activity", "project"],
    "additionalProperties": False,
}


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _bounded_ids(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [clean for item in value[:16] if (clean := _clean(item)[:120])]


def _words(value: object) -> set[str]:
    return {
        word
        for word in _WORD.findall(str(value or "").casefold())
        if word not in _STOPWORDS
    }


def _eligible(run: Mapping[str, Any], action: str) -> bool:
    state = str(run.get("state") or "")
    if action == "status":
        return True
    if action in {"cancel", "steer"}:
        return state in _ACTIVE_STATES
    if action == "retry":
        return state in _RETRYABLE_STATES
    return False


def _label(run: Mapping[str, Any]) -> str:
    short = str(run.get("run_id") or "")[:8]
    project = Path(str(run.get("cwd") or "")).name or "unknown project"
    return f"{project} ({short})"


def resolve_fleet_run(
    reference: object,
    *,
    action: str = "status",
    runs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Resolve a spoken Fleet reference without guessing between live runs."""

    ordered = [dict(run) for run in runs]
    if not ordered:
        raise FleetResolutionError("there are no Fleet runs on record")
    eligible = [run for run in ordered if _eligible(run, action)]
    text = _clean(reference)

    if text and _ID.match(text.replace(" ", "")):
        wanted = text.replace(" ", "").casefold()
        matches = [
            run
            for run in ordered
            if str(run.get("run_id") or "").casefold().startswith(wanted)
        ]
        if not matches:
            raise FleetResolutionError(f"no Fleet run starts with {wanted[:12]}")
        if len(matches) > 1:
            raise FleetResolutionError(f"more than one Fleet run starts with {wanted[:12]}")
        if not _eligible(matches[0], action):
            raise FleetResolutionError(
                f"that Fleet run is {matches[0].get('state') or 'unknown'} and cannot {action}"
            )
        return matches[0]

    if not text or _PRONOUN.match(text):
        active = [run for run in eligible if str(run.get("state") or "") in _ACTIVE_STATES]
        candidates = active or eligible
        if not candidates:
            raise FleetResolutionError(f"there is no Fleet run available to {action}")
        if active and len(active) > 1:
            names = ", ".join(_label(run) for run in active[:4])
            raise FleetResolutionError(f"which Fleet run: {names}?")
        return candidates[0]

    if _LATEST.match(text):
        if not eligible:
            raise FleetResolutionError(f"there is no Fleet run available to {action}")
        return eligible[0]

    wanted = _words(text)
    scored: list[tuple[int, dict[str, Any]]] = []
    for run in eligible:
        project_words = _words(Path(str(run.get("cwd") or "")).name)
        score = 3 * len(wanted & project_words)
        score += len(wanted & _words(run.get("task")))
        if score:
            scored.append((score, run))
    if not scored:
        raise FleetResolutionError("nothing in Fleet matches that reference")
    best = max(score for score, _run in scored)
    winners = [run for score, run in scored if score == best]
    if len(winners) > 1:
        names = ", ".join(_label(run) for run in winners[:4])
        raise FleetResolutionError(f"which Fleet run: {names}?")
    return winners[0]


def _run_projection(run: Mapping[str, Any]) -> dict[str, Any]:
    workers = []
    phases = list(run.get("phases") or [])
    for phase in phases:
        for leg in phase.get("legs") or []:
            attempt = leg.get("current_attempt") or {}
            workers.append(
                {
                    "phase": phase.get("display_name") or phase.get("name"),
                    "worker_key": _clean(leg.get("worker_key"))[:120] or None,
                    "worker_label": _clean(leg.get("worker_label"))[:120] or None,
                    "assignment": _clean(leg.get("assignment"))[:500] or None,
                    "assignment_ids": _bounded_ids(leg.get("assignment_ids")),
                    "review_target_ids": _bounded_ids(leg.get("review_target_ids")),
                    "role": leg.get("role"),
                    "provider": leg.get("provider") or leg.get("runtime"),
                    "requested_model": leg.get("model"),
                    "actual_model": attempt.get("actual_model"),
                    "actual_effort": attempt.get("actual_effort"),
                    "state": leg.get("state"),
                    "error": _clean(attempt.get("error"))[:300] or None,
                }
            )
    task = _clean(run.get("task"))
    error = _clean(run.get("error"))
    result = _clean(run.get("result_text"))
    progress = run.get("progress") or {}
    completed_phases = sum(phase.get("state") == "completed" for phase in phases)
    policy = run.get("policy") if isinstance(run.get("policy"), Mapping) else {}
    selection = policy.get("provider_selection") if isinstance(policy, Mapping) else {}
    if not isinstance(selection, Mapping):
        selection = policy.get("routing") if isinstance(policy.get("routing"), Mapping) else {}
    scaling = policy.get("scaling") if isinstance(policy.get("scaling"), Mapping) else {}
    first_phase = phases[0] if phases else {}
    provider_counts = {"codex": 0, "claude": 0}
    for leg in first_phase.get("legs") or []:
        provider = str(leg.get("provider") or leg.get("runtime") or "").strip().casefold()
        if provider in provider_counts:
            provider_counts[provider] += 1
    requested_mode = _clean(
        policy.get("requested_provider_mode")
        or selection.get("requested_mode")
        or selection.get("provider_mode")
        or scaling.get("provider_mode")
        or "auto"
    ).casefold()
    selected_mode = _clean(
        policy.get("provider_mode")
        or selection.get("selected_mode")
        or selection.get("selected_provider_mode")
        or policy.get("selected_provider_mode")
        or requested_mode
    ).casefold()
    selection_reason = _clean(
        selection.get("reason")
        or policy.get("provider_selection_reason")
        or scaling.get("provider_reason")
        or scaling.get("reason")
    )
    return {
        "run_id": run.get("run_id"),
        "short_id": str(run.get("run_id") or "")[:8],
        "state": run.get("state"),
        "activity": run.get("activity"),
        "project": Path(str(run.get("cwd") or "")).name,
        "task": task[:500],
        "current_phase": run.get("current_phase_display") or run.get("current_phase"),
        "step_progress": {
            "completed_steps": int(progress.get("completed") or 0),
            "total_steps": int(progress.get("total") or 0),
        },
        "agent_count": int(run.get("agent_count") or 0),
        "chat_count": int(run.get("chat_count") or 0),
        "provider_routing": {
            "requested_mode": requested_mode,
            "selected_mode": selected_mode,
            "provider_counts": provider_counts,
            "reason": selection_reason[:500] or None,
        },
        "phase_progress": {
            "completed_phases": completed_phases,
            "total_phases": len(phases),
        },
        "workers": workers,
        "error": error[:500] or None,
        "result_preview": result[:1_200] or None,
        "completed_at": run.get("completed_at"),
    }


def _spoken_status(run: Mapping[str, Any]) -> str:
    progress = run.get("progress") or {}
    short = str(run.get("run_id") or "")[:8]
    state = str(run.get("state") or "unknown")
    phase = str(run.get("current_phase_display") or run.get("current_phase") or "")
    done = int(progress.get("completed") or 0)
    total = int(progress.get("total") or 0)
    project = Path(str(run.get("cwd") or "")).name or "its project"
    text = f"Fleet {short} for {project} is {state}"
    if phase:
        text += f" in {phase}"
    if total:
        text += f", with {done} of {total} agent steps complete"
    error = _clean(run.get("error"))
    if error:
        text += f". Last error: {error[:240]}"
    return text + "."


def fleet_status(reference: object = "") -> dict[str, Any]:
    summaries = list_runs(limit=50)
    selected = resolve_fleet_run(reference, action="status", runs=summaries)
    run = get_run(str(selected["run_id"]))
    if run is None:
        raise FleetResolutionError("that Fleet run is no longer on record")
    projection = _run_projection(run)
    projection["spoken"] = _spoken_status(run)
    return projection


def _control_denial(action: str, text: str, origin: Mapping[str, object]) -> str:
    spoken = _clean(origin.get("text"))
    protocol = str(origin.get("protocol") or "").strip().lower()
    if not spoken:
        return "no originating spoken turn is bound to this Fleet control"
    if protocol not in _SPOKEN_PROTOCOLS:
        return f"Fleet controls require a live spoken turn, not {protocol or 'unknown'}"
    if action == "steer" and not _clean(text):
        return "steering needs the complete correction in words"
    if len(_clean(text)) > 4_000:
        return "that steering message is too long"
    return ""


def control_fleet_run(
    action: str,
    *,
    reference: object = "",
    text: str = "",
    origin: Mapping[str, object],
) -> dict[str, Any]:
    action = str(action or "").strip().casefold()
    if action not in {"cancel", "retry", "steer"}:
        return {"ok": False, "changed": False, "error": f"unsupported Fleet control: {action}"}
    denial = _control_denial(action, text, origin)
    if denial:
        return {"ok": False, "changed": False, "error": denial}
    try:
        selected = resolve_fleet_run(
            reference,
            action=action,
            runs=list_runs(limit=50),
        )
        run_id = str(selected["run_id"])
        if action == "cancel":
            run = stop_run(run_id)
        elif action == "retry":
            run = retry_run(run_id)
        else:
            run = steer_run(run_id, _clean(text))
    except (FleetResolutionError, KeyError, ValueError, RuntimeError) as error:
        return {"ok": False, "changed": False, "error": _clean(error)[:500]}
    return {
        "ok": True,
        "changed": True,
        "action": action,
        "run": _run_projection(run),
    }


def start_fleet(
    task: str,
    *,
    activity: str,
    project: str,
    origin: Mapping[str, object],
    provider_mode: str = "auto",
    worker_count: int | None = None,
) -> dict[str, Any]:
    spoken = _clean(origin.get("text"))
    protocol = str(origin.get("protocol") or "").strip().lower()
    clean_task = _clean(task)
    selected_activity = str(activity or "auto").strip().casefold() or "auto"
    selected_provider = str(provider_mode or "auto").strip().casefold() or "auto"
    if protocol not in _SPOKEN_PROTOCOLS:
        return {"ok": False, "started": False, "error": "Fleet starts require a live spoken turn"}
    if not _FLEET_SIGNAL.search(spoken):
        return {
            "ok": False,
            "started": False,
            "error": "the current turn does not name Fleet",
        }
    if selected_activity not in {"auto", "coding", "research"}:
        return {"ok": False, "started": False, "error": "activity must be auto, coding, or research"}
    if selected_provider not in {"auto", "balanced", "codex", "claude"}:
        return {
            "ok": False,
            "started": False,
            "error": "provider_mode must be auto, balanced, codex, or claude",
        }
    if worker_count is not None and (
        isinstance(worker_count, bool)
        or not isinstance(worker_count, int)
        or worker_count < 1
        or worker_count > 4
    ):
        return {"ok": False, "started": False, "error": "worker_count must be 1 through 4"}
    if not clean_task:
        return {"ok": False, "started": False, "error": "Fleet needs a task"}
    window = " ".join([spoken, *recent_turn_texts()])
    if not (_words(clean_task) & _words(window)):
        # Grounding reads the recent conversation, not one sentence, for the
        # same reason memory deletes do: "create a fleet to fix it" arrives
        # AFTER the turn that described what "it" is.
        return {
            "ok": False,
            "started": False,
            "error": "the proposed Fleet task does not match the current request",
        }
    try:
        root = resolve_repository_root(
            spoken + "\n" + clean_task,
            project_hint=_clean(project),
        )
        run = start_run(
            clean_task,
            activity=selected_activity,
            provider_mode=selected_provider,
            worker_count=worker_count,
            cwd=str(root),
            origin_session_id=None,
            origin_agent="serena",
        )
    except (RepositoryResolutionError, ValueError, RuntimeError) as error:
        return {"ok": False, "started": False, "error": _clean(error)[:500]}
    return {"ok": True, "started": True, "run": _run_projection(run)}


def _text(value: object) -> dict:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            }
        ]
    }


@tool(
    "fleet_run_status",
    "Read the durable Serena Fleet tab state for one run, including its phase, "
    "progress, requested and actual worker models, errors, and a bounded result "
    "preview. reference may be empty for the one active run, 'latest', a short "
    "run id, a project, or words from its task. Use this whenever Raghav asks "
    "what Fleet is doing instead of guessing from a worker chat.",
    {"reference": str},
    annotations=_READ_ONLY,
)
async def fleet_run_status(args):
    try:
        return _text(await asyncio.to_thread(fleet_status, args.get("reference") or ""))
    except FleetResolutionError as error:
        return _text({"ok": False, "found": False, "error": str(error)})


@tool(
    "control_fleet_run",
    "Cancel, retry, or steer an existing Fleet run on Raghav's current live "
    "request. reference may be empty for the one active run, 'latest', a short "
    "run id, project, or task words. text is required for steer and is passed "
    "only to future Fleet legs. Never claim it changed unless ok is true.",
    {"action": str, "reference": str, "text": str},
    annotations=_BROKERED_ACTION,
)
async def control_fleet_run_tool(args):
    return _text(
        await asyncio.to_thread(
            control_fleet_run,
            str(args.get("action") or ""),
            reference=args.get("reference") or "",
            text=str(args.get("text") or ""),
            origin=current_turn(),
        )
    )


@tool(
    "start_fleet_run",
    "Start Serena Fleet whenever Raghav's turn names Fleet and asks for one, in "
    "any phrasing: create, spin up, kick off, all valid. Never ask him to reword. "
    "Ordinary coding requests must use start_coding_work instead. Give the exact "
    "task, activity auto/coding/research, provider_mode auto/balanced/codex/claude, "
    "optional worker_count 1-4, and the project name or Git path. Use codex for "
    "Codex-only or no-Claude requests, and claude for the inverse. The broker "
    "resolves one real Git repository and refuses ambiguity.",
    _START_FLEET_SCHEMA,
    annotations=_BROKERED_ACTION,
)
async def start_fleet_run_tool(args):
    return _text(
        await asyncio.to_thread(
            start_fleet,
            str(args.get("task") or ""),
            activity=str(args.get("activity") or "auto"),
            project=str(args.get("project") or ""),
            origin=current_turn(),
            provider_mode=str(args.get("provider_mode") or "auto"),
            worker_count=args.get("worker_count"),
        )
    )


FLEET_TOOLS = (fleet_run_status, control_fleet_run_tool, start_fleet_run_tool)
FLEET_TOOL_NAMES = [f"mcp__serena-fleet__{item.name}" for item in FLEET_TOOLS]


def fleet_tools_server():
    return create_sdk_mcp_server(name="serena-fleet", tools=list(FLEET_TOOLS))
