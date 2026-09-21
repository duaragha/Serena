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
    from core.journal import store

    day = str((args or {}).get("day") or "").strip()
    if day:
        record = store.load_day(day)
        if record is None:
            return _failed(f"there is no journal draft for {day}")
    else:
        open_days = store.open_days(limit=1)
        if not open_days:
            return _ok("no journal day is waiting on him")
        record = open_days[0]
    facts = record["facts"]
    return _ok(json.dumps({
        "day": record["day"],
        "summary": record["summary"],
        "people": [p["name"] for p in facts.get("people") or []],
        "open_questions": [{"id": q["id"], "kind": q["kind"], "text": q["text"]}
                           for q in store.unanswered(record)],
        "his_answers": [a["answer"] for a in record["answers"]],
        "unavailable_sources": sorted((facts.get("unavailable") or {}).keys()),
    }, indent=2))


@tool("journal_answer",
      "Record what he said in answer to one of your journal questions. `answer` is "
      "his words, kept as he said them. `question_id` is the question it answers "
      "(from journal_day). For a `where` question, also pass `place_name`: just the "
      "place, short (\"MOTW Cafe\"), so the spot is remembered next time. `people` "
      "is first names he says he was with.",
      {"day": str, "answer": str, "question_id": str, "place_name": str, "people": list},
      annotations=_WRITES)
async def journal_answer(args):
    from core.journal import nightly

    args = args or {}
    day = str(args.get("day") or "").strip()
    if not day:
        from core.journal import store

        waiting = store.open_days(limit=1)
        if not waiting:
            return _failed("no journal day has open questions; pass `day`")
        day = waiting[0]["day"]
    people = args.get("people") or []
    if not isinstance(people, list):
        people = [str(people)]
    try:
        result = await asyncio.to_thread(
            nightly.record_answer, day, str(args.get("answer") or ""),
            question_id=str(args.get("question_id") or ""),
            place_name=str(args.get("place_name") or ""),
            people=[str(p) for p in people])
    except Exception as exc:
        return _failed(f"his answer was NOT saved: {type(exc).__name__}: {exc}")
    return _ok(json.dumps(result))


JOURNAL_TOOLS = (journal_day, journal_answer)
JOURNAL_TOOL_NAMES = [f"mcp__serena-journal__{t.name}" for t in JOURNAL_TOOLS]


def journal_tools_server():
    return create_sdk_mcp_server(name="serena-journal", tools=list(JOURNAL_TOOLS))
