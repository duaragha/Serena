"""The nightly loop: draft the day, send it to him, take his answers.

At ~10pm she gathers the day, writes her entry into Locket straight away (so
no day is ever blank, even if he never replies), texts him the summary and the
one to three things only he can answer, and -- when there are questions --
rings him too. Both, because he asked for both.

His answers come back through her brain on either channel: a reply on
Telegram and a spoken answer on a call both reach the same `journal_answer`
tool, which is `record_answer` below.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from core.journal import draft, locket, store
from core.journal import facts as facts_mod

TZ = ZoneInfo("America/Toronto")
NIGHTLY_HOUR = 22  # 10pm, his time


class JournalError(RuntimeError):
    pass


def today() -> date:
    return datetime.now(TZ).date()


def build(day: date) -> dict[str, Any]:
    """Gather, draft and write the entry. Safe to rerun; it updates in place."""

    key = day.isoformat()
    existing = store.load_day(key) or {}
    gathered = facts_mod.gather(day).to_dict()
    answers = existing.get("answers") or []
    asked = draft.questions(gathered)
    # Questions he already answered stay answered even if a rerun rephrases them.
    text = draft.summary(gathered, answers)
    html = draft.render_html(key, gathered, text, answers, asked)
    written = locket.write_entry(day=key, title=draft.title(key), html=html,
                                 entry_id=existing.get("entry_id"),
                                 base=existing.get("entry_base"),
                                 written=existing.get("entry_written"))
    store.save_day(key, facts=gathered, questions=asked, summary=text,
                   entry_id=written["id"], entry_base=written["base"],
                   entry_written=written["written"], drafted_at=time.time())
    return store.load_day(key) or {}


def send(day: date, *, call: bool = True) -> dict[str, Any]:
    """Text him the draft; ring him too when there is something to ask."""

    from core import phone_line

    key = day.isoformat()
    record = store.load_day(key)
    if record is None:
        raise JournalError(f"no draft for {key}; build it first")
    open_questions = store.unanswered(record)
    sent = phone_line.send(draft.telegram_text(key, record["summary"], open_questions),
                           key=f"journal:{key}")
    result = {"texted": bool(sent), "called": False}
    if sent:
        store.save_day(key, sent_at=time.time())
    if call and open_questions:
        from core import phone_call

        try:
            if phone_call.enabled():
                phone_call.place(
                    f"hey, it's me. i've got {len(open_questions)} quick "
                    f"question{'s' if len(open_questions) > 1 else ''} about your day "
                    f"for your journal. {open_questions[0]['text']}",
                    key=f"journal:{key}")
                store.save_day(key, called_at=time.time())
                result["called"] = True
        except Exception as exc:  # the text already went out; a failed call is not fatal
            result["call_error"] = f"{type(exc).__name__}: {exc}"
    return result


def run_nightly(day: date | None = None) -> dict[str, Any]:
    target = day or today()
    record = build(target)
    outcome = send(target)
    return {"day": target.isoformat(), "entry_id": record.get("entry_id"),
            "questions": len(record.get("questions") or []), **outcome}


def record_answer(day: str, answer: str, *, question_id: str = "",
                  place_name: str = "", people: list[str] | None = None) -> dict[str, Any]:
    """Keep what he said, in his words, and fold it into the entry.

    `place_name` names the place for a "where were you" question, so the next
    visit to the same spot resolves on its own. `people` adds first names he
    says he was with; they go in as certain, because he said so.
    """

    record = store.load_day(day)
    if record is None:
        raise JournalError(f"there is no journal draft for {day}")
    text = " ".join(str(answer or "").split())
    if not text:
        raise JournalError("an answer needs some words in it")
    question = next((q for q in record["questions"] if q["id"] == question_id), None)
    if question_id and question is None:
        raise JournalError(f"{day} has no question {question_id!r}")

    answers = list(record["answers"])
    answers.append({"question_id": question_id or "", "answer": text, "at": time.time()})

    facts = record["facts"]
    if place_name and question and question.get("kind") == "where" and "lat" in question:
        store.name_place(place_name, float(question["lat"]), float(question["lng"]))
        for visit in facts.get("visits") or []:
            if f"where-{int(visit['arrived_ts'])}" == question_id:
                visit["place"] = " ".join(place_name.split())
    known = {p["name"].lower() for p in facts.get("people") or []}
    for name in people or []:
        first = str(name).strip().split(" ")[0].capitalize()
        if first and first.lower() not in known:
            facts.setdefault("people", []).append({"name": first, "confidence": "high",
                                                   "when": "", "evidence": [], "source": "you"})
            known.add(first.lower())
        elif first:
            for p in facts["people"]:
                if p["name"].lower() == first.lower():
                    p["confidence"] = "high"

    text_summary = draft.summary(facts, answers)
    html = draft.render_html(day, facts, text_summary, answers, record["questions"])
    written = locket.write_entry(day=day, title=draft.title(day), html=html,
                                 entry_id=record.get("entry_id"),
                                 base=record.get("entry_base"),
                                 written=record.get("entry_written"))
    entry_id = written["id"]
    store.save_day(day, facts=facts, answers=answers, summary=text_summary, entry_id=entry_id,
                   entry_base=written["base"], entry_written=written["written"])
    remaining = store.unanswered(store.load_day(day) or {})
    return {"day": day, "saved": True, "entry_id": entry_id,
            "still_open": [q["text"] for q in remaining]}
