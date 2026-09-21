"""Who he actually spent the day with, read off the plans in his chats.

Plans get made in chats -- a group chat, a DM, whatever network the friend is
on -- and Unified already has all of them. So this reads the planning, not the
hangout: "pull up at 7", "where we linking", "I'm here now", and afterwards
"just got home". Tested against three real days before it was built, and it
named the right people all three times.

The two ways it got things wrong in those tests are the two rules here:

- It wrote "Farooj on O'Connor" as the place. Farooj was not a place. Chats
  are evidence for WHO and roughly WHEN, never for WHERE; places come from
  location data or from him.
- It wrote "Rakial" as a person. That was him saying "Rachael" funny. A name
  is taken from how the chat itself labels someone whenever possible, and a
  name that only appears as a nickname inside a sentence is a question, not a
  fact.

And one guard against the model itself: every person must come with evidence
quoted from the transcript, and a quote that is not actually in the chat it
claims to come from gets that person dropped. A plausible invented quote is the
exact failure mode of a model summarising private messages, and it is cheap to
catch mechanically.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from core.journal.unified_source import ChatMessage, messages_between

TZ = ZoneInfo("America/Toronto")
# Plans for a day are made the evening before; confirmations ("got home safe")
# land after midnight. Measured on the three test days: the earliest useful
# message was 6:21pm the day before, the latest 2:56am the day after.
WINDOW_BEFORE = timedelta(hours=6)   # from 6pm the evening before
WINDOW_AFTER = timedelta(hours=28)   # to 4am the morning after
MODEL = "sonnet"
MAX_MESSAGE_CHARS = 220
MAX_TRANSCRIPT_CHARS = 180_000
# Her own line, his notes-to-self, and bots are not people he met.
NOISE_CHATS = re.compile(
    r"^(serena|me|me \(imessage\)|note to self|botfather|telegram|set)$|^\d{3,6}\b",
    re.IGNORECASE)


@dataclass
class Evidence:
    chat: str
    time: str
    quote: str


@dataclass
class Person:
    name: str
    confidence: str  # high | medium | low
    when: str
    evidence: list[Evidence] = field(default_factory=list)
    note: str = ""


@dataclass
class Excluded:
    name: str
    reason: str


@dataclass
class PeopleResult:
    day: str
    people: list[Person]
    excluded: list[Excluded]
    questions: list[str]
    dropped_unverified: list[str]
    message_count: int

    def to_dict(self) -> dict:
        return asdict(self)


def window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0, 0), TZ) - WINDOW_BEFORE
    return start, start + WINDOW_BEFORE + WINDOW_AFTER


def transcript(messages: list[ChatMessage]) -> str:
    """Group by chat so a plan reads as one conversation, not interleaved noise."""

    by_chat: dict[str, list[ChatMessage]] = {}
    for message in messages:
        if NOISE_CHATS.search(message.chat.strip()):
            continue
        by_chat.setdefault(message.chat, []).append(message)
    blocks = []
    for chat, rows in sorted(by_chat.items(), key=lambda item: item[1][0].at):
        lines = []
        for m in rows:
            text = m.text if len(m.text) <= MAX_MESSAGE_CHARS else m.text[:MAX_MESSAGE_CHARS] + "…"
            lines.append(m.line(TZ).split(" | ", 1)[0] + " | " +
                         ("ME" if m.outgoing else m.author) + ": " + text)
        blocks.append(f"### CHAT: {chat}\n" + "\n".join(lines))
    text = "\n\n".join(blocks)
    return text[:MAX_TRANSCRIPT_CHARS]


def _prompt(day: date, body: str) -> str:
    # Built by hand: "%-d" is glibc-only and this runs on the Windows PC.
    weekday = f"{day.strftime('%A %B')} {day.day}, {day.year}"
    return f"""You are reading Raghav's own chat messages to work out who he physically
spent time with in person on {weekday}. The transcript runs from the evening
before to early the morning after, because plans are made the night before
and "got home" messages land after midnight. "ME" is Raghav.

Find in-person meetups on {weekday} only, from the planning and confirmation
messages: arranging a time or place, "omw", "here now", "where we linking",
picking someone up, "got home safe", and so on.

Rules -- follow them exactly:
- Use FIRST NAMES only. Take the name from how the chat labels the person
  (e.g. author "saad ahmed" -> "Saad", "Sohaib Sher" -> "Sohaib", "Rana (WA)"
  -> "Rana"). If someone is only mentioned by a nickname or a misspelling
  inside a message and you cannot match them to a labelled author, do NOT
  list them as a person; put a short question about them in "questions".
- Never report places. Places in chat are slang, jokes or changed plans.
- Someone who was invited but dropped out, cancelled, or was busy goes in
  "excluded", not in "people". The reason is at most six neutral words
  ("dropped out, had work", "said no") -- never why, never their feelings,
  never anything about other people.
- Meetups on other days (the day before, the day after) do not count.
- Calls, video chats and texting are not meeting in person.
- Family he lives with does not count as "hanging out" unless they clearly
  went out together somewhere.
- confidence: "high" when both a plan and an arrival/after confirmation
  exist; "medium" when the plan was confirmed but nothing shows it happened;
  "low" when it is only suggested.
- Every person needs 1-3 evidence items, each quoting a message EXACTLY as it
  appears (a short verbatim substring, under 80 characters), with the chat
  name exactly as in the "### CHAT:" header and the time as shown.
- Do not describe, summarise or quote anything personal beyond the planning
  messages you cite as evidence.
- "questions" is only for possible companions you could not identify, e.g.
  "who is rakial?". At most two. No questions about logistics, family, or
  anything that is not "who was this person".

Reply with ONLY this JSON, no prose:
{{"people": [{{"name": "", "confidence": "high|medium|low", "when": "e.g. ~2:30pm-evening",
  "evidence": [{{"chat": "", "time": "", "quote": ""}}], "note": ""}}],
 "excluded": [{{"name": "", "reason": ""}}],
 "questions": [""]}}

If he met nobody, return empty lists.

TRANSCRIPT:
{body}
"""


def _ask(prompt: str) -> str:
    from core.journal.model import ask

    return ask(prompt, system="You extract facts from chat logs and reply with strict JSON only.")


def _json_block(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("model reply had no JSON object")
    return json.loads(match.group(0))


def _normal(text: str) -> str:
    return " ".join(re.sub(r"[‘’]", "'", text).lower().split())


def verify(people: list[Person], messages: list[ChatMessage]) -> tuple[list[Person], list[str]]:
    """Keep a person only if at least one quote is really in the cited chat."""

    by_chat: dict[str, list[str]] = {}
    for m in messages:
        by_chat.setdefault(_normal(m.chat), []).append(_normal(m.text))
    kept, dropped = [], []
    for person in people:
        real = []
        for item in person.evidence:
            quote = _normal(item.quote).rstrip("…").strip()
            texts = by_chat.get(_normal(item.chat), [])
            if quote and any(quote in text for text in texts):
                real.append(item)
        if real:
            person.evidence = real
            kept.append(person)
        else:
            dropped.append(person.name)
    return kept, dropped


def who_i_met(day: date, *, messages: list[ChatMessage] | None = None) -> PeopleResult:
    start, end = window(day)
    rows = messages if messages is not None else messages_between(start, end)
    body = transcript(rows)
    if not body.strip():
        return PeopleResult(day.isoformat(), [], [], [], [], len(rows))
    data = _json_block(_ask(_prompt(day, body)))
    people = []
    for raw in data.get("people") or []:
        name = str(raw.get("name") or "").strip().split(" ")[0].capitalize()
        if not name:
            continue
        people.append(Person(
            name=name,
            confidence=str(raw.get("confidence") or "low").lower(),
            when=str(raw.get("when") or ""),
            evidence=[Evidence(str(e.get("chat") or ""), str(e.get("time") or ""),
                               str(e.get("quote") or ""))
                      for e in (raw.get("evidence") or []) if isinstance(e, dict)],
            note=str(raw.get("note") or ""),
        ))
    people, dropped = verify(people, rows)
    excluded = [Excluded(str(x.get("name") or "").split(" ")[0].capitalize(), str(x.get("reason") or ""))
                for x in (data.get("excluded") or []) if isinstance(x, dict) and x.get("name")]
    questions = [str(q) for q in (data.get("questions") or []) if str(q).strip()]
    return PeopleResult(day.isoformat(), people, excluded, questions, dropped, len(rows))
