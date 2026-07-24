"""Serena's own hands for starting coding work on a spoken turn.

Kept out of core/brain_tools.py on purpose: that module is read-only by
construction and its docstring promises so. This one is brokered write
authority, gated by core.work_authority against the real spoken turn.
"""

from __future__ import annotations

import asyncio

from claude_agent_sdk import create_sdk_mcp_server, tool
from mcp.types import ToolAnnotations

from core.brain_laptop_tools import _CURRENT_TURN
from core.work_authority import start_coding_work as _start_coding_work

_BROKERED_WORK = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,  # same call_id+turn_id will not double-queue
    openWorldHint=False,
)


@tool(
    "start_coding_work",
    "Hand one coding job to your own private worker on this machine, for "
    "when Raghav has asked you by voice to build, fix, change, investigate "
    "or continue something. Use your judgement about whether he actually "
    "asked for work: if it is clear, start it and tell him you have; if it "
    "is genuinely ambiguous or you need to know which project, ask him one "
    "short question first instead of calling this. Read the ledger or your "
    "memory first when that would let you brief the worker properly. "
    "'request' must be a complete, self-contained instruction including the "
    "project or repo when you know it, because the worker cannot hear the "
    "conversation. A capability broker independently re-reads his actual "
    "spoken turn, so never claim work started unless this returns queued.",
    {"request": str},
    annotations=_BROKERED_WORK,
)
async def start_coding_work(args):
    result = await asyncio.to_thread(
        _start_coding_work,
        str(args.get("request") or ""),
        origin=_CURRENT_TURN.get() or {},
    )
    if result.allowed:
        text = f"queued (job {result.item_id[:8]}): {result.request}"
    else:
        text = f"not queued, {result.reason}"
    return {"content": [{"type": "text", "text": text}]}


WORK_TOOLS = (start_coding_work,)
WORK_TOOL_NAMES = [f"mcp__serena-work__{item.name}" for item in WORK_TOOLS]


def work_tools_server():
    return create_sdk_mcp_server(name="serena-work", tools=list(WORK_TOOLS))
