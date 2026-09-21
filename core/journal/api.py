"""The journal's two operations, as plain functions any caller can run.

Her brain tools call these; so does the PC when another machine asks it to
(see core.journal.remote). Keeping them free of the tool layer is what lets
the laptop's orb reach the one journal that lives on the PC.
"""

from __future__ import annotations

from typing import Any


def day(day: str = "") -> dict[str, Any]:
    from core.journal import store

    if day:
        record = store.load_day(day)
        if record is None:
            return {"error": f"there is no journal draft for {day}"}
    else:
        waiting = store.open_days(limit=1)
        if not waiting:
            return {"note": "no journal day is waiting on him"}
        record = waiting[0]
    facts = record["facts"]
    return {
        "day": record["day"],
        "summary": record["summary"],
        "people": [p["name"] for p in facts.get("people") or []],
        "open_questions": [{"id": q["id"], "kind": q["kind"], "text": q["text"]}
                           for q in store.unanswered(record)],
        "his_answers": [a["answer"] for a in record["answers"]],
        "unavailable_sources": sorted((facts.get("unavailable") or {}).keys()),
    }


def answer(day: str = "", answer: str = "", question_id: str = "",
           place_name: str = "", people: list[str] | None = None) -> dict[str, Any]:
    from core.journal import nightly, store

    if not day:
        waiting = store.open_days(limit=1)
        # An answer goes to the day that asked; anything else is about today.
        day = waiting[0]["day"] if waiting and question_id else nightly.today().isoformat()
    try:
        return nightly.record_answer(day, answer, question_id=question_id,
                                     place_name=place_name, people=list(people or []))
    except Exception as exc:
        return {"error": f"his words were NOT saved: {type(exc).__name__}: {exc}"}
