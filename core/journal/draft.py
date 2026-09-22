"""Turn a day's facts into her draft: a short summary, a timeline, and questions.

The summary is the only part a model writes, and it is checked mechanically:
every capitalised word in it has to appear somewhere in the facts. A summary
that mentions a person or place the facts do not contain is discarded for a
plain template, because a fluent sentence about something that did not happen
is the one thing this journal must never contain.
"""

from __future__ import annotations

import copy
import html
import json
import re
from typing import Any

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
            out.append({
                "id": f"where-{int(visit['arrived_ts'])}", "kind": "where",
                "text": (f"whose place was that, {visit['near']}, {span(visit)}?"
                         if visit.get("near") else f"where were you {span(visit)}?"),
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


def span(visit: dict[str, Any]) -> str:
    """"2:10pm–5:40pm", or open-ended when the visit runs past his day."""

    start = "" if visit.get("started_before") else visit.get("arrived", "")
    end = "" if visit.get("ends_after") else (visit.get("departed") or "")
    if start and end:
        return f"{start}–{end}"
    if end:
        return f"until {end}"
    return f"from {start}" if start else "all day"


def _timeline(facts: dict[str, Any]) -> list[tuple[str, str]]:
    """Where he was, in order -- only once places have names.

    A timeline of "1:59pm · drive, 36.7 km" said nothing he could not already
    see in the Auto-logged panel, and he called it useless until it can say
    where. So a row appears only when it names a place: a visit that has a
    name, or a drive whose ends are known places.
    """

    def key(clock: str) -> str:
        m = re.match(r"(\d+):(\d+)(am|pm)", clock or "")
        if not m:
            return "99:99"
        hour = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
        # His day runs to 5am, so 12:27am comes after 10pm, not before 7am.
        if hour < 5:
            hour += 24
        return f"{hour:02d}:{m.group(2)}"

    rows: list[tuple[str, str]] = []
    for v in facts.get("visits") or []:
        if v.get("place"):
            rows.append(("00:00" if v.get("started_before") else key(v["arrived"]),
                         f"{span(v)} · {v['place']}"))
    for d in facts.get("drives") or []:
        if d.get("from") and d.get("to") and d["from"] != d["to"]:
            rows.append((key(d["start"]), f"{d['start']} · drove {d['from']} → {d['to']}"))
        for stop in d.get("stops") or []:
            where = f" · {stop['place']}" if stop.get("place") else ""
            rows.append((key(stop["at"]), f"{stop['at']} · stopped {stop['minutes']} min{where}"))
    return sorted(rows)


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def template_summary(facts: dict[str, Any]) -> str:
    """The fallback summary: people, named places and work -- never what is auto-logged."""

    parts = []
    people = facts.get("people") or []
    if people:
        parts.append(f"You spent time with {_and([p['name'] for p in people])}.")
    places = [v["place"] for v in facts.get("visits") or [] if v.get("place")]
    if places:
        parts.append(f"You were at {_and(list(dict.fromkeys(places)))}.")
    if facts.get("commits"):
        total = sum(c["count"] for c in facts["commits"])
        repos = _and([c["repo"] for c in facts["commits"][:3]])
        parts.append(f"{total} commit{'s' if total != 1 else ''} in {repos}.")
    return " ".join(parts)


def faithful(summary: str, facts: dict[str, Any]) -> bool:
    """Every capitalised word must come from the facts themselves."""

    haystack = _names_in_facts(facts)
    for word in re.findall(r"\b[A-Z][a-zA-Z'’-]+\b", summary):
        if word in _ALLOWED_CAPITALS:
            continue
        if word.lower() not in haystack:
            return False
    return True


def _ask(prompt: str) -> str:
    from core.journal.model import ask

    return ask(prompt, system="You write short, plain journal summaries.")


def _prompt_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """The facts a summary may draw on: no chat quotes, no dropouts, no coordinates."""

    # Drives, workouts and shows already sit in the entry's Auto-logged panel;
    # he asked for none of it to be written again.
    keep = ("day", "people", "visits", "commits")
    trimmed = copy.deepcopy({k: facts.get(k) for k in keep if facts.get(k)})
    for p in trimmed.get("people") or []:
        p.pop("evidence", None)
    for v in trimmed.get("visits") or []:
        for k in ("lat", "lng", "arrived_ts", "departed_ts", "place_source"):
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
        text = _ask(prompt)
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
    # No byline: it is his journal. What is hers is tracked on the PC instead
    # (see core.journal.locket.write_entry), not announced on the page.
    parts: list[str] = []
    if text:
        parts.append(f"<p>{_e(text)}</p>")
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
