"""Her hands on his journal: read a day's draft, and write down his answers.

He answers her journal questions two ways -- a reply to her text, or out loud
on the call she places -- and both reach her through the brain, so both land
here. The tools write only what he said, in his words; the one thing they
derive is a first name or a short place name he just gave, which is exactly
what he asked her to use her judgement on.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from mcp.types import ToolAnnotations

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                             idempotentHint=True, openWorldHint=False)
_WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                          idempotentHint=False, openWorldHint=True)


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _failed(text: str) -> dict[str, Any]:
    # Flagged, so a failed save is never read back to him as "got it".
    return {"content": [{"type": "text", "text": text}], "is_error": True}


@tool("journal_day",
      "His journal draft for a day (YYYY-MM-DD; empty for the most recent day with "
      "open questions): the summary, the people, the questions still unanswered, and "
      "what he has already said.",
      {"day": str}, annotations=_READ_ONLY)
async def journal_day(args):
    from core.journal import remote

    result = await asyncio.to_thread(remote.call, "day", {"day": str((args or {}).get("day") or "").strip()})
    if "error" in result:
        return _failed(result["error"])
    return _ok(json.dumps(result, indent=2))


@tool("journal_answer",
      "Put what he said into his journal, in his words. Use it for answers to your "
      "journal questions (pass `question_id` from journal_day) and whenever he asks you "
      "to add something to a day's journal (no `question_id`; `day` is YYYY-MM-DD, "
      "today if he does not say). For a `where` question, also pass `place_name`: just "
      "the place, short (\"MOTW Cafe\"), so the spot is remembered next time. `people` "
      "is first names he says he was with. If the result has `tell_him`, say that to him "
      "instead of telling him it is in his journal: it means the Locket entry was not changed.",
      {"day": str, "answer": str, "question_id": str, "place_name": str, "people": list},
      annotations=_WRITES)
async def journal_answer(args):
    from core.journal import remote

    args = args or {}
    people = args.get("people") or []
    if not isinstance(people, list):
        people = [str(people)]
    result = await asyncio.to_thread(remote.call, "answer", {
        "day": str(args.get("day") or "").strip(),
        "answer": str(args.get("answer") or ""),
        "question_id": str(args.get("question_id") or ""),
        "place_name": str(args.get("place_name") or ""),
        "people": [str(p) for p in people],
    })
    if "error" in result:
        return _failed(result["error"])
    return _ok(json.dumps(result))


JOURNAL_TOOLS = (journal_day, journal_answer)
JOURNAL_TOOL_NAMES = [f"mcp__serena-journal__{t.name}" for t in JOURNAL_TOOLS]


def journal_tools_server():
    return create_sdk_mcp_server(name="serena-journal", tools=list(JOURNAL_TOOLS))
