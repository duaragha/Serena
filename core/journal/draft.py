"""Turn a day's facts into her draft: a short summary, a timeline, and questions.

The summary is the only part a model writes, and it is checked mechanically:
every capitalised word in it has to appear somewhere in the facts. A summary
that mentions a person or place the facts do not contain is discarded for a
plain template, because a fluent sentence about something that did not happen
is the one thing this journal must never contain.
"""

from __future__ import annotations

import asyncio
import copy
import html
import json
import re
from typing import Any

MODEL = "sonnet"
MAX_QUESTIONS = 3
# Worth asking about. A 20-minute stop is in the timeline; only a longer one
# is worth interrupting him to name.
ASK_ABOUT_VISIT_MINUTES = 45
_ALLOWED_CAPITALS = {
    "I", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "You", "Your", "Spent", "Went", "Drove", "Worked", "Did", "Had", "Then", "After",
    "Before", "Later", "Most", "The", "A", "An", "In", "On", "At", "With", "Around",
    "About", "Morning", "Afternoon", "Evening", "Night", "Today", "Also", "And", "Got",
    # Dates are facts too: "On September 20th" failed the check on the first
    # live run and threw away a perfectly good summary.
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
}


def _names_in_facts(facts: dict[str, Any]) -> str:
    return json.dumps(facts, ensure_ascii=False).lower()


def questions(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """What only he can tell her, most useful first, at most three."""

    out: list[dict[str, Any]] = []
    for i, text in enumerate(facts.get("people_questions") or []):
        out.append({"id": f"who-{i + 1}", "kind": "who", "text": text})
    for visit in facts.get("visits") or []:
        if not visit.get("place") and (visit.get("minutes") or 0) >= ASK_ABOUT_VISIT_MINUTES:
            span = f"{visit['arrived']}–{visit['departed'] or 'late'}"
            out.append({
                "id": f"where-{int(visit['arrived_ts'])}", "kind": "where",
                "text": f"where were you {span}?",
                "lat": visit["lat"], "lng": visit["lng"],
            })
    people = [p["name"] for p in facts.get("people") or []]
    if people and not facts.get("visits"):
        # No location history (yet) for this day: the one thing chats cannot
        # say is where, so ask it once rather than guess it from a plan.
        names = _and(people)
        out.append({"id": "where-with", "kind": "where-with",
                    "text": f"where did you go with {names}?"})
    return out[:MAX_QUESTIONS]


def _timeline(facts: dict[str, Any]) -> list[tuple[str, str]]:
    """(sort key, line) pairs, times as h:mmam so they sort within a day."""

    def key(clock: str) -> str:
        m = re.match(r"(\d+):(\d+)(am|pm)", clock or "")
        if not m:
            return "99:99"
        hour = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
        return f"{hour:02d}:{m.group(2)}"

    rows: list[tuple[str, str]] = []
    for v in facts.get("visits") or []:
        where = v.get("place") or "somewhere unnamed"
        rows.append((key(v["arrived"]), f"{v['arrived']}–{v['departed'] or 'late'} · {where}"))
    for d in facts.get("drives") or []:
        route = " → ".join(x for x in (d.get("from"), d.get("to")) if x) or "drive"
        rows.append((key(d["start"]), f"{d['start']} · {route}, {d.get('km')} km, {d.get('minutes')} min"))
    for w in facts.get("workouts") or []:
        rows.append((key(w["start"]), f"{w['start']} · {w['name']}, {w.get('sets')} sets"))
    return sorted(rows)


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def template_summary(facts: dict[str, Any]) -> str:
    parts = []
    people = facts.get("people") or []
    if people:
        parts.append(f"You spent time with {_and([p['name'] for p in people])}.")
    if facts.get("workouts"):
        parts.append(f"Worked out ({facts['workouts'][0]['name']}).")
    if facts.get("drives"):
        km = sum(float(d.get("km") or 0) for d in facts["drives"])
        parts.append(f"Drove {len(facts['drives'])} time{'s' if len(facts['drives']) > 1 else ''}, {km:.0f} km in all.")
    if facts.get("commits"):
        total = sum(c["count"] for c in facts["commits"])
        repos = _and([c["repo"] for c in facts["commits"][:3]])
        parts.append(f"{total} commit{'s' if total != 1 else ''} in {repos}.")
    return " ".join(parts) or "Nothing was recorded for this day."


def faithful(summary: str, facts: dict[str, Any]) -> bool:
    """Every capitalised word must come from the facts themselves."""

    haystack = _names_in_facts(facts)
    for word in re.findall(r"\b[A-Z][a-zA-Z'’-]+\b", summary):
        if word in _ALLOWED_CAPITALS:
            continue
        if word.lower() not in haystack:
            return False
    return True


async def _ask(prompt: str) -> str:
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    options = ClaudeAgentOptions(model=MODEL, tools=[], allowed_tools=[], setting_sources=[],
                                 max_turns=1, system_prompt="You write short, plain journal summaries.")
    chunks: list[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            chunks.extend(b.text for b in message.content if isinstance(b, TextBlock))
    return "".join(chunks).strip()


def _prompt_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """The facts a summary may draw on: no chat quotes, no dropouts, no coordinates."""

    keep = ("day", "people", "visits", "drives", "workouts", "activity", "commits")
    trimmed = copy.deepcopy({k: facts.get(k) for k in keep if facts.get(k)})
    for p in trimmed.get("people") or []:
        p.pop("evidence", None)
    for v in trimmed.get("visits") or []:
        for k in ("lat", "lng", "arrived_ts", "departed_ts"):
            v.pop(k, None)
    for d in trimmed.get("drives") or []:
        d.pop("from_point", None)
        d.pop("to_point", None)
    # Counts and project names only. Given the subjects, the model paraphrased
    # them into the summary despite being told not to.
    for c in trimmed.get("commits") or []:
        c.pop("subjects", None)
    return trimmed


def summary(facts: dict[str, Any], answers: list[dict[str, Any]] | None = None) -> str:
    trimmed = _prompt_facts(facts)
    said = [a.get("answer") for a in answers or [] if a.get("answer")]
    prompt = (
        "Write 2-3 short sentences summarising Raghav's day for his journal, in second "
        "person (\"you\"), plain and factual. Use ONLY what is in FACTS and in HIS ANSWERS. "
        "Do not add feelings, reasons, food, or any person or place that is not written "
        "there. Do not mention commits by message; a count and project names are enough. "
        "If something is marked low confidence, say \"probably\". No markdown.\n\n"
        f"FACTS: {json.dumps(trimmed, ensure_ascii=False)}\n\nHIS ANSWERS: {json.dumps(said, ensure_ascii=False)}"
    )
    try:
        text = asyncio.run(_ask(prompt))
    except Exception:
        return template_summary(facts)
    # Checked against the same trimmed facts the model saw. The chat evidence
    # is left out of both on purpose: a quoted plan ("Farooj at 2?") would
    # otherwise make a place that was never a place look like a fact.
    check = dict(trimmed)
    check["answers"] = said
    return text if text and faithful(text, check) else template_summary(facts)


def _e(text: Any) -> str:
    return html.escape(str(text or ""), quote=False)


def title(day: str) -> str:
    from datetime import date

    d = date.fromisoformat(day)
    return f"{d.strftime('%A, %B')} {d.day}"


def render_html(day: str, facts: dict[str, Any], text: str,
                answers: list[dict[str, Any]], questions_: list[dict[str, Any]]) -> str:
    parts = [
        "<p><em>Drafted by Serena from your chats, drives and location. "
        "Anything under “In your words” is exactly what you said.</em></p>",
        f"<p>{_e(text)}</p>",
    ]
    people = facts.get("people") or []
    if people:
        items = "".join(
            f"<li>{_e(p['name'])}{' (probably)' if p.get('confidence') in ('medium', 'low') else ''}"
            f"{' · ' + _e(p['when']) if p.get('when') else ''}</li>" for p in people)
        parts.append(f"<h3>People</h3><ul>{items}</ul>")
    timeline = _timeline(facts)
    if timeline:
        parts.append("<h3>Timeline</h3><ul>" + "".join(f"<li>{_e(line)}</li>" for _, line in timeline) + "</ul>")
    if facts.get("commits"):
        items = "".join(f"<li>{_e(c['repo'])}: {c['count']} commit{'s' if c['count'] != 1 else ''}</li>"
                        for c in facts["commits"])
        parts.append(f"<h3>Work</h3><ul>{items}</ul>")
    if facts.get("activity"):
        items = "".join(f"<li>{_e(a['summary'])}</li>" for a in facts["activity"] if a.get("summary"))
        if items:
            parts.append(f"<h3>Also</h3><ul>{items}</ul>")
    if answers:
        items = "".join(f"<li>{_e(a['answer'])}</li>" for a in answers if a.get("answer"))
        parts.append(f"<h3>In your words</h3><ul>{items}</ul>")
    answered = {a.get("question_id") for a in answers}
    open_q = [q for q in questions_ if q["id"] not in answered]
    if open_q:
        parts.append("<h3>Still to fill in</h3><ul>" +
                     "".join(f"<li>{_e(q['text'])}</li>" for q in open_q) + "</ul>")
    missing = facts.get("unavailable") or {}
    if missing:
        parts.append("<p><em>Couldn’t read: " + _e(", ".join(sorted(missing))) +
                     ". Anything from there is missing, not absent.</em></p>")
    return "".join(parts)


def telegram_text(day: str, text: str, questions_: list[dict[str, Any]]) -> str:
    lines = [f"your journal for {title(day).lower()}:", "", text]
    if questions_:
        lines += ["", "only you can fill these in:"]
        lines += [f"- {q['text']}" for q in questions_]
        lines += ["", "just reply in your own words, or ignore it and the facts stay as they are."]
    return "\n".join(lines)
