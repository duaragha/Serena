"""Validated model routing and four-phase policy for Serena Fleet.

The policy is deliberately deterministic.  Choosing which models work on a
task must not itself consume another model turn, and a configured model is
never silently replaced with a cheaper fallback.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fleet.contracts import build_work_unit_contracts, validate_work_unit_contracts
from fleet.read_mcp import DEFAULT_READ_SERVERS

SCHEMA_VERSION = 1
PHASES = ("discover", "execute", "verify", "finalize")
ACTIVITIES = frozenset({"auto", "coding", "research"})
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
PROVIDERS = frozenset({"codex", "claude"})
REQUESTED_PROVIDER_MODES = frozenset({"auto", "balanced", "mixed", "codex", "claude"})
SELECTED_PROVIDER_MODES = frozenset({"balanced", "codex", "claude", "adaptive"})
SESSION_MODES = frozenset({"per_leg", "persistent_by_worker"})
CAPACITY_HANDOFF_MODES = frozenset({"automatic", "manual"})
MAX_WORKERS = 4
MAX_EXPLICIT_WORKSTREAMS = 16
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "serena" / "fleet.json"
REPOSITORY_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "fleet.example.json"

# This is a hard Fleet contract, not a soft preference. One model per phase,
# every agent in that phase runs it, and no worker may change it. Effort is
# named per model because the curves differ in shape.
#
# The scores below are DeepSWE v1.1 (a coding benchmark), so they justify Code,
# Fix and Review. They say nothing about Research; the Luna choice there rests
# on a direct measurement recorded in the discover comment.
#
#   claude-opus-5 high    73% +/-2   $6.08    73 turns
#   claude-opus-5 xhigh   73% +/-3   $9.07    89 turns   (same score, +50% cost)
#   claude-opus-5 max     74% +/-4  $11.84    99 turns   (+1 point, ~2x cost)
#   claude-opus-5 medium  69% +/-1   $3.29    52 turns
#   gpt-5.6-sol high      69% +/-1   $3.47    37 turns   (leanest of the top tier)
#   gpt-5.6-sol xhigh     71% +/-1   $4.70    44 turns
#   gpt-5.6-luna max      67% +/-4   $0.61   102 turns   (chatty, but 1/5 the price)
#   claude-sonnet-5 high  48% +/-5   $7.43   147 turns   (dominated everywhere)
#
# Sonnet is deliberately absent. It lost to claude-opus-5 medium on score, cost
# and turn count simultaneously, so there is no slot where it is the right pick.
PHASE_MODEL_POLICY = {
    "coding": {
        # Research runs Luna at max, which the published benchmarks say it should
        # not. Luna's 41.3% MRCR long-context recall (vs Terra 89.6%) reads as
        # disqualifying, so it was measured before being believed: one real
        # Research leg each, Luna max vs Terra max, same prompt and checkout,
        # graded against a 20-fact key written in advance with six facts planted
        # mid-file where MRCR predicts Luna collapses. Luna scored 18.5/20 to
        # Terra's 16, both 6/6 on the mid-file facts, in 31% less wall clock and
        # ~30% fewer output and reasoning tokens. MRCR measures needle retrieval
        # from one loaded context; a Fleet worker reads in ~500-line chunks and
        # re-reads on demand, so the cliff does not describe this phase.
        # (2026-08-20, knowledge/openai-models/best-research-model-aug-2026.md)
        "discover": (("codex", "gpt-5.6-luna", "max"),),
        # Code runs medium, Fix runs high, deliberately not the same rung.
        # On DeepSWE medium is 69% at 37k tokens against high's 73% at 64k, so
        # the 4 points cost 42% more burn on the phase that generates the most
        # of it. Code can afford that because a defect it introduces is caught
        # by Review and repaired by Fix; those two phases exist for exactly this.
        # Watch Review findings per run: if they climb, the saving is being paid
        # back in retries, which cost a whole leg and wipe it out several times
        # over. (2026-08-24)
        "execute": (("claude", "claude-opus-5", "medium"),),
        # Sol high is the leanest strong reviewer on the board, 28k tokens across
        # 37 turns. Review reads a diff and argues with it; paying xhigh for more
        # turns is not what makes that better.
        "verify": (("codex", "gpt-5.6-sol", "high"),),
        # Fix stays high while Code drops to medium. It is the phase with no
        # safety net: its mistakes land in already-reviewed code that nothing
        # downstream re-reads, so the rung that is cheap to give up on Code is
        # not cheap to give up here.
        "finalize": (("claude", "claude-opus-5", "high"),),
    },
    # Research runs read, analyse, review, refine. Same four models in the same
    # order: Luna reads, Opus analyses, Sol reviews, Opus refines.
    "research": {
        "discover": (("codex", "gpt-5.6-luna", "max"),),
        "execute": (("claude", "claude-opus-5", "high"),),
        "verify": (("codex", "gpt-5.6-sol", "high"),),
        "finalize": (("claude", "claude-opus-5", "high"),),
    },
}

# Escape hatches for when one provider is exhausted or explicitly excluded. The
# pipeline above needs both providers alive; these keep a run possible on one.
# They are explicit provider substitutions, never chosen silently, and always
# recorded in the frozen policy as the mode that was actually selected.
PROVIDER_ONLY_POLICY = {
    "codex": {
        "coding": {
            "discover": (("codex", "gpt-5.6-luna", "max"),),
            # Sol xhigh gives an exhausted Opus-medium Code leg a small quality
            # margin instead of merely matching it at Sol high.
            "execute": (("codex", "gpt-5.6-sol", "xhigh"),),
            "verify": (("codex", "gpt-5.6-sol", "high"),),
            # Fix normally runs Opus high and has no downstream safety net, so
            # its Codex substitute is Sol max rather than the Code-phase rung.
            "finalize": (("codex", "gpt-5.6-sol", "max"),),
        },
        "research": {
            "discover": (("codex", "gpt-5.6-luna", "max"),),
            "execute": (("codex", "gpt-5.6-sol", "xhigh"),),
            "verify": (("codex", "gpt-5.6-sol", "high"),),
            "finalize": (("codex", "gpt-5.6-sol", "xhigh"),),
        },
    },
    # Opus 5 medium replaces what used to be Sonnet here: 69% at $3.29 over 52
    # turns against Sonnet high's 48% at $7.43 over 147. Better, cheaper, faster.
    "claude": {
        "coding": {
            "discover": (("claude", "claude-opus-5", "medium"),),
            "execute": (("claude", "claude-opus-5", "medium"),),
            "verify": (("claude", "claude-opus-5", "medium"),),
            "finalize": (("claude", "claude-opus-5", "high"),),
        },
        "research": {
            "discover": (("claude", "claude-opus-5", "medium"),),
            "execute": (("claude", "claude-opus-5", "high"),),
            "verify": (("claude", "claude-opus-5", "medium"),),
            "finalize": (("claude", "claude-opus-5", "high"),),
        },
    },
}

_LEGACY_PROFILE_PHASE = {"coding": "execute", "research": "discover"}


RESEARCH_DEPTHS = frozenset({"full"})

_CODING_SIGNAL = re.compile(
    r"\b(?:build|code|coding|implement|fix|patch|refactor|debug|test|tests|"
    r"feature|bug|repo|repository|worktree|commit|function|class|module|api|"
    r"frontend|backend|database|migration|deploy|css|html|typescript|python|"
    r"javascript|rust|go|java|swift|kotlin)\b",
    re.IGNORECASE,
)
_RESEARCH_SIGNAL = re.compile(
    r"\b(?:research|investigate|compare|sources?|citations?|papers?|literature|"
    r"evidence|market|history|explain|report|find out|look up|verify claims?)\b",
    re.IGNORECASE,
)
_WORKSTREAM_SECTION = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?"
    r"(?:use\s+(?:(?:one|two|three|four|[1-4])\s+)?(?:independent\s+)?)?"
    r"(?:tasks?|objectives?|workstreams?)\s*:\s*(.*)$",
    re.IGNORECASE,
)
_NAMED_WORKSTREAM = re.compile(
    r"^\s{0,3}(?:[-*+]\s+)?(?:task|objective|workstream)\s+"
    r"(?:#\s*)?(?:\d{1,2}|[a-z])\s*[:.)\-–—]\s*(\S.*)$",
    re.IGNORECASE,
)
_NUMBERED_WORKSTREAM = re.compile(r"^\s{0,3}\d{1,2}[.)]\s+(\S.*)$")
_LETTERED_WORKSTREAM = re.compile(r"^\s{0,3}[a-z][.)]\s+(\S.*)$", re.IGNORECASE)
_BULLETED_WORKSTREAM = re.compile(r"^\s{0,3}[-*+]\s+(?:\[[ xX]\]\s+)?(\S.*)$")
_NON_WORKSTREAM = re.compile(
    r"^(?:do not|don't|must not|never|constraints?\b|acceptance criteria\b|"
    r"definition of done\b)",
    re.IGNORECASE,
)
_PHASE_ONLY = re.compile(
    r"^(?:research|discover|analy[sz]e|plan|implement|code|review|verify|test|fix|"
    r"finalize|refine|report)(?:\s+(?:it|everything|the task|the work|the changes|"
    r"results?|code|implementation|findings?))?[.!]?$",
    re.IGNORECASE,
)
_DIRECTIVE_PREFIX = (
    r"(?:^|[\n.!?;])\s*(?:please\s+)?(?:fleet\s*[:=-]?\s*)?"
    r"(?:(?:hard\s+)?(?:worker|provider)\s+routing\s*:\s*)?"
)
_DIRECTIVE_SCOPE_END = (
    r"(?=\s*(?:$|[.!?:;,])|\s+for\s+(?:(?:this|the)\s+)?(?:run|task|fleet)\b)"
)
_ONLY_PROVIDER_DIRECTIVE = re.compile(
    _DIRECTIVE_PREFIX
    + r"(?:"
    + r"(?:use|run|spawn|launch)\s+"
    + r"(?:(?P<count_a>[1-4]|one|two|three|four)\s+)?"
    + r"(?P<provider_a>codex|claude)(?:\s+(?:agents?|workers?|chats?))?\s+only"
    + r"|(?:use|run|spawn|launch)\s+only\s+"
    + r"(?:(?P<count_b>[1-4]|one|two|three|four)\s+)?"
    + r"(?P<provider_b>codex|claude)(?:\s+(?:agents?|workers?|chats?))?"
    + r"|only\s+(?:(?P<count_c>[1-4]|one|two|three|four)\s+)?"
    + r"(?P<provider_c>codex|claude)(?:\s+(?:agents?|workers?|chats?))?"
    + _DIRECTIVE_SCOPE_END
    + r"|(?P<provider_d>codex|claude)[-\s]+only(?=\s*(?:$|[:;,]))"
    + r")\b",
    re.IGNORECASE,
)
_EXCLUDED_PROVIDER_DIRECTIVE = re.compile(
    _DIRECTIVE_PREFIX
    + r"(?:"
    + r"(?:no|without|zero)\s*[- ]?\s*(?P<excluded_a>codex|claude)"
    + r"(?:\s+(?:agents?|workers?|chats?))?"
    + _DIRECTIVE_SCOPE_END
    + r"|(?:do\s+not|don't)\s+(?:use|run|spawn|launch)\s+(?:any\s+)?"
    + r"(?P<excluded_b>codex|claude)(?:\s+(?:agents?|workers?|chats?))?"
    + r")\b",
    re.IGNORECASE,
)
_COUNT_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4}


@dataclass(frozen=True, slots=True)
class WorkstreamPolicy:
    workstream_id: str
    title: str
    description: str
    synthetic: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.workstream_id,
            "title": self.title,
            "description": self.description,
            "synthetic": self.synthetic,
        }


@dataclass(frozen=True, slots=True)
class ScalingPolicy:
    mode: str
    detected_workstreams: int
    selected_workers: int
    capacity: int
    capacity_limited: bool
    reason: str
    requested_workers: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "detected_workstreams": self.detected_workstreams,
            "selected_workers": self.selected_workers,
            "capacity": self.capacity,
            "capacity_limited": self.capacity_limited,
            "reason": self.reason,
            "requested_workers": self.requested_workers,
        }


@dataclass(frozen=True, slots=True)
class WorkerPolicy:
    provider: str
    model: str
    effort: str
    role: str
    access_mode: str
    worker_key: str = ""
    worker_label: str = ""
    assignment: str = ""
    assignment_ids: tuple[str, ...] = ()
    review_target_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "runtime": self.provider,
            "model": self.model,
            "effort": self.effort,
            "role": self.role,
            "access_mode": self.access_mode,
            "worker_key": self.worker_key,
            "worker_label": self.worker_label,
            "assignment": self.assignment,
            "assignment_ids": list(self.assignment_ids),
            "review_target_ids": list(self.review_target_ids),
        }


@dataclass(frozen=True, slots=True)
class PhasePolicy:
    index: int
    name: str
    display_name: str
    execution: str
    workers: tuple[WorkerPolicy, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "display_name": self.display_name,
            "execution": self.execution,
            "workers": [worker.to_dict() for worker in self.workers],
        }


@dataclass(frozen=True, slots=True)
class FleetPolicy:
    schema_version: int
    activity: str
    requested_provider_mode: str
    provider_mode: str
    session_mode: str
    research_depth: str
    minimum_workers_per_phase: int
    no_silent_fallback: bool
    capacity_handoff: str
    max_parallel_workers: int
    max_parallel_writers: int
    scaling: ScalingPolicy
    workstreams: tuple[WorkstreamPolicy, ...]
    work_units: tuple[dict[str, Any], ...]
    phases: tuple[PhasePolicy, ...]
    handoffs: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "activity": self.activity,
            "requested_provider_mode": self.requested_provider_mode,
            "provider_mode": self.provider_mode,
            "session_mode": self.session_mode,
            "research_depth": self.research_depth,
            "minimum_workers_per_phase": self.minimum_workers_per_phase,
            "no_silent_fallback": self.no_silent_fallback,
            "capacity_handoff": self.capacity_handoff,
            "max_parallel_workers": self.max_parallel_workers,
            "max_parallel_writers": self.max_parallel_writers,
            "scaling": self.scaling.to_dict(),
            "workstreams": [workstream.to_dict() for workstream in self.workstreams],
            "work_units": [deepcopy(work_unit) for work_unit in self.work_units],
            "phases": [phase.to_dict() for phase in self.phases],
            "handoffs": [dict(handoff) for handoff in self.handoffs],
        }


def _worker_entries(
    specs: tuple[tuple[str, str, str], ...],
) -> list[dict[str, str]]:
    return [
        {"provider": provider, "model": model, "effort": effort}
        for provider, model, effort in specs
    ]


def builtin_config() -> dict[str, Any]:
    """Return an independent copy of Fleet's safe default configuration."""

    return {
        "schema_version": SCHEMA_VERSION,
        "defaults": {
            "phases": list(PHASES),
            "session_mode": "persistent_by_worker",
            "minimum_workers_per_phase": 1,
            "no_silent_fallback": True,
            "capacity_handoff": "automatic",
            "max_parallel_workers": 4,
            "max_parallel_writers": 2,
            # Which real accounts a non-writing leg may read. Enforced per tool
            # by core/fleet_read_mcp.py, never by the leg's label alone.
            "mcp_read_access": {
                "enabled": True,
                "servers": list(DEFAULT_READ_SERVERS),
            },
        },
        "profiles": {
            "coding": {
                # Retained for old Fleet MCP processes during a rolling restart.
                "workers": _worker_entries(PHASE_MODEL_POLICY["coding"]["execute"]),
                "phases": {
                    phase: _worker_entries(PHASE_MODEL_POLICY["coding"][phase])
                    for phase in PHASES
                },
            },
            "research": {
                "workers": _worker_entries(PHASE_MODEL_POLICY["research"]["discover"]),
                "phases": {
                    phase: _worker_entries(PHASE_MODEL_POLICY["research"][phase])
                    for phase in PHASES
                },
            },
        },
    }


def config_path() -> Path | None:
    explicit = os.environ.get("SERENA_FLEET_CONFIG", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if DEFAULT_CONFIG_PATH.is_file():
        return DEFAULT_CONFIG_PATH
    if REPOSITORY_CONFIG_PATH.is_file():
        return REPOSITORY_CONFIG_PATH
    return None


def load_config(path: Path | None = None) -> dict[str, Any]:
    chosen = Path(path).expanduser() if path is not None else config_path()
    if chosen is None:
        data = builtin_config()
    else:
        try:
            data = json.loads(chosen.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid Fleet configuration at {chosen}: {exc}") from exc
    validate_config(data)
    return data


def validate_config(data: object) -> None:
    if not isinstance(data, dict):
        raise ValueError("Fleet configuration must be a JSON object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Fleet schema_version must be {SCHEMA_VERSION}")
    defaults = data.get("defaults")
    profiles = data.get("profiles")
    if not isinstance(defaults, dict) or not isinstance(profiles, dict):
        raise ValueError("Fleet configuration requires defaults and profiles")
    phases = tuple(str(value) for value in (defaults.get("phases") or ()))
    if phases != PHASES:
        raise ValueError(f"Fleet phases must be exactly {list(PHASES)}")
    minimum = _positive_int(defaults.get("minimum_workers_per_phase"), "minimum_workers_per_phase")
    parallel = _positive_int(defaults.get("max_parallel_workers"), "max_parallel_workers")
    if parallel > MAX_WORKERS:
        raise ValueError(f"max_parallel_workers cannot exceed {MAX_WORKERS}")
    _positive_int(defaults.get("max_parallel_writers", 2), "max_parallel_writers")
    if defaults.get("no_silent_fallback") is not True:
        raise ValueError("no_silent_fallback must remain true")
    _validate_mcp_read_access(defaults.get("mcp_read_access"))
    capacity_handoff = str(defaults.get("capacity_handoff") or "automatic").lower()
    if capacity_handoff not in CAPACITY_HANDOFF_MODES:
        raise ValueError("capacity_handoff must be automatic or manual")
    session_mode = str(defaults.get("session_mode") or "persistent_by_worker")
    if session_mode not in SESSION_MODES:
        raise ValueError("session_mode must be per_leg or persistent_by_worker")
    for activity in ("coding", "research"):
        profile = profiles.get(activity)
        workers = profile.get("workers") if isinstance(profile, dict) else None
        if not isinstance(workers, list) or len(workers) < minimum:
            raise ValueError(f"{activity} requires at least {minimum} workers")
        for worker in workers:
            _validate_worker(worker, activity)
        # A phase used to need one template per provider. It now carries exactly
        # one locked spec, and which provider that implies is the phase's choice.
        for worker in workers:
            if str(worker.get("provider") or "").lower() not in PROVIDERS:
                raise ValueError(f"{activity} worker provider must be codex or claude")
        legacy_specs = _configured_worker_specs(workers)
        expected_legacy = PHASE_MODEL_POLICY[activity][_LEGACY_PROFILE_PHASE[activity]]
        if legacy_specs != expected_legacy:
            raise ValueError(f"{activity} legacy workers must match Fleet's fixed model policy")

        phase_workers = profile.get("phases") if isinstance(profile, dict) else None
        if not isinstance(phase_workers, dict) or set(phase_workers) != set(PHASES):
            raise ValueError(f"{activity} requires exact phase-specific Fleet workers")
        for phase in PHASES:
            configured = phase_workers.get(phase)
            if not isinstance(configured, list) or len(configured) < minimum:
                raise ValueError(f"{activity} {phase} requires at least {minimum} workers")
            for worker in configured:
                _validate_worker(worker, f"{activity} {phase}")
            if _configured_worker_specs(configured) != PHASE_MODEL_POLICY[activity][phase]:
                raise ValueError(
                    f"{activity} {phase} workers must match Fleet's fixed model policy"
                )


def resolve_activity(task: str, activity: str = "auto", cwd: str | Path | None = None) -> str:
    clean = str(activity or "auto").strip().lower()
    if clean not in ACTIVITIES:
        raise ValueError("activity must be auto, coding, or research")
    if clean != "auto":
        return clean
    text = " ".join(str(task or "").split())
    coding = bool(_CODING_SIGNAL.search(text))
    research = bool(_RESEARCH_SIGNAL.search(text))
    if coding and not research:
        return "coding"
    if research and not coding:
        return "research"
    if coding:
        return "coding"
    if cwd:
        root = Path(cwd).expanduser()
        if (root / ".git").exists():
            return "coding"
    return "research"


def extract_explicit_workstreams(task: str) -> tuple[str, ...]:
    """Extract only work the user presented as an explicit task list.

    Fleet deliberately does not guess complexity from prose. Named and numbered
    items are explicit on their own. Bullets count only inside a tasks,
    objectives, or workstreams section, which avoids treating ordinary
    requirements and acceptance criteria as independent writers.
    """

    candidates: list[str] = []
    in_section = False
    pending: int | None = None
    for raw_line in str(task or "").splitlines():
        line = raw_line.rstrip()
        section = _WORKSTREAM_SECTION.match(line)
        if section:
            in_section = True
            pending = None
            inline = section.group(1).strip()
            if inline:
                parts = re.split(r"\s*;\s*", inline) if ";" in inline else [inline]
                for part in parts:
                    if part.strip():
                        candidates.append(part.strip())
                        pending = len(candidates) - 1
            continue

        named = _NAMED_WORKSTREAM.match(line)
        numbered = _NUMBERED_WORKSTREAM.match(line)
        if named or numbered:
            candidates.append((named or numbered).group(1).strip())
            pending = len(candidates) - 1
            in_section = False
            continue

        lettered = _LETTERED_WORKSTREAM.match(line) if in_section else None
        if lettered:
            candidates.append(lettered.group(1).strip())
            pending = len(candidates) - 1
            continue

        bullet = _BULLETED_WORKSTREAM.match(line) if in_section else None
        if bullet:
            candidates.append(bullet.group(1).strip())
            pending = len(candidates) - 1
            continue
        if not line.strip():
            continue
        indentation = len(line) - len(line.lstrip())
        if pending is not None and indentation >= 2:
            candidates[pending] = f"{candidates[pending]} {line.strip()}"
            continue
        in_section = False
        pending = None

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        clean = _clean_workstream(candidate)
        if not clean:
            continue
        key = re.sub(r"[^a-z0-9]+", " ", clean.casefold()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(clean)
        if len(unique) >= MAX_EXPLICIT_WORKSTREAMS:
            break
    return tuple(unique)


def _directive_count(match: re.Match[str]) -> int | None:
    raw = next(
        (
            str(match.group(name)).casefold()
            for name in ("count_a", "count_b", "count_c")
            if name in match.re.groupindex and match.group(name)
        ),
        None,
    )
    if raw is None:
        return None
    return _COUNT_WORDS.get(raw, int(raw) if raw.isdigit() else None)


def _task_provider_directive(task: str) -> tuple[str | None, int | None]:
    requests: list[tuple[str, int | None]] = []
    for match in _ONLY_PROVIDER_DIRECTIVE.finditer(str(task or "")):
        provider = next(
            str(match.group(name)).casefold()
            for name in ("provider_a", "provider_b", "provider_c", "provider_d")
            if match.group(name)
        )
        requests.append((provider, _directive_count(match)))
    for match in _EXCLUDED_PROVIDER_DIRECTIVE.finditer(str(task or "")):
        excluded = next(
            str(match.group(name)).casefold()
            for name in ("excluded_a", "excluded_b")
            if match.group(name)
        )
        requests.append(("claude" if excluded == "codex" else "codex", None))
    providers = {provider for provider, _count in requests}
    if len(providers) > 1:
        raise ValueError("Fleet task contains conflicting codex-only and claude-only directives")
    counts = {count for _provider, count in requests if count is not None}
    if len(counts) > 1:
        raise ValueError("Fleet task contains conflicting worker-count directives")
    return (
        next(iter(providers), None),
        next(iter(counts), None),
    )


def _optional_worker_count(value: object, name: str = "worker_count") -> int | None:
    if value is None:
        return None
    parsed = _positive_int(value, name)
    if parsed > MAX_WORKERS:
        raise ValueError(f"{name} cannot exceed {MAX_WORKERS}")
    return parsed


def _capacity_value(entry: object) -> tuple[bool, str]:
    if isinstance(entry, bool):
        return entry, ""
    if isinstance(entry, str):
        status = entry.strip().casefold().replace("-", "_").replace(" ", "_")
        if status in {"unavailable", "unusable", "exhausted", "quota_exhausted", "blocked", "disabled", "offline", "failed"}:
            return False, entry.strip()
        return True, entry.strip()
    if isinstance(entry, Mapping):
        usable = entry.get("usable")
        status = entry.get("status")
        reason = entry.get("reason")
    else:
        usable = getattr(entry, "usable", None)
        status = getattr(entry, "status", None)
        reason = getattr(entry, "reason", None)
    if isinstance(usable, str):
        clean = usable.strip().casefold()
        if clean in {"true", "yes", "1", "on", "usable", "available"}:
            usable = True
        elif clean in {"false", "no", "0", "off", "unusable", "unavailable"}:
            usable = False
        else:
            usable = None
    if isinstance(usable, (int, float)) and not isinstance(usable, bool):
        usable = bool(usable)
    status_text = str(status or "").strip()
    status_key = status_text.casefold().replace("-", "_").replace(" ", "_")
    if not isinstance(usable, bool):
        usable = status_key not in {
            "unavailable",
            "unusable",
            "exhausted",
            "quota_exhausted",
            "blocked",
            "disabled",
            "offline",
            "failed",
        }
    return usable, str(reason or status_text).strip()


def _provider_capacity(
    value: Mapping[str, object] | None,
) -> dict[str, tuple[bool, str]]:
    if value is None:
        return {provider: (True, "") for provider in PROVIDERS}
    if not isinstance(value, Mapping):
        raise ValueError("provider_capacity must be a mapping")
    return {
        provider: _capacity_value(value[provider]) if provider in value else (True, "")
        for provider in PROVIDERS
    }


def _resolve_provider_mode(
    task: str,
    requested: object,
    provider_capacity: Mapping[str, object] | None,
) -> tuple[str, str, int | None, dict[str, tuple[bool, str]], str]:
    requested_mode = str(requested or "auto").strip().casefold()
    if requested_mode not in REQUESTED_PROVIDER_MODES:
        raise ValueError("provider_mode must be auto, balanced, mixed, codex, or claude")
    explicit_mode = "balanced" if requested_mode == "mixed" else requested_mode
    task_mode, task_count = _task_provider_directive(task)
    if explicit_mode != "auto" and task_mode and task_mode != explicit_mode:
        raise ValueError(
            f"Fleet provider_mode {requested_mode} conflicts with the task's {task_mode}-only directive"
        )
    capacities = _provider_capacity(provider_capacity)
    routing_reason = ""
    if explicit_mode == "auto":
        if task_mode:
            selected_mode = task_mode
        else:
            usable = [provider for provider in ("codex", "claude") if capacities[provider][0]]
            if not usable:
                raise ValueError("no usable Fleet providers are available")
            selected_mode = "balanced" if len(usable) == 2 else usable[0]
            if len(usable) == 1:
                excluded = "claude" if usable[0] == "codex" else "codex"
                routing_reason = (
                    f"auto excluded {excluded}: "
                    f"{capacities[excluded][1] or 'provider unavailable'}"
                )
    else:
        selected_mode = explicit_mode
    required = PROVIDERS if selected_mode == "balanced" else {selected_mode}
    unavailable = [provider for provider in sorted(required) if not capacities[provider][0]]
    if unavailable:
        details = "; ".join(
            f"{provider}: {capacities[provider][1] or 'unavailable'}" for provider in unavailable
        )
        raise ValueError(f"requested Fleet provider mode {selected_mode} is unavailable ({details})")
    requested_selection = task_mode if requested_mode == "auto" and task_mode else requested_mode
    return requested_selection, selected_mode, task_count, capacities, routing_reason


def build_policy(
    activity: str,
    task: str = "",
    config: dict[str, Any] | None = None,
    *,
    cwd: str | Path | None = None,
    provider_mode: str = "auto",
    worker_count: int | None = None,
    provider_capacity: Mapping[str, object] | None = None,
) -> FleetPolicy:
    if activity not in {"coding", "research"}:
        raise ValueError("resolved Fleet activity must be coding or research")
    data = config if config is not None else load_config()
    validate_config(data)
    defaults = data["defaults"]
    configured_phases = data["profiles"][activity]["phases"]
    explicit = extract_explicit_workstreams(task)
    capacity = int(defaults["max_parallel_workers"])
    (
        requested_mode,
        selected_mode,
        task_worker_count,
        _capacities,
        routing_reason,
    ) = _resolve_provider_mode(
        task,
        provider_mode,
        provider_capacity,
    )
    if selected_mode == "balanced" and capacity < 2:
        if requested_mode != "auto":
            raise ValueError("balanced Fleet mode requires configured capacity of at least two")
        selected_mode = "codex"
        routing_reason = "auto could not form a balanced pair at configured capacity 1"
    explicit_worker_count = _optional_worker_count(worker_count)
    if (
        explicit_worker_count is not None
        and task_worker_count is not None
        and explicit_worker_count != task_worker_count
    ):
        raise ValueError("worker_count conflicts with the task's worker-count directive")
    requested_workers = explicit_worker_count or task_worker_count
    if requested_workers is not None and requested_workers > capacity:
        raise ValueError(
            f"requested worker_count {requested_workers} exceeds configured capacity {capacity}"
        )
    # Worker count is now independent of provider mode. A phase runs one model
    # and every agent runs it, so there is no pair to keep even.
    desired_workers = requested_workers or min(MAX_WORKERS, max(1, len(explicit)))
    selected_workers = min(desired_workers, capacity)
    capacity_limited = selected_workers < desired_workers
    if routing_reason:
        reason = (
            f"{routing_reason}; selected {selected_workers} {selected_mode}-only "
            f"worker{'s' if selected_workers != 1 else ''}"
        )
    elif capacity_limited:
        reason = (
            f"{desired_workers} workers inferred for {selected_mode} mode, but configured "
            f"capacity is {capacity}"
        )
    elif requested_workers is not None:
        reason = f"explicit request selected {selected_workers} {selected_mode} Fleet workers"
    elif selected_mode == "balanced" and selected_workers == 4:
        reason = f"{len(explicit)} explicit workstreams selected a balanced four-agent team"
    elif selected_mode == "balanced":
        reason = "fewer than three explicit workstreams selected the balanced two-agent baseline"
    else:
        reason = (
            f"{len(explicit)} explicit workstreams selected {selected_workers} "
            f"{selected_mode}-only Fleet worker{'s' if selected_workers != 1 else ''}"
        )
    scaling = ScalingPolicy(
        mode="explicit_workstreams",
        detected_workstreams=len(explicit),
        selected_workers=selected_workers,
        capacity=capacity,
        capacity_limited=capacity_limited,
        reason=reason,
        requested_workers=requested_workers,
    )
    workstreams, assignment_ids = _build_workstream_plan(
        task,
        explicit,
        selected_workers,
    )
    by_id = {workstream.workstream_id: workstream for workstream in workstreams}
    assignments = tuple(_assignment_text(ids, by_id) for ids in assignment_ids)
    review_targets = (
        ((),)
        if selected_workers == 1
        else tuple(
            assignment_ids[(ordinal + 1) % selected_workers]
            for ordinal in range(selected_workers)
        )
    )
    # The locked pipeline needs both providers alive. A provider-only run is an
    # explicit downgrade to that provider's stack, never a silent substitution.
    if selected_mode in PROVIDER_ONLY_POLICY:
        phase_workers = {
            phase: _worker_entries(PROVIDER_ONLY_POLICY[selected_mode][activity][phase])
            for phase in PHASES
        }
    else:
        phase_workers = configured_phases
    phases: list[PhasePolicy] = []
    for index, name in enumerate(PHASES):
        roster = _select_roster(
            phase_workers[name],
            selected_workers,
            selected_mode,
        )
        workers = tuple(
            WorkerPolicy(
                provider=str(raw["provider"]).lower(),
                model=str(raw["model"]),
                effort=str(raw["effort"]).lower(),
                role=_role(activity, name, ordinal, str(raw["provider"]).lower()),
                access_mode=_access_mode(activity, name, ordinal),
                # Identity is the agent slot, not the runtime. Agent A stays
                # Agent A while the provider under it changes every phase.
                worker_key=f"agent:{suffix}",
                worker_label=f"Agent {suffix.upper()}",
                assignment=assignments[ordinal],
                assignment_ids=assignment_ids[ordinal],
                review_target_ids=review_targets[ordinal],
            )
            for ordinal, (raw, suffix) in enumerate(roster)
        )
        phases.append(
            PhasePolicy(
                index=index,
                name=name,
                display_name=_display_name(activity, name),
                execution="parallel",
                workers=workers,
            )
        )
    work_unit_contracts = build_work_unit_contracts(
        activity,
        [workstream.to_dict() for workstream in workstreams],
        [worker.to_dict() for worker in phases[0].workers],
        cwd=cwd,
    )
    policy = FleetPolicy(
        schema_version=SCHEMA_VERSION,
        activity=activity,
        requested_provider_mode=requested_mode,
        provider_mode=selected_mode,
        session_mode=str(defaults.get("session_mode") or "persistent_by_worker"),
        research_depth=resolve_research_depth(activity, task),
        minimum_workers_per_phase=min(
            int(defaults["minimum_workers_per_phase"]),
            selected_workers,
        ),
        no_silent_fallback=True,
        capacity_handoff=str(defaults.get("capacity_handoff") or "automatic").lower(),
        max_parallel_workers=int(defaults["max_parallel_workers"]),
        max_parallel_writers=int(defaults.get("max_parallel_writers", 2)),
        scaling=scaling,
        workstreams=workstreams,
        work_units=tuple(work_unit_contracts),
        phases=tuple(phases),
        handoffs=(),
    )
    validate_policy_snapshot(policy.to_dict())
    return policy


def _clean_workstream(value: object) -> str:
    clean = " ".join(str(value or "").strip().strip("; ").split())
    clean = clean.strip("*_` ")
    if not clean or _NON_WORKSTREAM.match(clean) or _PHASE_ONLY.fullmatch(clean):
        return ""
    return clean[:4_000]


def _workstream_title(description: str) -> str:
    first_sentence = re.split(r"(?<=[.!?])\s+", description, maxsplit=1)[0]
    if len(first_sentence) <= 160:
        return first_sentence
    return first_sentence[:157].rstrip() + "..."


def _integration_workstream(
    identifier: str,
    explicit: tuple[str, ...],
    task: str,
) -> WorkstreamPolicy:
    scope = ", ".join(f"ws-{index}" for index in range(1, len(explicit) + 1))
    if scope:
        description = (
            f"Own integration, shared dependencies, and end-to-end coherence across {scope}."
        )
    else:
        description = (
            "Own architecture, integration, risk, and independent validation across the complete "
            f"task: {' '.join(str(task or '').split())[:2_000]}"
        )
    return WorkstreamPolicy(
        workstream_id=identifier,
        title="integration and shared dependencies",
        description=description,
        synthetic=True,
    )


def _build_workstream_plan(
    task: str,
    explicit: tuple[str, ...],
    selected_workers: int,
) -> tuple[tuple[WorkstreamPolicy, ...], tuple[tuple[str, ...], ...]]:
    workstreams = [
        WorkstreamPolicy(
            workstream_id=f"ws-{index}",
            title=_workstream_title(description),
            description=description,
        )
        for index, description in enumerate(explicit, start=1)
    ]
    if not workstreams:
        description = " ".join(str(task or "").split())[:4_000] or "complete the Fleet task"
        workstreams.append(
            WorkstreamPolicy(
                workstream_id="ws-1",
                title="primary delivery",
                description=description,
                synthetic=True,
            )
        )
    if len(workstreams) < selected_workers:
        while len(workstreams) < selected_workers:
            workstreams.append(
                _integration_workstream(
                    f"ws-{len(workstreams) + 1}",
                    explicit,
                    task,
                )
            )
    buckets: list[list[str]] = [[] for _ in range(selected_workers)]
    for index, workstream in enumerate(workstreams):
        buckets[index % selected_workers].append(workstream.workstream_id)
    return tuple(workstreams), tuple(tuple(bucket) for bucket in buckets)


def _assignment_text(
    identifiers: tuple[str, ...],
    workstreams: dict[str, WorkstreamPolicy],
) -> str:
    return " | ".join(
        f"{identifier}: {workstreams[identifier].description}" for identifier in identifiers
    )


def _select_roster(
    workers: list[dict[str, Any]],
    selected_workers: int,
    provider_mode: str,
) -> tuple[tuple[dict[str, Any], str], ...]:
    """Give every agent in this phase the phase's one locked spec.

    Workers are no longer branded by provider. Agent A is Agent A across the
    whole run; which provider it runs on is a property of the phase, not of the
    worker. The roster therefore just repeats the phase spec once per agent and
    numbers them a, b, c, d.
    """

    if not workers:
        raise ValueError("a Fleet phase must configure exactly one worker spec")
    spec = workers[0]
    return tuple(
        (spec, chr(ord("a") + worker_index)) for worker_index in range(selected_workers)
    )


def _worker_from_snapshot(worker: dict[str, Any], ordinal: int) -> WorkerPolicy:
    provider = str(worker.get("provider") or worker.get("runtime"))
    # One agent per ordinal now; the old layout interleaved two providers per
    # slot and had to halve the ordinal to recover the letter.
    suffix = chr(ord("a") + ordinal)
    return WorkerPolicy(
        provider=provider,
        model=str(worker["model"]),
        effort=str(worker["effort"]),
        role=str(worker["role"]),
        access_mode=str(worker["access_mode"]),
        worker_key=str(worker.get("worker_key") or f"agent:{suffix}"),
        worker_label=str(worker.get("worker_label") or f"Agent {suffix.upper()}"),
        assignment=str(worker.get("assignment") or ""),
        assignment_ids=tuple(str(value) for value in (worker.get("assignment_ids") or ())),
        review_target_ids=tuple(str(value) for value in (worker.get("review_target_ids") or ())),
    )


def policy_from_snapshot(snapshot: dict[str, Any]) -> FleetPolicy:
    validate_policy_snapshot(snapshot)
    raw_workstreams = snapshot.get("workstreams") or []
    workstreams = tuple(
        WorkstreamPolicy(
            workstream_id=str(item["id"]),
            title=str(item.get("title") or item["id"]),
            description=str(item.get("description") or item.get("title") or item["id"]),
            synthetic=bool(item.get("synthetic")),
        )
        for item in raw_workstreams
        if isinstance(item, dict)
    )
    phases = tuple(
        PhasePolicy(
            index=int(phase["index"]),
            name=str(phase["name"]),
            display_name=str(phase.get("display_name") or phase["name"]),
            execution=str(phase["execution"]),
            workers=tuple(
                _worker_from_snapshot(worker, ordinal)
                for ordinal, worker in enumerate(phase["workers"])
            ),
        )
        for phase in snapshot["phases"]
    )
    parallel = int(snapshot["max_parallel_workers"])
    raw_scaling = snapshot.get("scaling")
    if isinstance(raw_scaling, dict):
        raw_requested_workers = raw_scaling.get("requested_workers")
        scaling = ScalingPolicy(
            mode=str(raw_scaling.get("mode") or "explicit_workstreams"),
            detected_workstreams=int(raw_scaling.get("detected_workstreams") or 0),
            selected_workers=int(raw_scaling.get("selected_workers") or len(phases[0].workers)),
            capacity=int(raw_scaling.get("capacity") or parallel),
            capacity_limited=bool(raw_scaling.get("capacity_limited")),
            reason=str(raw_scaling.get("reason") or "snapshotted Fleet scaling policy"),
            requested_workers=(
                int(raw_requested_workers) if raw_requested_workers is not None else None
            ),
        )
    else:
        scaling = ScalingPolicy(
            mode="legacy",
            detected_workstreams=0,
            selected_workers=len(phases[0].workers),
            capacity=parallel,
            capacity_limited=False,
            reason="legacy policy snapshot",
            requested_workers=None,
        )
    return FleetPolicy(
        schema_version=int(snapshot["schema_version"]),
        activity=str(snapshot["activity"]),
        requested_provider_mode=str(snapshot.get("requested_provider_mode") or "auto"),
        provider_mode=str(snapshot.get("provider_mode") or "balanced"),
        # Old snapshots did not declare a session topology. Keeping those
        # phase-local avoids changing an already-materialized run mid-flight.
        session_mode=str(snapshot.get("session_mode") or "per_leg"),
        # Full research is the only active mandate. Legacy proportionate
        # snapshots are upgraded when read instead of weakening a retry.
        research_depth=(
            str(snapshot.get("research_depth") or "full").lower()
            if str(snapshot.get("research_depth") or "full").lower() in RESEARCH_DEPTHS
            else "full"
        ),
        minimum_workers_per_phase=int(snapshot["minimum_workers_per_phase"]),
        no_silent_fallback=bool(snapshot["no_silent_fallback"]),
        capacity_handoff=str(snapshot.get("capacity_handoff") or "automatic").lower(),
        max_parallel_workers=parallel,
        max_parallel_writers=int(snapshot.get("max_parallel_writers") or 2),
        scaling=scaling,
        workstreams=workstreams,
        work_units=tuple(
            deepcopy(item)
            for item in (snapshot.get("work_units") or ())
            if isinstance(item, dict)
        ),
        phases=phases,
        handoffs=tuple(
            dict(item)
            for item in (snapshot.get("handoffs") or ())
            if isinstance(item, dict)
        ),
    )


def validate_policy_snapshot(snapshot: object) -> None:
    if not isinstance(snapshot, dict):
        raise ValueError("Fleet policy snapshot must be an object")
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Fleet policy snapshot has an unsupported schema")
    if snapshot.get("activity") not in {"coding", "research"}:
        raise ValueError("Fleet policy snapshot has an invalid activity")
    requested_provider_mode = str(snapshot.get("requested_provider_mode") or "auto")
    provider_mode = str(snapshot.get("provider_mode") or "balanced")
    if requested_provider_mode not in REQUESTED_PROVIDER_MODES:
        raise ValueError("Fleet policy snapshot has an invalid requested_provider_mode")
    if provider_mode not in SELECTED_PROVIDER_MODES:
        raise ValueError("Fleet policy snapshot has an invalid provider_mode")
    canonical_request = (
        "balanced" if requested_provider_mode == "mixed" else requested_provider_mode
    )
    if (
        provider_mode != "adaptive"
        and canonical_request != "auto"
        and canonical_request != provider_mode
    ):
        raise ValueError("Fleet policy requested and selected provider modes conflict")
    session_mode = str(snapshot.get("session_mode") or "per_leg")
    if session_mode not in SESSION_MODES:
        raise ValueError("Fleet policy snapshot has an invalid session_mode")
    minimum = _positive_int(snapshot.get("minimum_workers_per_phase"), "minimum_workers_per_phase")
    if snapshot.get("no_silent_fallback") is not True:
        raise ValueError("Fleet policy cannot weaken worker minimums or model identity")
    capacity_handoff = str(snapshot.get("capacity_handoff") or "automatic").lower()
    if capacity_handoff not in CAPACITY_HANDOFF_MODES:
        raise ValueError("Fleet policy has an invalid capacity_handoff mode")
    handoffs = snapshot.get("handoffs") or []
    if not isinstance(handoffs, list) or any(not isinstance(item, dict) for item in handoffs):
        raise ValueError("Fleet policy handoffs must be a list of objects")
    if provider_mode == "adaptive" and not handoffs:
        raise ValueError("adaptive Fleet routing requires a recorded provider handoff")
    parallel = _positive_int(snapshot.get("max_parallel_workers"), "max_parallel_workers")
    if parallel > MAX_WORKERS:
        raise ValueError(f"Fleet policy cannot exceed {MAX_WORKERS} parallel workers")
    _positive_int(snapshot.get("max_parallel_writers", 2), "max_parallel_writers")
    raw_workstreams = snapshot.get("workstreams")
    workstream_ids: set[str] = set()
    if raw_workstreams is not None:
        if not isinstance(raw_workstreams, list):
            raise ValueError("Fleet policy workstreams must be a list")
        for item in raw_workstreams:
            if not isinstance(item, dict):
                raise ValueError("Fleet workstreams must be objects")
            identifier = str(item.get("id") or "").strip()
            if not identifier or identifier in workstream_ids:
                raise ValueError("Fleet workstream ids must be present and unique")
            if not str(item.get("title") or "").strip():
                raise ValueError("Fleet workstream title is required")
            if not str(item.get("description") or "").strip():
                raise ValueError("Fleet workstream description is required")
            workstream_ids.add(identifier)
    raw_work_units = snapshot.get("work_units")
    if raw_work_units is not None and raw_workstreams is None:
        raise ValueError("Fleet work units require persisted workstreams")
    raw_scaling = snapshot.get("scaling")
    selected_workers: int | None = None
    if raw_scaling is not None:
        if not isinstance(raw_scaling, dict):
            raise ValueError("Fleet policy scaling metadata must be an object")
        selected_workers = _positive_int(raw_scaling.get("selected_workers"), "selected_workers")
        if selected_workers > MAX_WORKERS:
            raise ValueError("Fleet policy selects between one and four workers")
        if selected_workers > parallel:
            raise ValueError("Fleet policy selects more workers than configured capacity")
        requested_workers = _optional_worker_count(
            raw_scaling.get("requested_workers"),
            "requested_workers",
        )
        if requested_workers is not None and requested_workers != selected_workers:
            raise ValueError("Fleet policy requested_workers must match selected_workers")
        detected = int(raw_scaling.get("detected_workstreams") or 0)
        if detected < 0 or not str(raw_scaling.get("reason") or "").strip():
            raise ValueError("Fleet policy scaling metadata is incomplete")
    phases = snapshot.get("phases")
    if not isinstance(phases, list) or [
        item.get("name") for item in phases if isinstance(item, dict)
    ] != list(PHASES):
        raise ValueError("Fleet policy snapshot must contain the four ordered phases")
    phase_worker_count: int | None = None
    for expected_index, phase in enumerate(phases):
        if not isinstance(phase, dict) or phase.get("index") != expected_index:
            raise ValueError("Fleet phase indices must be ordered")
        if phase.get("execution") not in {"parallel", "sequential"}:
            raise ValueError("Fleet phase execution must be parallel or sequential")
        display_name = phase.get("display_name")
        if display_name is not None and not str(display_name).strip():
            raise ValueError("Fleet phase display_name cannot be blank")
        workers = phase.get("workers")
        if not isinstance(workers, list) or len(workers) < minimum:
            raise ValueError(f"Fleet phase {phase.get('name')} has fewer than {minimum} workers")
        if len(workers) > MAX_WORKERS:
            raise ValueError(f"Fleet phase {phase.get('name')} exceeds the four-worker cap")
        if len(workers) > parallel:
            raise ValueError(f"Fleet phase {phase.get('name')} exceeds configured capacity")
        if phase_worker_count is None:
            phase_worker_count = len(workers)
        elif len(workers) != phase_worker_count:
            raise ValueError("Fleet phases must retain the same durable worker count")
        worker_keys: set[str] = set()
        provider_counts = {"codex": 0, "claude": 0}
        for worker in workers:
            _validate_snapshot_worker(worker)
            provider = str(worker.get("provider") or worker.get("runtime") or "").lower()
            provider_counts[provider] += 1
            worker_key = str(worker.get("worker_key") or "").strip()
            if worker_key:
                if worker_key in worker_keys:
                    raise ValueError("Fleet worker keys must be unique within a phase")
                worker_keys.add(worker_key)
            assignment_ids = {str(value) for value in (worker.get("assignment_ids") or ())}
            review_ids = {str(value) for value in (worker.get("review_target_ids") or ())}
            if workstream_ids and not (assignment_ids | review_ids).issubset(workstream_ids):
                raise ValueError("Fleet worker assignments reference an unknown workstream")
            if assignment_ids & review_ids:
                raise ValueError("Fleet workers cannot review their own assignment")
        # A phase is single-provider when it is built, because every agent runs
        # the phase's one locked model. It stops being single-provider the
        # moment one worker is handed to the other provider for capacity, which
        # is legitimate, so there is no phase-level provider rule to enforce
        # here. policy_models_match_contract is what holds the line: it accepts
        # only the phase's pipeline model or that provider's escape-hatch model.
        if provider_mode in PROVIDERS and provider_counts[provider_mode] != len(workers):
            raise ValueError(f"{provider_mode}-only Fleet phases cannot include another provider")
    if selected_workers is not None and phase_worker_count != selected_workers:
        raise ValueError("Fleet scaling metadata does not match its durable worker count")
    if raw_work_units is not None:
        validate_work_unit_contracts(
            raw_work_units,
            workstreams=raw_workstreams,
            workers=phases[0]["workers"],
        )


def expected_model_matches(provider: str, requested: str, actual: str | None) -> bool:
    """Strict family check used after a provider reports the model it ran."""

    if not actual:
        return False
    requested_clean = requested.casefold().strip()
    actual_clean = actual.casefold().strip()
    if provider == "codex":
        return actual_clean == requested_clean
    if requested_clean in {"opus", "claude-opus-5"}:
        return actual_clean == "claude-opus-5" or actual_clean.startswith("claude-opus-5-")
    if requested_clean.startswith("claude-opus-"):
        return actual_clean == requested_clean or actual_clean.startswith(requested_clean + "-")
    if requested_clean.startswith("claude-"):
        return actual_clean == requested_clean or actual_clean.startswith(requested_clean + "-")
    aliases = {"sonnet": "sonnet", "haiku": "haiku", "fable": "fable"}
    needle = aliases.get(requested_clean)
    return needle in actual_clean if needle else actual_clean == requested_clean


def policy_models_match_contract(
    activity: str, snapshot: object, *, baseline: object = None
) -> bool:
    """Return whether every saved worker still follows Fleet's hard phase matrix.

    ``baseline`` is the run's own frozen policy, and passing it says "this run
    started under an earlier matrix". A run freezes its contract at creation so
    it stays reproducible, but the check re-read the live matrix, so editing the
    matrix invalidated every run already in flight: their next handoff or retry
    raised "provider handoff violated Fleet's phase model contract" for a
    contract Fleet itself had issued. A worker is legal when it matches the
    current matrix, the current escape-hatch stack, or the spec this very run
    was created with. Inventing a model is still refused.
    """

    if activity not in PHASE_MODEL_POLICY or not isinstance(snapshot, dict):
        return False
    raw_phases = snapshot.get("phases")
    if not isinstance(raw_phases, list) or len(raw_phases) != len(PHASES):
        return False
    phases = {
        str(phase.get("name") or ""): phase
        for phase in raw_phases
        if isinstance(phase, dict)
    }
    if set(phases) != set(PHASES):
        return False
    for phase_name in PHASES:
        # Two specs are legal per phase: the pipeline's, and the provider-only
        # stack a capacity handoff falls back to. Nothing else.
        expected = {
            provider: (model, effort)
            for provider, model, effort in PHASE_MODEL_POLICY[activity][phase_name]
        }
        # A provider can legitimately carry more than one spec for a phase: the
        # pipeline's and the escape-hatch stack's. Keyed by provider alone, the
        # second one silently replaced the first.
        allowed: dict[str, set[tuple[str, str]]] = {
            provider: {(model, effort)} for provider, (model, effort) in expected.items()
        }
        for stacks in PROVIDER_ONLY_POLICY.values():
            fallback_provider, model, effort = stacks[activity][phase_name][0]
            allowed.setdefault(fallback_provider, set()).add((model, effort))
        for spec in _baseline_specs(baseline, phase_name):
            allowed.setdefault(spec[0], set()).add((spec[1], spec[2]))
        workers = phases[phase_name].get("workers")
        if not isinstance(workers, list) or not workers:
            return False
        for worker in workers:
            if not isinstance(worker, dict):
                return False
            provider = str(worker.get("provider") or worker.get("runtime") or "").lower()
            actual = (
                str(worker.get("model") or ""),
                str(worker.get("effort") or "").lower(),
            )
            if actual not in allowed.get(provider, set()):
                return False
    return True


def _baseline_specs(baseline: object, phase_name: str) -> list[tuple[str, str, str]]:
    """Every (provider, model, effort) the run was actually created with."""

    if not isinstance(baseline, dict):
        return []
    specs: list[tuple[str, str, str]] = []
    for phase in baseline.get("phases") or []:
        if not isinstance(phase, dict) or str(phase.get("name") or "") != phase_name:
            continue
        for worker in phase.get("workers") or []:
            if not isinstance(worker, dict):
                continue
            provider = str(worker.get("provider") or worker.get("runtime") or "").lower()
            model = str(worker.get("model") or "")
            effort = str(worker.get("effort") or "").lower()
            if provider and model and effort:
                specs.append((provider, model, effort))
    return specs


def build_provider_handoff_policy(
    snapshot: dict[str, Any],
    *,
    phase_index: int,
    ordinal: int,
    target_provider: str,
    reason: str,
    automatic: bool = False,
    requested_at: float | None = None,
) -> dict[str, Any]:
    """Move one logical worker slot to the other native provider.

    Native Claude and Codex session ids cannot cross runtimes. The worker key,
    assignment, checkout and evidence lineage stay stable while every
    unfinished phase receives the target provider's locked phase model.
    """

    validate_policy_snapshot(snapshot)
    target = str(target_provider or "").strip().lower()
    if target not in PROVIDERS:
        raise ValueError("handoff provider must be claude or codex")
    try:
        start_index = int(phase_index)
        worker_ordinal = int(ordinal)
    except (TypeError, ValueError) as exc:
        raise ValueError("handoff phase and worker ordinal must be integers") from exc
    phases = snapshot.get("phases") or []
    if start_index < 0 or start_index >= len(phases):
        raise ValueError("handoff phase is outside the Fleet plan")
    workers = phases[start_index].get("workers") if isinstance(phases[start_index], dict) else None
    if not isinstance(workers, list) or worker_ordinal < 0 or worker_ordinal >= len(workers):
        raise ValueError("handoff worker is outside the Fleet plan")
    source_worker = workers[worker_ordinal]
    source = str(source_worker.get("provider") or source_worker.get("runtime") or "").lower()
    if source not in PROVIDERS:
        raise ValueError("handoff source provider is invalid")
    if source == target:
        raise ValueError(f"worker already uses {target}")

    replacement = deepcopy(snapshot)
    source_label = str(source_worker.get("worker_label") or source.title())
    worker_key = str(source_worker.get("worker_key") or f"slot:{worker_ordinal + 1}")
    # The agent keeps its name through a handoff. Which provider is carrying it
    # lives in runtime and handoff_from_provider, not in the worker's identity.
    target_label = source_label
    activity = str(replacement["activity"])
    for index in range(start_index, len(replacement["phases"])):
        phase = replacement["phases"][index]
        worker = phase["workers"][worker_ordinal]
        phase_name = str(phase["name"])
        # The pipeline pins one provider per phase, so a handoff to the other
        # one takes that provider's escape-hatch model for this phase.
        model_entry = next(
            (
                entry
                for entry in PHASE_MODEL_POLICY[activity][phase_name]
                if entry[0] == target
            ),
            PROVIDER_ONLY_POLICY[target][activity][phase_name][0],
        )
        worker["provider"] = target
        worker["runtime"] = target
        worker["model"] = model_entry[1]
        worker["effort"] = model_entry[2]
        worker["role"] = _role(activity, phase_name, worker_ordinal, target)
        worker["worker_key"] = worker_key
        worker["worker_label"] = target_label
        worker["handoff_from_provider"] = source
        worker["handoff_source_label"] = source_label

    replacement["provider_mode"] = "adaptive"
    replacement["capacity_handoff"] = str(
        replacement.get("capacity_handoff") or "automatic"
    ).lower()
    history = replacement.setdefault("handoffs", [])
    history.append(
        {
            "phase_index": start_index,
            "phase": str(replacement["phases"][start_index]["name"]),
            "ordinal": worker_ordinal,
            "worker_key": worker_key,
            "from_provider": source,
            "to_provider": target,
            "reason": str(reason or "provider handoff").strip()[:1_000],
            "automatic": bool(automatic),
            "requested_at": float(requested_at if requested_at is not None else time.time()),
        }
    )
    validate_policy_snapshot(replacement)
    if not policy_models_match_contract(activity, replacement, baseline=snapshot):
        expected = PHASE_MODEL_POLICY[activity]
        raise ValueError(
            "provider handoff violated Fleet's phase model contract: "
            f"{target} cannot serve this run's phases. Current matrix is "
            + ", ".join(
                f"{name}={expected[name][0][1]}/{expected[name][0][2]}" for name in PHASES
            )
        )
    return replacement


def _validate_mcp_read_access(value: object) -> None:
    """Reject a malformed read-access block instead of silently ignoring it.

    A typo here would otherwise read as "no servers configured", which fails
    closed but does it quietly. Fleet says so instead.
    """

    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ValueError("mcp_read_access must be an object")
    if "enabled" in value and not isinstance(value["enabled"], bool):
        raise ValueError("mcp_read_access.enabled must be a boolean")
    servers = value.get("servers")
    if servers is None:
        return
    if not isinstance(servers, list) or any(not str(item).strip() for item in servers):
        raise ValueError("mcp_read_access.servers must be a list of server names")


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _validate_worker(worker: object, activity: str) -> None:
    if not isinstance(worker, dict):
        raise ValueError(f"{activity} workers must be objects")
    provider = str(worker.get("provider") or "").lower()
    model = str(worker.get("model") or "").strip()
    effort = str(worker.get("effort") or "").lower()
    if provider not in PROVIDERS or not model or effort not in EFFORTS:
        raise ValueError(f"invalid worker in {activity} profile")


def _configured_worker_specs(
    workers: list[dict[str, Any]],
) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (
            str(worker.get("provider") or "").lower(),
            str(worker.get("model") or "").strip(),
            str(worker.get("effort") or "").lower(),
        )
        for worker in workers
    )


def _validate_snapshot_worker(worker: object) -> None:
    if not isinstance(worker, dict):
        raise ValueError("Fleet workers must be objects")
    provider = str(worker.get("provider") or worker.get("runtime") or "").lower()
    if provider not in PROVIDERS:
        raise ValueError("Fleet worker provider must be claude or codex")
    if not str(worker.get("model") or "").strip():
        raise ValueError("Fleet worker model is required")
    if str(worker.get("effort") or "").lower() not in EFFORTS:
        raise ValueError("Fleet worker effort is invalid")
    if not str(worker.get("role") or "").strip():
        raise ValueError("Fleet worker role is required")
    if worker.get("access_mode") not in {"read_only", "write", "review", "verify"}:
        raise ValueError("Fleet worker access_mode is invalid")
    for key in ("assignment_ids", "review_target_ids"):
        value = worker.get(key)
        if value is not None and (
            not isinstance(value, list) or any(not str(item).strip() for item in value)
        ):
            raise ValueError(f"Fleet worker {key} must be a list of ids")


def resolve_research_depth(activity: str, task: str) -> str:
    """Require extensive current online research from every Research worker."""

    del activity, task
    return "full"


def _access_mode(activity: str, phase: str, ordinal: int) -> str:
    if activity == "research":
        return "review" if phase == "verify" else "read_only"
    if phase in {"execute", "finalize"}:
        return "write"
    if phase == "verify":
        return "review"
    return "read_only"


def _role(activity: str, phase: str, ordinal: int, provider: str) -> str:
    roles = {
        "coding": {
            "discover": (
                "codebase-researcher",
                "architecture-risk-researcher",
                "dependency-researcher",
                "integration-researcher",
            ),
            "execute": (
                "core-co-implementer",
                "integration-co-implementer",
                "feature-co-implementer",
                "compatibility-co-implementer",
            ),
            "verify": (
                "correctness-reviewer",
                "regression-reviewer",
                "integration-reviewer",
                "validation-reviewer",
            ),
            "finalize": (
                "functional-fixer",
                "hardening-fixer",
                "integration-fixer",
                "regression-fixer",
            ),
        },
        "research": {
            "discover": (
                "research-scout",
                "question-scout",
                "source-scout",
                "synthesis-risk-scout",
            ),
            "execute": (
                "source-analyst",
                "independent-analyst",
                "comparative-analyst",
                "integration-analyst",
            ),
            "verify": (
                "claim-reviewer",
                "source-reviewer",
                "contradiction-reviewer",
                "coverage-reviewer",
            ),
            "finalize": (
                "report-refiner",
                "evidence-refiner",
                "uncertainty-refiner",
                "synthesis-refiner",
            ),
        },
    }
    available = roles[activity][phase]
    if ordinal < len(available):
        return available[ordinal]
    return f"{provider}-{phase}-{ordinal + 1}"


def _display_name(activity: str, phase: str) -> str:
    labels = {
        "coding": {
            "discover": "Research",
            "execute": "Code",
            "verify": "Review",
            "finalize": "Fix",
        },
        "research": {
            "discover": "Research",
            "execute": "Analyze",
            "verify": "Review",
            "finalize": "Refine",
        },
    }
    return labels[activity][phase]
