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
DRIVE_END_RADIUS_M = 1000.0
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
    from core.journal import location

    start, end = location.day_window(day)
    for visit in location.visits_on(day):
        minutes = visit.minutes()
        if minutes < MIN_VISIT_MINUTES:
            continue
        departed = _clock(visit.departed)
        facts.visits.append({
            "lat": round(visit.lat, 5), "lng": round(visit.lng, 5),
            "arrived": _clock(visit.arrived),
            # "~" marks a departure iOS never sent, read off where he went next.
            "departed": f"~{departed}" if departed and visit.departed_inferred else departed,
            "arrived_ts": visit.arrived, "departed_ts": visit.departed,
            "started_before": visit.arrived < start,
            "ends_after": visit.departed is None or visit.departed > end,
            "minutes": minutes,
            **_name_visit(visit.lat, visit.lng),
        })


def _name_visit(lat: float, lng: float) -> dict[str, str]:
    """A name he gave it, else home/work, else the map; a house gets its street.

    The map lookup is offline (core.journal.osm_places). A house is never
    named from the map -- "a house on X" is a hint for the question, since
    only he knows whose it was.
    """

    from core.journal import osm_places, store

    named = store.place_for(lat, lng)
    if named:
        return {"place": named}
    try:
        found = osm_places.lookup(lat, lng)
    except Exception:
        found = None
    if found and found.get("name"):
        return {"place": found["name"], "place_source": "map"}
    if found and found.get("street"):
        return {"place": "", "near": f"a house on {found['street']}"}
    return {"place": ""}


def _locket(facts: DayFacts, day: date) -> None:
    from core.journal import locket, store

    data = locket.day_facts(day.isoformat())

    def named(point: dict[str, Any] | None) -> str:
        # A drive's ends are its first and last GPS fix, which land wherever
        # he parks -- the drive home from work started ~700m from where the
        # visit was recorded -- so the ends match known places more loosely.
        if not point:
            return ""
        return store.place_for(float(point["lat"]), float(point["lng"]),
                               radius_m=DRIVE_END_RADIUS_M)

    for d in data.get("drives") or []:
        facts.drives.append({
            "start": _clock(d.get("startedAt")), "end": _clock(d.get("endedAt")),
            "minutes": d.get("minutes"), "km": d.get("km"),
            "from": named(d.get("from")), "to": named(d.get("to")),
            "from_point": d.get("from"), "to_point": d.get("to"),
            "stops": _stops(d),
        })
    for w in data.get("workouts") or []:
        facts.workouts.append({
            "name": w.get("name") or "workout", "start": _clock(w.get("startedAt")),
            "minutes": w.get("minutes"), "sets": w.get("sets"), "where": w.get("location") or "",
        })
    for a in data.get("activity") or []:
        facts.activity.append({"kind": a.get("kind"), "summary": a.get("summary") or a.get("label")})


# Idling in the driveway or the parking lot is not a stop he made.
STOP_ENDPOINT_RADIUS_M = 250.0


def _stops(drive: dict[str, Any]) -> list[dict[str, Any]]:
    """Where a drive paused with the engine on -- a pickup, a drop-off.

    Monday's commute was 52 km in two hours for a 27 km trip; the two stops
    inside it were picking Sarim up and dropping him near York.
    """

    from core.journal.store import distance_m

    ends = [p for p in (drive.get("from"), drive.get("to")) if p]
    out = []
    for stop in drive.get("stops") or []:
        lat, lng = float(stop["lat"]), float(stop["lng"])
        if any(distance_m(lat, lng, float(p["lat"]), float(p["lng"])) <= STOP_ENDPOINT_RADIUS_M
               for p in ends):
            continue
        named = _name_visit(lat, lng)
        # A two-minute stop on a main road is not "a house on" it; for a stop
        # the nearest address only says which street the car was on.
        near = named.get("near", "").replace("a house on ", "on ", 1)
        out.append({
            "at": _clock(stop.get("startedAt")), "minutes": stop.get("minutes"),
            "place": named.get("place") or near,
        })
    return out


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


# Commits written by agents carry their name in a trailer ("Co-Authored-By:
# Claude ...") or are Fleet's own dispatch commits. They go out under his git
# identity, so without this every one of them read as "you made 16 commits"
# on a day he made none.
AGENT_MARKERS = ("claude", "anthropic", "codex", "openai", "gpt-", "muse")


def _is_agents(subject: str, trailers: str) -> bool:
    lowered = trailers.lower()
    return any(marker in lowered for marker in AGENT_MARKERS) or subject.lower().startswith("serena:")


def _commits(facts: DayFacts, day: date) -> None:
    from core.journal.location import day_window

    root = _projects_root()
    if root is None:
        raise RuntimeError("no Projects tree with a serena checkout on this machine")
    start, end = day_window(day)
    since = datetime.fromtimestamp(start, TZ).isoformat()
    until = datetime.fromtimestamp(end, TZ).isoformat()
    per_repo: dict[str, list[str]] = {}
    for repo in _repos(root):
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "log", "--all", "--no-merges",
                 f"--since={since}", f"--until={until}",
                 "--format=%ae%x09%s%x09%(trailers:key=Co-Authored-By,valueonly,separator=%x2C)%x1e"],
                capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        subjects = []
        for record in out.split("\x1e"):
            fields = record.strip("\n").split("\t")
            if len(fields) < 2:
                continue
            author, subject = fields[0], fields[1]
            trailers = fields[2] if len(fields) > 2 else ""
            if any(p in author.lower() for p in AUTHOR_PATTERNS) and not _is_agents(subject, trailers):
                subjects.append(subject)
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
