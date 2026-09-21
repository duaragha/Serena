"""Everything recorded about one day, gathered from his own data.

No model writes anything here. Each source is queried, and each one that
cannot be read is recorded as unavailable -- never as empty -- so the draft can
say "Locket was down" instead of implying he did nothing all day.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Toronto")
# A visit shorter than this is a stop at a light, not somewhere he went.
MIN_VISIT_MINUTES = 20
AUTHOR_PATTERNS = ("duaragha", "raghav")
GIT_TIMEOUT_SECONDS = 15


@dataclass
class DayFacts:
    day: str
    people: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    people_questions: list[str] = field(default_factory=list)
    visits: list[dict[str, Any]] = field(default_factory=list)
    drives: list[dict[str, Any]] = field(default_factory=list)
    workouts: list[dict[str, Any]] = field(default_factory=list)
    activity: list[dict[str, Any]] = field(default_factory=list)
    commits: list[dict[str, Any]] = field(default_factory=list)
    unavailable: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def empty(self) -> bool:
        return not any((self.people, self.visits, self.drives, self.workouts,
                        self.activity, self.commits))


def _clock(value: float | str | None) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        moment = datetime.fromtimestamp(float(value), TZ)
    local = moment.astimezone(TZ)
    # Built by hand: "%-I" is glibc-only and this runs on the Windows PC.
    return f"{local.hour % 12 or 12}:{local.minute:02d}{'am' if local.hour < 12 else 'pm'}"


def _people(facts: DayFacts, day: date) -> None:
    from core.journal.people import who_i_met

    result = who_i_met(day)
    facts.people = [{"name": p.name, "confidence": p.confidence, "when": p.when,
                     "evidence": [asdict(e) for e in p.evidence]} for p in result.people]
    facts.excluded = [asdict(x) for x in result.excluded]
    facts.people_questions = result.questions


def _visits(facts: DayFacts, day: date) -> None:
    from core.journal import location, store

    for visit in location.visits_on(day):
        minutes = visit.minutes()
        if minutes < MIN_VISIT_MINUTES:
            continue
        facts.visits.append({
            "lat": round(visit.lat, 5), "lng": round(visit.lng, 5),
            "arrived": _clock(visit.arrived), "departed": _clock(visit.departed),
            "arrived_ts": visit.arrived, "departed_ts": visit.departed,
            "minutes": minutes,
            "place": store.place_for(visit.lat, visit.lng),
        })


def _locket(facts: DayFacts, day: date) -> None:
    from core.journal import locket, store

    data = locket.day_facts(day.isoformat())

    def named(point: dict[str, Any] | None) -> str:
        if not point:
            return ""
        return store.place_for(float(point["lat"]), float(point["lng"]))

    for d in data.get("drives") or []:
        facts.drives.append({
            "start": _clock(d.get("startedAt")), "end": _clock(d.get("endedAt")),
            "minutes": d.get("minutes"), "km": d.get("km"),
            "from": named(d.get("from")), "to": named(d.get("to")),
            "from_point": d.get("from"), "to_point": d.get("to"),
        })
    for w in data.get("workouts") or []:
        facts.workouts.append({
            "name": w.get("name") or "workout", "start": _clock(w.get("startedAt")),
            "minutes": w.get("minutes"), "sets": w.get("sets"), "where": w.get("location") or "",
        })
    for a in data.get("activity") or []:
        facts.activity.append({"kind": a.get("kind"), "summary": a.get("summary") or a.get("label")})


def _repos(root: Path) -> list[Path]:
    repos = []
    for depth in (1, 2):
        for candidate in root.glob("/".join(["*"] * depth)):
            if (candidate / ".git").exists():
                repos.append(candidate)
    return repos


def _projects_root() -> Path | None:
    """The synced Projects tree, whatever checkout this code is running from.

    Not machine_context.projects_root(): that answers "the directory above this
    repo", which is right for a checkout inside Projects and wrong for the PC's
    runtime checkout at C:/Users/ragha/serena-runtime, where it names the whole
    home directory. The Projects tree is the one that holds the serena checkout.
    """

    override = os.environ.get("SERENA_PROJECTS_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    home = Path.home()
    for candidate in (home / "Projects", home / "Documents" / "Projects"):
        if (candidate / "serena").is_dir():
            return candidate
    return None


def _commits(facts: DayFacts, day: date) -> None:
    root = _projects_root()
    if root is None:
        raise RuntimeError("no Projects tree with a serena checkout on this machine")
    start = datetime.combine(day, datetime.min.time(), TZ)
    since, until = start.isoformat(), start.replace(hour=23, minute=59, second=59).isoformat()
    per_repo: dict[str, list[str]] = {}
    for repo in _repos(root):
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "log", "--all", "--no-merges",
                 f"--since={since}", f"--until={until}", "--format=%ae%x09%s"],
                capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        subjects = [line.split("\t", 1)[1] for line in out.splitlines()
                    if "\t" in line and any(p in line.split("\t", 1)[0].lower() for p in AUTHOR_PATTERNS)]
        if subjects:
            per_repo[repo.name] = subjects
    facts.commits = [{"repo": name, "count": len(subjects), "subjects": subjects[:5]}
                     for name, subjects in sorted(per_repo.items(), key=lambda kv: -len(kv[1]))]


SOURCES = (("chats", _people), ("location", _visits), ("locket", _locket), ("commits", _commits))


def gather(day: date) -> DayFacts:
    facts = DayFacts(day=day.isoformat())
    for name, collect in SOURCES:
        try:
            collect(facts, day)
        except Exception as exc:  # one source down must not blank the day
            facts.unavailable[name] = f"{type(exc).__name__}: {exc}"[:300]
    return facts
