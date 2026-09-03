"""Machine-enforced completion contracts for Serena Fleet legs.

A work-unit contract is only real if something refuses to accept a worker's
word for it. Fleet already persists outcomes, acceptance criteria, required
evidence, constraints, and stop conditions per work unit, and already prints
them into the worker prompt. Until this module existed, nothing read the
answer back: an attempt became `completed` because the provider CLI exited
zero, which is a statement about a process, not about the work.

This module turns the final worker message into a structured verdict. It is
deliberately pure: no database, no git, no provider. Callers supply the
persisted contract, the durable path claims, and the real changed paths, and
get back an accept/reject decision with concrete, quotable failures.

The enforcement coupling is intentional and narrow: Fleet only gates a leg on
a contract it actually handed that worker. A run with no work-unit contracts
is not silently passed, it is reported as unenforced, so the difference stays
visible instead of looking like success.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import urlsplit

EVIDENCE_SCHEMA_VERSION = 1
EVIDENCE_OPEN = "<serena-evidence>"
EVIDENCE_CLOSE = "</serena-evidence>"

# One block, non-greedy, so trailing prose after the envelope stays readable.
_ENVELOPE = re.compile(
    re.escape(EVIDENCE_OPEN) + r"(.*?)" + re.escape(EVIDENCE_CLOSE),
    re.DOTALL | re.IGNORECASE,
)
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*|\s*```\s*$")

UNIT_STATUSES = frozenset({"completed", "blocked", "stopped"})
TERMINAL_BLOCKED_STATUSES = frozenset({"blocked", "stopped"})

# Review used to hand the next phase prose. Prose cannot be routed to an owner
# or counted, so Fix ran for every worker whether or not anything was wrong
# with its surface. Findings are structured now: an explicit empty list is a
# real, checkable statement that this reviewer found nothing.
FINDING_SEVERITIES = frozenset({"blocker", "major", "minor"})

# A recorded "test" has to look like something that can actually fail. This is
# the same vocabulary the single-job coding contract uses, kept local so an
# edit there cannot silently loosen the Fleet gate.
_REAL_TEST = re.compile(
    r"(?:^|[\s'\"])(?:[^\s'\"]*/)?(?:pytest|"
    # Every `python -m` form the gate will re-run, plus a worker's own script
    # when its name says it verifies something. Without these a worker could
    # satisfy the re-run and still be told nothing looked like a real test.
    r"python[0-9.]*\s+-m\s+(?:pytest|unittest|doctest|json\.tool|py_compile|ruff)|"
    r"python[0-9.]*\s+[^\s'\"]*(?:test|check|verify|preflight)[^\s'\"]*\.py|"
    r"npm\s+(?:run\s+)?test|"
    r"pnpm\s+(?:run\s+)?test|yarn\s+test|bun\s+test|cargo\s+test|"
    r"go\s+test|swift\s+test|gradle\w*\s+.*test|node\s+--test|"
    r"node(?:\s+--[a-z0-9-]+(?:=[^\s'\"]+)?)\s+[^\s'\"]*(?:test|spec)[^\s'\"]*\.(?:[cm]?js|tsx?)|"
    r"node\s+(?:--[a-z0-9-]+(?:=[^\s'\"]+)?\s+)*-(?:e|-eval|eval)\s+.*\bassert\b|"
    # git diff --check with any refs, not only the bare form. It is the most
    # declared verification in the fleet and was not counted as one.
    r"git\s+(?:-C\s+\S+\s+)?diff\s+(?:[^\s'\"]+\s+)*--check\b|"
    r"git\s+diff\s+--no-index\s+--check\b|"
    r"git\s+(?:-C\s+\S+\s+)?merge-base\s+--is-ancestor\b|"
    # A syntax check is a check. node --check was declared ~96 times and never
    # counted, because the pattern demanded "test" in the filename.
    r"node\s+--check\b|"
    # An assertion, not a browse: the quiet/silent forms exit non-zero on a
    # mismatch, which is what makes them a check.
    r"(?:^|[\s'\"])(?:grep|rg|cmp|diff)\s+(?:-[A-Za-z]*[qs]|--quiet|--silent)|"
    r"ruff\s+check|mypy|pyright|tsc\b|eslint\b|vitest\b|jest\b|biome\s+check)",
    re.IGNORECASE,
)

MIN_FINAL_RESPONSE_CHARS = 40
MAX_FAILURE_CHARS = 400
MIN_RESEARCH_SEARCHES = 3
MIN_RESEARCH_SOURCES = 5
MIN_RESEARCH_DOMAINS = 3
MIN_AUTHORITATIVE_SOURCES = 2
MAX_RESEARCH_ACCESS_AGE_DAYS = 7

# Every Research worker must perform substantial current online research. The
# fallback remains full so an old or malformed snapshot cannot weaken the gate.
RESEARCH_DEPTH_THRESHOLDS = {
    "full": {
        "searches": MIN_RESEARCH_SEARCHES,
        "sources": MIN_RESEARCH_SOURCES,
        "domains": MIN_RESEARCH_DOMAINS,
        "authoritative": MIN_AUTHORITATIVE_SOURCES,
        "best_practices": 2,
    },
}
DEFAULT_RESEARCH_DEPTH = "full"
RESEARCH_SOURCE_TYPES = frozenset(
    {
        "official",
        "primary",
        "standard",
        "research",
        "reputable_secondary",
        "community",
    }
)
AUTHORITATIVE_SOURCE_TYPES = frozenset(
    {"official", "primary", "standard", "research"}
)


@dataclass(frozen=True, slots=True)
class UnitVerdict:
    unit_id: str
    claimed_status: str
    accepted: bool
    failures: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    stop_condition: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "claimed_status": self.claimed_status,
            "accepted": self.accepted,
            "failures": list(self.failures),
            "changed_paths": list(self.changed_paths),
            "stop_condition": self.stop_condition,
        }


@dataclass(frozen=True, slots=True)
class CompletionVerdict:
    """Whether a leg may be recorded as completed, and exactly why not."""

    accepted: bool
    enforced: bool
    reason: str
    envelope_present: bool
    failures: tuple[str, ...] = ()
    units: tuple[UnitVerdict, ...] = ()
    schema_version: int = EVIDENCE_SCHEMA_VERSION
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal_stop(self) -> bool:
        """True when honest evidence says the assigned work did not finish."""

        return self.enforced and self.accepted and any(
            unit.claimed_status in {"blocked", "stopped"} for unit in self.units
        )

    @property
    def completion_allowed(self) -> bool:
        return not self.enforced or (self.accepted and not self.terminal_stop)

    @property
    def blocked(self) -> bool:
        return self.enforced and not self.completion_allowed

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "accepted": self.accepted,
            "completion_allowed": self.completion_allowed,
            "terminal_stop": self.terminal_stop,
            "enforced": self.enforced,
            "reason": self.reason,
            "envelope_present": self.envelope_present,
            "failures": list(self.failures),
            "units": [unit.to_dict() for unit in self.units],
            "evidence": self.evidence,
        }

    def summary(self) -> str:
        if not self.enforced:
            return self.reason
        if self.terminal_stop:
            stopped = [
                f"{unit.unit_id} {unit.claimed_status}: {unit.stop_condition}"
                for unit in self.units
                if unit.claimed_status in {"blocked", "stopped"}
            ]
            detail = "; ".join(stopped[:6])
            return f"work stopped before completion: {detail}" if detail else self.reason
        if self.accepted:
            return "completion evidence satisfied the work-unit contract"
        listed = "; ".join(self.failures[:6])
        return f"completion evidence rejected: {listed}" if listed else self.reason


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _text_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [_clean(item) for item in value if _clean(item)]


def _normalise_path(value: object) -> str:
    raw = str(value or "").strip().replace("\\", "/").strip("/")
    while raw.startswith("./"):
        raw = raw[2:]
    return raw


def _criterion_key(value: object) -> str:
    """Stable identity for acceptance criteria without weakening their text."""

    return _clean(value).casefold()


def extract_envelope(output_text: str) -> tuple[dict[str, Any] | None, str, str]:
    """Split a worker message into its evidence envelope and its prose.

    Returns (payload, prose, error). A payload of None with an empty error
    means the worker never emitted an envelope at all, which is a different
    failure from emitting one that does not parse.
    """

    raw = str(output_text or "")
    matches = list(_ENVELOPE.finditer(raw))
    if not matches:
        return None, raw.strip(), ""
    if len(matches) > 1:
        return None, raw.strip(), "more than one completion evidence envelope was emitted"
    match = matches[0]
    prose = (raw[: match.start()] + raw[match.end() :]).strip()
    body = _FENCE.sub("", match.group(1).strip()).strip()
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None, prose, "the completion evidence envelope was not valid JSON"
    if not isinstance(payload, dict):
        return None, prose, "the completion evidence envelope was not a JSON object"
    return payload, prose, ""


def evaluate_completion(
    *,
    output_text: str,
    units: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    assignment_ids: list[str] | tuple[str, ...],
    access_mode: str = "read",
    activity: str = "coding",
    phase: str = "",
    claimed_paths: list[str] | None = None,
    observed_changed_paths: list[str] | None = None,
    observed_test_results: dict[str, int] | None = None,
    observed_research_activity: dict[str, int] | None = None,
    dependency_states: dict[str, str] | None = None,
    research_depth: str = DEFAULT_RESEARCH_DEPTH,
) -> CompletionVerdict:
    """Decide whether one finished leg may be recorded as completed."""

    owned = [str(item).strip() for item in assignment_ids if str(item).strip()]
    contracts = {
        str(unit.get("id") or "").strip(): unit
        for unit in (units or ())
        if isinstance(unit, dict) and str(unit.get("id") or "").strip()
    }
    scoped = {unit_id: contracts[unit_id] for unit_id in owned if unit_id in contracts}
    if not scoped:
        # No contract was handed to this worker, so there is nothing this
        # module is entitled to enforce. Say so rather than implying a pass.
        return CompletionVerdict(
            accepted=True,
            enforced=False,
            reason="no work-unit contract was assigned to this leg",
            envelope_present=False,
        )

    payload, prose, parse_error = extract_envelope(output_text)
    failures: list[str] = []
    if parse_error:
        failures.append(parse_error)
    elif payload is None:
        failures.append(
            "the final message contained no <serena-evidence> completion envelope"
        )
    if failures:
        return CompletionVerdict(
            accepted=False,
            enforced=True,
            reason="completion evidence is missing or unreadable",
            envelope_present=payload is not None,
            failures=tuple(failures),
        )
    assert payload is not None

    if len(prose) < MIN_FINAL_RESPONSE_CHARS:
        failures.append(
            "the final response obligation was not met: the message carried an "
            "evidence envelope but no readable answer for the next phase"
        )

    raw_units = payload.get("units")
    if not isinstance(raw_units, list) or not raw_units:
        failures.append("the completion envelope declared no work units")
        return CompletionVerdict(
            accepted=False,
            enforced=True,
            reason="completion evidence is structurally invalid",
            envelope_present=True,
            failures=tuple(failures),
            evidence=_bounded(payload),
        )

    reported: dict[str, dict[str, Any]] = {}
    for entry in raw_units:
        if not isinstance(entry, dict):
            failures.append("a completion envelope unit entry was not an object")
            continue
        unit_id = str(entry.get("id") or "").strip()
        if not unit_id:
            failures.append("a completion envelope unit entry had no id")
            continue
        if unit_id in reported:
            failures.append(f"work unit {unit_id} was reported more than once")
            continue
        reported[unit_id] = entry

    missing = [unit_id for unit_id in scoped if unit_id not in reported]
    if missing:
        failures.append(
            "no completion evidence was reported for owned work unit(s): "
            + ", ".join(sorted(missing))
        )
    unknown = [unit_id for unit_id in reported if unit_id not in scoped]
    if unknown:
        failures.append(
            "completion evidence was reported for work unit(s) this leg does not own: "
            + ", ".join(sorted(unknown))
        )

    claim_set = {_normalise_path(path) for path in (claimed_paths or []) if _normalise_path(path)}
    observed = {
        _normalise_path(path)
        for path in (observed_changed_paths or [])
        if _normalise_path(path)
    }
    observed_supplied = observed_changed_paths is not None

    verdicts: list[UnitVerdict] = []
    declared_total: set[str] = set()
    for unit_id, contract in scoped.items():
        entry = reported.get(unit_id)
        if entry is None:
            verdicts.append(
                UnitVerdict(unit_id, "", False, ("no evidence was reported",))
            )
            continue
        verdict = _evaluate_unit(
            unit_id=unit_id,
            contract=contract,
            entry=entry,
            access_mode=access_mode,
            activity=activity,
            phase=phase,
            claim_set=claim_set,
            claims_supplied=claimed_paths is not None,
            observed_test_results=observed_test_results,
            observed_research_activity=observed_research_activity,
            dependency_states=dependency_states or {},
            research_depth=research_depth,
        )
        verdicts.append(verdict)
        declared_total.update(verdict.changed_paths)

    # A write leg that quietly changed files it never declared is the exact
    # failure this gate exists for, and it can only be caught against a real
    # diff. Undeclared changes are attributed to the leg, not to a unit.
    if observed_supplied and access_mode == "write":
        undeclared = sorted(observed - declared_total)
        if undeclared:
            failures.append(
                "the working tree changed paths this leg never declared in its "
                "evidence: " + ", ".join(undeclared[:10])
            )

    unit_failures = [
        f"{verdict.unit_id}: {item}"
        for verdict in verdicts
        for item in verdict.failures
    ]
    failures.extend(unit_failures)
    accepted = not failures
    return CompletionVerdict(
        accepted=accepted,
        enforced=True,
        reason=(
            "completion evidence satisfied the work-unit contract"
            if accepted
            else "completion evidence did not satisfy the work-unit contract"
        ),
        envelope_present=True,
        failures=tuple(_clean(item)[:MAX_FAILURE_CHARS] for item in failures),
        units=tuple(verdicts),
        evidence=_bounded(payload),
    )


def _evaluate_unit(
    *,
    unit_id: str,
    contract: dict[str, Any],
    entry: dict[str, Any],
    access_mode: str,
    activity: str,
    phase: str,
    claim_set: set[str],
    claims_supplied: bool,
    observed_test_results: dict[str, int] | None,
    observed_research_activity: dict[str, int] | None,
    dependency_states: dict[str, str],
    research_depth: str = DEFAULT_RESEARCH_DEPTH,
) -> UnitVerdict:
    failures: list[str] = []
    status = _clean(entry.get("status")).lower()
    if status not in UNIT_STATUSES:
        failures.append(
            "status must be one of completed, blocked, or stopped, not "
            + (status or "(missing)")
        )
    stop_condition = _clean(entry.get("stop_condition"))
    completion = contract.get("completion_contract")
    completion = completion if isinstance(completion, dict) else {}

    if phase == "verify" and status == "completed":
        failures.extend(_review_findings_failures(entry.get("findings")))

    if phase == "discover" and status == "completed":
        failures.extend(
            _research_evidence_failures(
                entry.get("online_research"),
                observed_activity=observed_research_activity,
                depth=research_depth,
            )
        )

    if status == "completed" and stop_condition:
        failures.append(
            "contradictory evidence: the unit is reported completed while also "
            f"reporting a triggered stop condition ({stop_condition[:200]})"
        )
    if status in TERMINAL_BLOCKED_STATUSES and not stop_condition:
        failures.append(
            f"a unit reported {status} must name the stop condition that triggered it"
        )

    required_criteria = _text_list(completion.get("acceptance_criteria"))
    acceptance = entry.get("acceptance")
    if status == "completed":
        if not isinstance(acceptance, list) or not acceptance:
            failures.append(
                "a completed unit must answer every acceptance criterion in its contract"
            )
        else:
            answered: list[dict[str, Any]] = [
                item for item in acceptance if isinstance(item, dict)
            ]
            invalid_entries = len(acceptance) - len(answered)
            if invalid_entries:
                failures.append(
                    f"{invalid_entries} acceptance criterion entr"
                    + ("y was" if invalid_entries == 1 else "ies were")
                    + " not an object"
                )
            required_counts = Counter(_criterion_key(item) for item in required_criteria)
            answered_counts = Counter(
                _criterion_key(item.get("criterion"))
                for item in answered
                if _criterion_key(item.get("criterion"))
            )
            if len(answered) != len(required_criteria):
                failures.append(
                    f"{len(answered)} of {len(required_criteria)} acceptance criteria "
                    "were answered"
                )
            missing_criteria = list((required_counts - answered_counts).elements())
            unknown_criteria = list((answered_counts - required_counts).elements())
            if missing_criteria:
                failures.append(
                    "contract acceptance criteria were not answered exactly: "
                    + "; ".join(missing_criteria[:6])
                )
            if unknown_criteria:
                failures.append(
                    "unknown or duplicate acceptance criteria were reported: "
                    + "; ".join(unknown_criteria[:6])
                )
            for index, item in enumerate(answered):
                met = item.get("met")
                note = _clean(item.get("evidence"))
                label = _clean(item.get("criterion")) or f"criterion {index + 1}"
                if met is not True:
                    failures.append(
                        "contradictory evidence: the unit is reported completed "
                        f"while acceptance criterion is not met ({label[:160]})"
                    )
                elif not note:
                    failures.append(
                        f"acceptance criterion was marked met with no evidence ({label[:160]})"
                    )

        if entry.get("constraints_respected") is not True:
            failures.append(
                "a completed unit must affirm that its contract constraints were respected"
            )

        required_dependencies = _text_list(contract.get("dependency_ids"))
        unavailable_dependencies = [
            dependency
            for dependency in required_dependencies
            if dependency not in dependency_states
        ]
        if unavailable_dependencies:
            failures.append(
                "dependency state was unavailable for: "
                + ", ".join(sorted(unavailable_dependencies))
            )
        blocked_dependencies = [
            dependency
            for dependency in required_dependencies
            if dependency in dependency_states
            and dependency_states[dependency] != "completed"
        ]
        if blocked_dependencies:
            failures.append(
                "contradictory evidence: the unit is reported completed while its "
                "dependencies are not: " + ", ".join(sorted(blocked_dependencies))
            )

    changed_paths = tuple(
        dict.fromkeys(
            _normalise_path(path)
            for path in _text_list(entry.get("changed_paths"))
            if _normalise_path(path)
        )
    )
    tests = [item for item in (entry.get("tests") or []) if isinstance(item, dict)]

    if access_mode != "write":
        if changed_paths:
            failures.append(
                "a read-only leg reported changed files, which its access contract "
                "forbids: " + ", ".join(changed_paths[:10])
            )
    else:
        if claims_supplied and changed_paths:
            unclaimed = sorted(
                path
                for path in changed_paths
                if not any(_covers(claim, path) for claim in claim_set)
            )
            if unclaimed:
                failures.append(
                    "files were changed without an active path claim: "
                    + ", ".join(unclaimed[:10])
                )
        if changed_paths and status == "completed":
            unresolved = _unresolved_tests(tests, observed_test_results)
            if not tests:
                failures.append(
                    "code changed with no recorded test command and exit code"
                )
            elif unresolved:
                failures.append(
                    "a recorded test command never exited clean: " + unresolved[0][:200]
                )
            elif not any(
                _REAL_TEST.search(str(item.get("command") or "")) for item in tests
            ):
                failures.append(
                    "no recorded verification command looks like a real test or "
                    "static-analysis run"
                )
            if observed_test_results is None:
                failures.append(
                    "code changed without machine-observed test results from the worker workspace"
                )
            else:
                claimed_results = {
                    _clean(item.get("command")): item.get("exit_code")
                    for item in tests
                    if _clean(item.get("command"))
                }
                latest_items = {
                    _clean(item.get("command")): item
                    for item in tests
                    if _clean(item.get("command"))
                }
                missing_observations = sorted(
                    command
                    for command in claimed_results
                    if command not in observed_test_results
                )
                if missing_observations:
                    failures.append(
                        "verification commands were not observed by Fleet: "
                        + "; ".join(missing_observations[:4])
                    )
                mismatched = sorted(
                    command
                    for command, item in latest_items.items()
                    if command
                    and "exit_code" in item
                    and command in observed_test_results
                    and item.get("exit_code") != observed_test_results[command]
                )
                if mismatched:
                    failures.append(
                        "worker-reported test exits disagreed with Fleet observations: "
                        + "; ".join(mismatched[:4])
                    )
                # 126 is Fleet declining to run a command, not the command
                # failing. Reporting it as an unclean exit sent workers to
                # debug a script that was already passing, so it says so now.
                refused = sorted(
                    command
                    for command, code in observed_test_results.items()
                    if command in claimed_results and code == 126
                )
                if refused:
                    # Naming the shapes matters more than naming the command.
                    # "declare a test Fleet can run" sent workers guessing, and
                    # a wrong guess costs a whole attempt.
                    forms = _accepted_verification_forms()
                    failures.append(
                        "Fleet refused to re-run this verification command, so it "
                        "cannot be accepted as evidence: "
                        + "; ".join(refused[:4])
                        + ". Declare one of these instead: "
                        + " | ".join(forms)
                    )
                observed_failures = sorted(
                    command
                    for command, code in observed_test_results.items()
                    if command in claimed_results and code not in {0, 126}
                )
                if observed_failures:
                    failures.append(
                        "Fleet-observed verification did not exit cleanly: "
                        + "; ".join(observed_failures[:4])
                    )

    # Per-entry schema rules apply to write legs only. A read-only leg's tests
    # are informational (its instructions say to leave them empty); rejecting a
    # research worker over the shape of optional entries failed real runs on a
    # rule the prompt never stated for its access mode.
    if access_mode == "write":
        for item in tests:
            command = _clean(item.get("command"))
            if not command:
                failures.append("a recorded verification entry had no command")
                continue
            if "exit_code" not in item and (
                observed_test_results is None or command not in observed_test_results
            ):
                failures.append(f"verification command recorded no exit code: {command[:160]}")

    return UnitVerdict(
        unit_id=unit_id,
        claimed_status=status,
        accepted=not failures,
        failures=tuple(failures),
        changed_paths=changed_paths,
        stop_condition=stop_condition,
    )


def _review_findings_failures(value: object) -> list[str]:
    """Validate a review leg's structured findings list.

    An empty list is valid and meaningful: it says this reviewer looked and
    found nothing. A missing list is not, because "no findings" and "never
    reported" must not be indistinguishable to the phase that reads them.
    """

    if not isinstance(value, list):
        return [
            "Review must report a findings list, using an empty list when no "
            "defect was found"
        ]
    failures: list[str] = []
    for index, item in enumerate(value, start=1):
        prefix = f"Review finding {index}"
        if not isinstance(item, dict):
            failures.append(f"{prefix} must be an object")
            continue
        if not _clean(item.get("unit_id")):
            failures.append(f"{prefix} must name the unit_id it was found in")
        severity = _clean(item.get("severity")).casefold()
        if severity not in FINDING_SEVERITIES:
            failures.append(
                f"{prefix} severity must be one of " + ", ".join(sorted(FINDING_SEVERITIES))
            )
        if not _clean(item.get("summary")):
            failures.append(f"{prefix} requires a summary of the defect")
        if not _clean(item.get("evidence")):
            failures.append(f"{prefix} requires evidence for the defect")
    return failures


def _research_evidence_failures(
    value: object,
    *,
    observed_activity: dict[str, int] | None,
    depth: str = DEFAULT_RESEARCH_DEPTH,
) -> list[str]:
    """Validate the mandatory online-research receipt for a Research leg."""

    thresholds = RESEARCH_DEPTH_THRESHOLDS.get(
        str(depth or "").lower(), RESEARCH_DEPTH_THRESHOLDS[DEFAULT_RESEARCH_DEPTH]
    )
    min_searches = int(thresholds["searches"])
    min_sources = int(thresholds["sources"])
    min_domains = int(thresholds["domains"])
    min_authoritative = int(thresholds["authoritative"])
    min_best_practices = int(thresholds["best_practices"])

    if not isinstance(value, dict):
        return ["Research requires an online_research evidence object"]

    failures: list[str] = []
    queries = list(
        dict.fromkeys(item.casefold() for item in _text_list(value.get("search_queries")))
    )
    if len(queries) < min_searches:
        failures.append(
            f"Research requires at least {min_searches} distinct search queries"
        )

    observed_searches = int((observed_activity or {}).get("searches") or 0)
    if observed_searches < min_searches:
        failures.append(
            "Research requires at least "
            f"{min_searches} recorded provider web searches; observed "
            f"{observed_searches}"
        )

    raw_sources = value.get("sources")
    sources = [item for item in (raw_sources or []) if isinstance(item, dict)]
    if not isinstance(raw_sources, list):
        failures.append("Research sources must be a list")
    elif len(sources) != len(raw_sources):
        failures.append("every Research source entry must be an object")

    urls: set[str] = set()
    domains: set[str] = set()
    authoritative = 0
    today = date.today()
    for index, source in enumerate(sources, start=1):
        prefix = f"Research source {index}"
        url = str(source.get("url") or "").strip()
        scheme = ""
        try:
            parsed = urlsplit(url)
            scheme = parsed.scheme.casefold()
            domain = (parsed.hostname or "").casefold()
        except (TypeError, ValueError):
            domain = ""
        if scheme not in {"http", "https"} or not domain:
            failures.append(f"{prefix} must provide a direct http(s) URL")
        elif url in urls:
            failures.append(f"{prefix} duplicates another source URL")
        else:
            urls.add(url)
            domains.add(domain.removeprefix("www."))

        for field_name in ("title", "publisher", "finding", "relevance", "currency"):
            if not _clean(source.get(field_name)):
                failures.append(f"{prefix} requires {field_name}")

        source_type = _clean(source.get("source_type")).casefold()
        if source_type not in RESEARCH_SOURCE_TYPES:
            failures.append(
                f"{prefix} source_type must be one of "
                + ", ".join(sorted(RESEARCH_SOURCE_TYPES))
            )
        elif source_type in AUTHORITATIVE_SOURCE_TYPES:
            authoritative += 1

        accessed_at = _clean(source.get("accessed_at"))
        try:
            accessed = date.fromisoformat(accessed_at)
        except ValueError:
            failures.append(f"{prefix} accessed_at must be an ISO date")
        else:
            age = (today - accessed).days
            if age < 0:
                failures.append(f"{prefix} accessed_at cannot be in the future")
            elif age > MAX_RESEARCH_ACCESS_AGE_DAYS:
                failures.append(
                    f"{prefix} was not accessed in the last "
                    f"{MAX_RESEARCH_ACCESS_AGE_DAYS} days"
                )

    if len(urls) < min_sources:
        failures.append(
            f"Research requires at least {min_sources} unique direct sources"
        )
    if len(domains) < min_domains:
        failures.append(
            f"Research requires sources from at least {min_domains} domains"
        )
    if authoritative < min_authoritative:
        # Generated from the same set the check above uses. This message used to
        # hardcode "standards", which is not a value the validator accepts, so a
        # worker fixing a rejection was told to use the one word guaranteed to
        # reject it again.
        failures.append(
            "Research requires at least "
            f"{min_authoritative} {', '.join(sorted(AUTHORITATIVE_SOURCE_TYPES))} sources"
        )
    if len(_text_list(value.get("best_practices"))) < min_best_practices:
        failures.append(
            f"Research requires at least {min_best_practices} current best-practice finding"
            + ("s" if min_best_practices != 1 else "")
        )
    if not _text_list(value.get("recent_developments")):
        failures.append("Research requires at least one recent technology or ecosystem finding")
    if not _clean(value.get("recommendation_impact")):
        failures.append("Research must state how online findings affect this work unit")
    return failures


def _covers(claim: str, path: str) -> bool:
    """True when an active claim covers a changed path, directory claims included."""

    if not claim or not path:
        return False
    return claim == "*" or path == claim or path.startswith(claim.rstrip("/") + "/")


def _unresolved_tests(
    tests: list[dict[str, Any]], observed_test_results: dict[str, int] | None = None
) -> list[str]:
    """Commands whose most recent recorded run did not exit clean.

    Failing a test, fixing the defect, and rerunning it green is the normal
    shape of real work, so the last observed result for each exact command is
    what counts. A different command exiting zero afterwards does not clear it.
    """

    last: dict[str, Any] = {}
    for item in tests:
        command = _clean(item.get("command"))
        if command:
            last[command] = (
                observed_test_results[command]
                if observed_test_results is not None
                and command in observed_test_results
                else item.get("exit_code")
            )
    return [command for command, code in last.items() if code != 0]


def _bounded(payload: dict[str, Any], limit: int = 32_000) -> dict[str, Any]:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    if len(encoded) <= limit:
        return json.loads(encoded)
    return {"truncated": True, "preview": encoded[:limit]}


def _accepted_verification_forms() -> list[str]:
    """The gate's own list of runnable command shapes.

    Imported lazily: the gate imports this module, so a module-level import
    here would be circular.
    """

    try:
        from core.fleet_completion_gate import accepted_verification_forms

        return accepted_verification_forms()
    except Exception:
        return []


def render_evidence_instructions(
    units: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    assignment_ids: list[str] | tuple[str, ...],
    *,
    access_mode: str = "read",
    phase: str = "",
) -> str:
    """The exact envelope Fleet will machine-validate, stated to the worker.

    Fleet only enforces a contract it handed over, so the prompt text and the
    validator are generated from the same place on purpose.
    """

    owned = [
        str(item).strip()
        for item in assignment_ids
        if str(item).strip()
        and any(
            isinstance(unit, dict) and str(unit.get("id") or "").strip() == str(item).strip()
            for unit in (units or ())
        )
    ]
    if not owned:
        return ""
    writes = access_mode == "write"
    unit_example = {
        "id": owned[0],
        "status": "completed",
        "acceptance": [
            {
                "criterion": "the first acceptance criterion, copied verbatim",
                "met": True,
                "evidence": "what you actually observed that proves it",
            }
        ],
        "constraints_respected": True,
        "changed_paths": ["core/example.py"] if writes else [],
        "tests": (
            [{"command": "python -m pytest tests/test_example.py -q", "exit_code": 0}]
            if writes
            else []
        ),
        "stop_condition": "",
    }
    if phase == "discover":
        # Built from the validator's own set so the example cannot drift out of
        # it. Authoritative types lead, both because the contract requires
        # MIN_AUTHORITATIVE_SOURCES of them and so a truncated example still
        # satisfies the rule it is demonstrating.
        source_types = tuple(sorted(AUTHORITATIVE_SOURCE_TYPES)) + tuple(
            sorted(RESEARCH_SOURCE_TYPES - AUTHORITATIVE_SOURCE_TYPES)
        )
        unit_example["online_research"] = {
            "search_queries": [
                "current official guidance for the assigned work unit",
                "2026 best practices and failure modes for the assigned technology",
                "recent releases alternatives and emerging approaches",
            ],
            "sources": [
                {
                    "url": f"https://source{index}.example/relevant-page",
                    "title": f"Direct source {index}",
                    "publisher": f"Publisher {index}",
                    "accessed_at": date.today().isoformat(),
                    "source_type": source_types[index - 1],
                    "currency": "current version or publication date and why it is current",
                    "finding": "the material fact this source supports",
                    "relevance": "how that fact changes this assigned work unit",
                }
                for index in range(1, MIN_RESEARCH_SOURCES + 1)
            ],
            "best_practices": [
                "current best practice one",
                "current best practice two",
            ],
            "recent_developments": [
                "recent technology, release, standard, or ecosystem change considered"
            ],
            "recommendation_impact": "what the online evidence changes in the plan or answer",
        }
    example = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "units": [unit_example],
    }
    lines = [
        "COMPLETION CONTRACT ENFORCEMENT (machine-validated, not advisory).",
        "",
        "Your final message must end with one evidence envelope. Fleet parses it "
        "and refuses to record this leg as completed if it is missing, malformed, "
        "or contradicts what actually happened. A rejected leg is visible and "
        "retryable; it is not counted as success.",
        "",
        f"Report exactly these owned unit ids, once each: {', '.join(owned)}.",
        "",
        "Rules that are enforced:",
        "- status is completed, blocked, or stopped.",
        "- completed with a non-empty stop_condition is a contradiction and is rejected.",
        "- blocked or stopped requires the stop condition that actually triggered.",
        "- completed requires every acceptance criterion answered with met true and "
        "concrete evidence, and constraints_respected true.",
        # The gate matches criterion text exactly. Workers were specialising the
        # contract's generic wording to their own unit ("the requested ads-side
        # advance" for "the requested behavior"), which reads like filling in a
        # template and cost real legs a whole attempt. Say the rule instead of
        # letting them discover it from a rejection.
        "- copy each criterion string EXACTLY as the contract above states it. "
        "Do not reword, specialise, shorten, or merge them, and answer each one "
        "exactly once. Put your unit-specific detail in evidence, not in the "
        "criterion text.",
        "- completed is rejected when a declared dependency is not itself complete.",
    ]
    if writes:
        lines += [
            "- changed_paths must list every file you changed. Undeclared changes "
            "found in the working tree reject the leg.",
            "- changed files must be covered by an active path claim.",
            "- stay on the reserved Serena Fleet branch for normal local integration. "
            "If the task requires stacking on an unmerged dependency PR, create a "
            "dedicated branch from that dependency, commit only your own suffix, push "
            "it with an upstream, open the stacked PR, and leave the worktree clean at "
            "the pushed HEAD. In changed_paths declare the exact `git diff --name-only "
            "<dependency-head>..HEAD` result. Fleet verifies that bounded published "
            "suffix and records it without copying dependency commits into the shared tree.",
            "- changed code requires at least one real test command with its exit code, "
            "and the last run of each recorded command must exit 0.",
            "- every tests entry needs \"command\" and an integer \"exit_code\".",
            "- Fleet re-runs each recorded command verbatim from your workspace root "
            "and compares exit codes. Record only a single direct command per entry "
            "(e.g. .venv/bin/python -m pytest tests/test_x.py -q). No heredocs, "
            "pipes, ';', '&&', redirection, or cd — a command Fleet cannot re-run "
            "counts as a failed test. Allowed prefixes: `env -u NAME`, "
            "PYTHONDONTWRITEBYTECODE=1, PYTHONPATH=. .",
        ]
        # The accepted shapes used to live only inside the validator, so a
        # worker had to guess and lost an attempt for every wrong guess. They
        # are rendered from the validator's own sets here.
        lines += [
            "- Fleet will only re-run these shapes, so declare one of them:",
            *(f"    {form}" for form in _accepted_verification_forms()),
        ]
    else:
        lines += [
            "- this leg is read-only: reporting changed_paths rejects it.",
            "- tests is informational on a read-only leg: leave it as [] and "
            "describe any checks you ran inside the acceptance evidence text.",
        ]
    if phase == "discover":
        lines += [
            "- Research is online and extensive for every worker, even when the defect "
            "looks fully local. Repository inspection does not replace web research.",
            f"- perform at least {MIN_RESEARCH_SEARCHES} distinct provider web searches. "
            "Fleet checks the provider event log, so listing invented queries is rejected.",
            f"- cite at least {MIN_RESEARCH_SOURCES} unique direct http(s) sources across "
            f"at least {MIN_RESEARCH_DOMAINS} domains, each accessed within "
            f"{MAX_RESEARCH_ACCESS_AGE_DAYS} days.",
            f"- source_type must be exactly one of: "
            f"{', '.join(sorted(RESEARCH_SOURCE_TYPES))}. Anything else is rejected.",
            f"- at least {MIN_AUTHORITATIVE_SOURCES} sources must be "
            f"{', '.join(sorted(AUTHORITATIVE_SOURCE_TYPES))}. Every source needs "
            "title, publisher, accessed_at, source_type, currency, finding, and "
            "relevance.",
            "- report at least two current best practices, at least one recent "
            "technology or ecosystem development, and how the research changes your work.",
        ]
    lines += [
        "- keep your normal written answer for the next phase outside the envelope. "
        "An envelope with no readable answer is rejected.",
        "",
        "Format:",
        EVIDENCE_OPEN,
        json.dumps(example, indent=2),
        EVIDENCE_CLOSE,
    ]
    return "\n".join(lines)
