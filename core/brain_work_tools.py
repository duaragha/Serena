"""Serena's own hands for starting coding work on a spoken turn.

Kept out of core/brain_tools.py on purpose: that module is read-only by
construction and its docstring promises so. This one is brokered write
authority, gated by core.work_authority against the real spoken turn.
"""

from __future__ import annotations

import asyncio
import json

from claude_agent_sdk import create_sdk_mcp_server, tool
from mcp.types import ToolAnnotations

from core.brain_laptop_tools import current_turn
from core.coding_job_controls import control_job as _control_job
from core.coding_job_controls import job_status as _job_status
from core.work_authority import start_coding_work as _start_coding_work

_BROKERED_WORK = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,  # same call_id+turn_id will not double-queue
    openWorldHint=False,
)


@tool(
    "start_coding_work",
    "Hand one coding job to your own Sol worker on this machine, for "
    "when Raghav has asked you by voice to build, fix, change, investigate "
    "or continue something. Use your judgement about whether he actually "
    "asked for work: if it is clear, start it and tell him you have; if it "
    "is genuinely ambiguous or you need to know which project, ask him one "
    "short question first instead of calling this. Read the ledger or your "
    "memory first when that would let you brief the worker properly. "
    "'request' must be a complete, self-contained instruction. 'brief_json' "
    "must be a JSON object with project, relevant_conversation, project_context, "
    "memory_guidance, ledger_guidance, handoff_guidance, requested_outcome, "
    "acceptance_criteria, authority_boundaries, likely_files, complexity, and an "
    "explicit boolean "
    "commit_authorized (normally false). Every array must contain plain strings, "
    "including relevant_conversation. "
    "'likely_files' is the file map and it matters more than any other array: "
    "repository-relative paths, and where you know it, the specific function or "
    "element inside them, such as "
    "'core/voice_work_supervisor.py::_emit_job_snapshot builds the drawer "
    "snapshot'. Before you call this, spend the search you already have: "
    "search_memory, search_knowledge then read_knowledge, read_ledger, "
    "recall_chats, and git_latest for the surface he named. You have discussed this codebase "
    "hundreds of times; the worker starts cold and greps blind for minutes "
    "without this. Name your best guess even when you are unsure, and never "
    "state it as fact. A wrong path costs the worker one Read; no path costs it "
    "ninety greps. Leave the array empty only when you truly have nothing. "
    "The broker stats every path you name against the real repository and hands "
    "the worker the result, so a file that is not there arrives marked NOT "
    "FOUND. That makes a wrong guess cheap, not free: search first so the paths "
    "you give are ones that exist. "
    "'complexity' is 'routine', 'normal', or 'hard' and decides how hard the "
    "worker deliberates. Default to 'normal'. Use 'routine' for contained, "
    "low-risk work. Use 'hard' only for work you actually "
    "judge difficult: cross-cutting redesigns, concurrency, protocol or data "
    "migrations, or anything where a wrong approach is expensive. A one-file "
    "fix, a copy change, or a UI tweak is usually 'routine' or 'normal'. "
    "Serena-owned surfaces such as the coding "
    "pane or panel, coding app, Chats app, voice-work display, dot overlay, and "
    "Fleet tab belong to project 'serena' at "
    "/home/raghav/Documents/Projects/serena unless he explicitly names another "
    "project. Use empty arrays only when "
    "that category truly has no applicable context. A capability broker "
    "independently resolves one Git root, freezes the initial tree, and re-reads his actual "
    "spoken turn, so never claim work started unless this returns queued. "
    "Routing searches durable indexed Codex session logs, so the matching chat "
    "does not need to be open. It prefers a safe active exact-project pane, then "
    "resumes the most recent safe exact-project Sol chat by its frozen session id. "
    "He can explicitly ask for a new chat, or explicitly require the current "
    "or existing chat. The returned receipt is the truth about which happened.",
    {"request": str, "brief_json": str},
    annotations=_BROKERED_WORK,
)
async def start_coding_work(args):
    raw_brief = str(args.get("brief_json") or "")
    try:
        brief = json.loads(raw_brief)
    except json.JSONDecodeError:
        brief = None
    if not isinstance(brief, dict):
        text = (
            "NOT QUEUED. NOTHING HAS STARTED. Reason: the structured coding "
            "brief is malformed JSON. Tell Raghav plainly that you could not "
            "start it. Do not say you are on it, working on it, or that it is running."
        )
        return {"content": [{"type": "text", "text": text}]}
    result = await asyncio.to_thread(
        _start_coding_work,
        str(args.get("request") or ""),
        origin=current_turn(),
        brief_context=brief,
    )
    if result.allowed:
        text = (
            f"QUEUED. job {result.item_id[:8]}. {result.reason}. "
            f"Work has started: {result.request}"
        )
    else:
        # Unmissable, because the live failure mode was Serena calling this,
        # getting a refusal, and telling Raghav "started, I'm on it" anyway.
        text = (
            f"NOT QUEUED. NOTHING HAS STARTED. Reason: {result.reason}. "
            "Tell Raghav plainly that you could not start it and why. Do not "
            "say you are on it, working on it, or that it is running."
        )
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "coding_job_status",
    "Read what one of your coding jobs is actually doing: state, project, "
    "attempt, the model and effort it really ran on, changed files, test "
    "commands and exit codes, live proof, evidence gaps, and review verdict. "
    "Read-only. 'reference' may be empty for the one that is live, a short "
    "job id, 'last', or the project name. Use this whenever he asks how it is "
    "going instead of guessing or repeating what you said when you started "
    "it. If it comes back NOT FOUND, say so; never invent progress.",
    {"reference": str},
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def coding_job_status(args):
    result = await asyncio.to_thread(_job_status, str(args.get("reference") or ""))
    if result.ok:
        text = f"JOB {result.item_id[:8]}. {result.spoken}"
    else:
        text = (
            f"NOT FOUND. Reason: {result.reason}. Tell Raghav that plainly, or "
            "ask him the one short question it implies. Do not describe "
            "progress you did not read."
        )
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "control_coding_work",
    "Steer, cancel, or resume a coding job you already started. action is "
    "'steer' to hand the running Codex session a correction and keep the same "
    "session, 'cancel' to stop it, or 'resume' to pick a stopped job back up "
    "in its persisted session. Use steer when he corrects or adds to work "
    "that is already running: it is always better than starting a second job. "
    "'reference' may be empty for the live one, a short job id, 'last', or the "
    "project name; ask him one short question if the broker says it is "
    "ambiguous. 'text' is required for steer and must be the complete "
    "correction in his terms, since the worker cannot hear him. A broker "
    "re-reads his real spoken turn, so never claim a job was stopped, steered "
    "or resumed unless this returns DONE.",
    {"action": str, "reference": str, "text": str},
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
async def control_coding_work(args):
    result = await asyncio.to_thread(
        _control_job,
        str(args.get("action") or ""),
        reference=str(args.get("reference") or ""),
        text=str(args.get("text") or ""),
        origin=current_turn(),
    )
    if result.ok:
        text = f"DONE. {result.reason}. {result.spoken}"
    else:
        text = (
            f"NOT DONE. NOTHING CHANGED. Reason: {result.reason}. Tell Raghav "
            "plainly that it did not happen and why. Do not say you stopped "
            "it, passed it on, or picked it back up."
        )
    return {"content": [{"type": "text", "text": text}]}


WORK_TOOLS = (start_coding_work, coding_job_status, control_coding_work)
WORK_TOOL_NAMES = [f"mcp__serena-work__{item.name}" for item in WORK_TOOLS]


def work_tools_server():
    return create_sdk_mcp_server(name="serena-work", tools=list(WORK_TOOLS))
