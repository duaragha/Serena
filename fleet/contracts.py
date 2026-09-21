"""Durable, provider-neutral work-unit contracts for Serena Fleet.

The contract layer is intentionally smaller than a general Kanban system. It
turns Fleet's existing explicit workstreams into stable ownership,
dependencies, review responsibility, and completion evidence without changing
Serena's native Claude/Codex execution model.
"""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any

WORK_UNIT_SCHEMA_VERSION = 1
FILE_OWNERSHIP_MODES = frozenset(
    {
        "declare_before_edit",
        "repository_serialized",
        "claim_before_integration",
        "read_only",
    }
)
DEPENDENCY_MODES = frozenset({"none", "phase_barrier"})
WORK_UNIT_STATES = frozenset(
    {
        "planned",
        "pending",
        "queued",
        "in_progress",
        "running",
        "waiting_for_capacity",
        "waiting_for_resources",
        "waiting_for_input",
        "waiting_for_dependencies",
        "completed",
        "failed",
        "blocked_dependency_failed",
        "cancelled",
    }
)
_PATH_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_:/.-])"
    r"(?P<path>(?:[A-Za-z0-9_.-]+/)+(?:[A-Za-z0-9_.-]+)?|"
    r"[A-Za-z0-9_-]+\.[A-Za-z0-9][A-Za-z0-9_.-]+)"
    r"(?=$|[\s`'\"),;:#\]\}])"
)
_OWNERSHIP_CLAUSE = re.compile(
    r"\b(?:own|owns|owned|edit|edits|edited|editing|modify|modifies|modified|modifying|"
    r"change|changes|changed|changing|create|creates|created|creating|add|adds)\s+(?:only\s+)?(?P<scope>.*?)"
    r"(?=(?:\.\s+(?=[A-Z])|;|\n|$|[, ]\s*(?:(?:and|but|then)\s+)?"
    r"(?:read|preserve|ignore|inspect|verify|run|do\s+not|don't|never)\b))",
    re.IGNORECASE,
)
_NEGATED_OWNERSHIP = re.compile(
    r"(?:\bdo\s+not|\bdon't|\bnever|\bavoid|\bwithout|\bno)\s+$",
    re.IGNORECASE,
)
# Deploy-signal set for per-unit delivery scoping: the four named families
# (service, deploy, endpoint, bridge) plus tight morphological variants and
# deployment-tooling nouns. Whole-word matching only, so prose like
# "documentation" and identifiers like SERVICE_TOKEN never fire.
_DEPLOY_TEXT_RE = re.compile(
    r"\b(?:"
    r"microservices?|services?|"
    r"deployments?|deploys?|deployed|deploying|redeploy\w*|"
    r"ships?|shipped|shipping|"
    r"endpoints?|routes?|"
    r"bridges?|bridged|bridging|"
    r"docker\w*|kubernetes|k8s|terraform|ansible|helms?|"
    r"production|prod|"
    r"live[- ]surface"
    r")\b",
    re.IGNORECASE,
)
# Path-level deployment terms: descriptors, infra-as-code, and deploy paths.
_DEPLOY_PATH_RES = (
    re.compile(
        r"(?:^|/)(?:dockerfile[^/]*|docker-compose[^/]*|compose\.ya?ml|"
        r"procfile|caddyfile|fly\.toml|vercel\.json|netlify\.toml|"
        r"app\.ya?ml|\.dockerignore)$",
        re.IGNORECASE,
    ),
    re.compile(r"\.(?:tf|service)$", re.IGNORECASE),
    re.compile(
        r"deploy|(?:^|/)(?:docker|k8s|kubernetes|helm|ansible|terraform)(?:/|$)",
        re.IGNORECASE,
    ),
)
_DOCS_PATH_RE = re.compile(
    r"(?:^|/)(?:docs?|man)(?:/|$)"
    r"|(?:^|/)(?:README|CHANGELOG|CONTRIBUTING|LICENSE|AUTHORS)(?:\.|$)"
    r"|(?:^|/)[^/]*\.(?:md|rst|txt|adoc)$",
    re.IGNORECASE,
)
_TEST_PATH_RE = re.compile(
    r"(?:^|/)tests?(?:/|$)"
    r"|(?:^|/)(?:test_[^/]*|conftest\.py$)"
    r"|[^/]*(?:_test|_spec)\.[a-zA-Z]+$",
    re.IGNORECASE,
)
# Task-text framing that affirmatively shows library/tests/docs-only work when
# no paths were declared. Deliberately tight: bare "test" or "doc" never
# matches, so implement-the-feature prose stays ambiguous (delivery owed).
# Each entry notes whether a docs/test/library noun alone is enough (strong)
# or code-work language elsewhere in the text vetoes it (weak): "Read the
# README then implement the fix" mentions docs but is not docs-only work.
_ONLY_TEXT_RES = (
    (
        "explicitly scoped",
        re.compile(
            r"\b(?:docs?|documentation|readme|tests?|unit[- ]tests?|"
            r"librar(?:y|ies))\s+only\b"
            r"|\bonly\s+(?:docs?|documentation|tests?)\b"
            r"|\bno\s+(?:code|functional|deployable|deployed)\s+(?:changes?|surface)\b"
            r"|\bnothing\s+deployable\b|\bnot\s+deployed\b|\bno\s+deployment\b",
            re.IGNORECASE,
        ),
        False,
    ),
    (
        "docs-only",
        re.compile(r"\btypos?\b", re.IGNORECASE),
        False,
    ),
    (
        "docs-only",
        re.compile(
            r"\breadmes?\b|\bchangelogs?\b|\bdocstrings?\b|"
            r"\buser\s+guides?\b|\bman\s+pages?\b|"
            r"\b(?:update|write|fix|improve|correct)\s+(?:the\s+)?(?:docs?|documentation)\b|"
            r"\bdocument\s+(?:the|how|a|an)\b",
            re.IGNORECASE,
        ),
        True,
    ),
    (
        "tests-only",
        re.compile(
            r"\bunit[- ]tests?\b|\btest\s+coverage\b|"
            r"\b(?:add|write|extend)\s+(?:more\s+)?tests?\b|"
            r"\bregression\s+tests?\b",
            re.IGNORECASE,
        ),
        False,
    ),
    (
        "library-only",
        re.compile(
            r"\brefactor(?:ing|ed|s)?\b|\blibrar(?:y|ies)\b|"
            r"\bpure\s+functions?\b|\bhelper\s+(?:functions?|modules?|librar(?:y|ies))\b",
            re.IGNORECASE,
        ),
        False,
    ),
)
# Code-work language that vetoes a weak docs-noun match. "Fix the parser,
# see README" is code work; "fix the README links" is docs work, so a fix
# aimed at docs does not veto.
_CODE_WORK_RE = re.compile(
    r"\bimplement\w*|\bdevelop\w+|\bcod(?:e|ing)\b|\bprogram(?:ming|med)?\b|"
    r"\bfix(?:es|ed|ing)?\b"
    r"(?!\s+(?:a\s+|the\s+)?(?:typos?|readmes?|docs?|documentation|changelogs?)\b)",
    re.IGNORECASE,
)
_CODING_DELIVERY = (
    "the change is integrated into the run's base checkout, not left only "
    "in a worker worktree or an uncommitted patch",
    "every external surface this unit touches (service, bridge, endpoint, "
    "deployment target) is deployed, or recorded as explicitly out of scope",
    "the delivered result is verified against the live surface, with the "
    "observed response, or the exact reason live verification was impossible",
)


def completion_unit_ids(worker: dict[str, Any], phase: str) -> list[str]:
    """Return the logical units whose contract this phase must satisfy.

    Research, implementation, and fix phases advance the worker's owned
    assignments. Review is deliberately rotated, so its durable phase state
    and evidence belong to the units in ``review_target_ids`` instead. A solo
    worker has no rotated target and therefore self-reviews its assignments.
    """

    assignments = _ids(worker.get("assignment_ids"))
    review_targets = _ids(worker.get("review_target_ids"))
    if str(phase).strip().lower() == "verify" and review_targets:
        return review_targets
    return assignments


def build_work_unit_contracts(
    activity: str,
    workstreams: list[dict[str, Any]],
    workers: list[dict[str, Any]],
    *,
    cwd: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Build stable contracts from deterministic Fleet assignments."""

    if activity not in {"coding", "research"}:
        raise ValueError("work-unit activity must be coding or research")
    owner_by_id: dict[str, str] = {}
    reviewers_by_id: dict[str, list[str]] = {}
    for worker in workers:
        worker_key = str(worker.get("worker_key") or "").strip()
        if not worker_key:
            raise ValueError("work-unit owners require stable worker keys")
        for identifier in _ids(worker.get("assignment_ids")):
            if identifier in owner_by_id and owner_by_id[identifier] != worker_key:
                raise ValueError(f"work unit {identifier} has more than one logical owner")
            owner_by_id[identifier] = worker_key
        for identifier in _ids(worker.get("review_target_ids")):
            reviewers_by_id.setdefault(identifier, []).append(worker_key)

    primary_ids = [
        str(item.get("id") or "").strip()
        for item in workstreams
        if not bool(item.get("synthetic"))
    ]
    contracts: list[dict[str, Any]] = []
    for item in workstreams:
        identifier = str(item.get("id") or "").strip()
        title = str(item.get("title") or identifier).strip()
        description = str(item.get("description") or title).strip()
        owner = owner_by_id.get(identifier)
        if not identifier or not owner:
            raise ValueError(f"work unit {identifier or '(missing)'} has no logical owner")
        synthetic = bool(item.get("synthetic"))
        declared_paths = (
            extract_declared_paths(f"{title}\n{description}", cwd=cwd)
            if activity == "coding"
            else []
        )
        dependencies = tuple(dict.fromkeys([
            candidate
            for candidate in primary_ids
            if synthetic and candidate != identifier
        ] + list(item.get("dependency_ids") or []) + [value.lower() for value in re.findall(
            r"\bdepends\s+on\s+(ws-\d+)\b", description, re.IGNORECASE
        )]))
        file_mode = (
            "declare_before_edit"
            if declared_paths
            else "repository_serialized"
            if activity == "coding"
            else "read_only"
        )
        contracts.append(
            {
                "schema_version": WORK_UNIT_SCHEMA_VERSION,
                "id": identifier,
                "title": title,
                "description": description,
                "synthetic": synthetic,
                "owner_worker_key": owner,
                "reviewer_worker_keys": list(
                    dict.fromkeys(reviewers_by_id.get(identifier, ()))
                ),
                "dependency_ids": list(dependencies),
                "dependency_mode": "phase_barrier" if dependencies else "none",
                "logical_scope": description,
                "file_ownership": {
                    "mode": file_mode,
                    "declared_paths": declared_paths,
                    "claim_required_before_write": activity == "coding",
                    "claim_required_before_integration": activity == "coding",
                },
                "completion_contract": _completion_contract(
                    activity,
                    description,
                    title=title,
                    declared_paths=declared_paths,
                ),
            }
        )
    validate_work_unit_contracts(contracts, workstreams=workstreams, workers=workers)
    return contracts


def validate_work_unit_contracts(
    value: object,
    *,
    workstreams: list[dict[str, Any]],
    workers: list[dict[str, Any]],
) -> None:
    if not isinstance(value, list):
        raise ValueError("Fleet work_units must be a list")
    workstream_ids = {
        str(item.get("id") or "").strip()
        for item in workstreams
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    worker_keys = {
        str(worker.get("worker_key") or "").strip()
        for worker in workers
        if isinstance(worker, dict) and str(worker.get("worker_key") or "").strip()
    }
    units: dict[str, dict[str, Any]] = {}
    for unit in value:
        if not isinstance(unit, dict):
            raise ValueError("Fleet work units must be objects")
        if int(unit.get("schema_version") or 0) != WORK_UNIT_SCHEMA_VERSION:
            raise ValueError("Fleet work unit has an unsupported schema")
        identifier = str(unit.get("id") or "").strip()
        if not identifier or identifier in units:
            raise ValueError("Fleet work-unit ids must be present and unique")
        if not str(unit.get("title") or "").strip():
            raise ValueError(f"Fleet work unit {identifier} requires a title")
        if not str(unit.get("description") or "").strip():
            raise ValueError(f"Fleet work unit {identifier} requires a description")
        owner = str(unit.get("owner_worker_key") or "").strip()
        if owner not in worker_keys:
            raise ValueError(f"Fleet work unit {identifier} references an unknown owner")
        reviewers = _required_id_list(unit.get("reviewer_worker_keys"), "reviewer_worker_keys")
        if not set(reviewers).issubset(worker_keys):
            raise ValueError(f"Fleet work unit {identifier} references an unknown reviewer")
        dependencies = _required_id_list(unit.get("dependency_ids"), "dependency_ids")
        if identifier in dependencies:
            raise ValueError(f"Fleet work unit {identifier} cannot depend on itself")
        mode = str(unit.get("dependency_mode") or "")
        if mode not in DEPENDENCY_MODES:
            raise ValueError(f"Fleet work unit {identifier} has an invalid dependency mode")
        if bool(dependencies) != (mode == "phase_barrier"):
            raise ValueError(f"Fleet work unit {identifier} dependency mode is inconsistent")
        ownership = unit.get("file_ownership")
        if not isinstance(ownership, dict):
            raise ValueError(f"Fleet work unit {identifier} requires file ownership metadata")
        if str(ownership.get("mode") or "") not in FILE_OWNERSHIP_MODES:
            raise ValueError(f"Fleet work unit {identifier} has an invalid file ownership mode")
        paths = ownership.get("declared_paths")
        if not isinstance(paths, list) or any(not str(path).strip() for path in paths):
            raise ValueError(f"Fleet work unit {identifier} declared_paths must be a list")
        completion = unit.get("completion_contract")
        if not isinstance(completion, dict) or not str(completion.get("outcome") or "").strip():
            raise ValueError(f"Fleet work unit {identifier} requires a completion outcome")
        for key in ("acceptance_criteria", "required_evidence", "constraints", "stop_conditions"):
            entries = completion.get(key)
            if not isinstance(entries, list) or not entries or any(
                not str(entry).strip() for entry in entries
            ):
                raise ValueError(f"Fleet work unit {identifier} requires {key}")
        # Present for every unit, empty only where nothing is deployed. Absent
        # means an old contract, which would silently skip delivery entirely.
        delivery = completion.get("delivery_requirements")
        if not isinstance(delivery, list) or any(
            not str(entry).strip() for entry in delivery
        ):
            raise ValueError(
                f"Fleet work unit {identifier} requires delivery_requirements "
                "(an empty list when the unit delivers nothing deployable)"
            )
        units[identifier] = unit
    if set(units) != workstream_ids:
        raise ValueError("Fleet work units must cover every workstream exactly once")
    for identifier, unit in units.items():
        unknown = set(_ids(unit.get("dependency_ids"))) - set(units)
        if unknown:
            raise ValueError(f"Fleet work unit {identifier} has unknown dependencies")
    _assert_acyclic(units)
    _serialise_declared_path_overlaps(units)
    _assert_acyclic(units)


def extract_declared_paths(
    value: str,
    *,
    cwd: str | Path | None = None,
) -> list[str]:
    """Extract explicit repository paths without treating prose as ownership."""

    source = str(value or "")
    ownership_scopes: list[str] = []
    for match in _OWNERSHIP_CLAUSE.finditer(source):
        # Paths in "read X", "do not edit X", test commands, and dependency
        # notes are not write ownership. Treating any repository-looking token
        # as an exact claim made a task that merely said "Read CLAUDE.md" claim
        # only that file, then reject every real implementation path after the
        # worker had completed valid work. Unknown ownership is deliberately a
        # repository-wide serialized claim, so false negatives are safe while
        # false positives are not.
        prefix = source[max(0, match.start() - 24) : match.start()]
        scope = match.group("scope").strip()
        if scope and not _NEGATED_OWNERSHIP.search(prefix):
            ownership_scopes.append(scope)
    if not ownership_scopes:
        return []
    # An explicit positive ownership clause is authoritative. Paths mentioned
    # later in commands, tests, or read-only verification are dependencies,
    # not writes.
    scan_source = "\n".join(ownership_scopes)
    root = Path(cwd).expanduser().resolve() if cwd is not None else None
    paths: list[str] = []
    for match in _PATH_TOKEN.finditer(scan_source):
        if (
            match.start() >= 3
            and scan_source[match.start() - 3 : match.start()] == "://"
        ):
            continue
        # A path at the end of a sentence is prose punctuation, not a Linux
        # filename whose final component ends in a dot. Without this trim,
        # ``core/parser.py.`` silently failed the repository-existence check.
        candidate = match.group("path").rstrip("/.")
        parts = Path(candidate).parts
        if not candidate or any(part in {"", ".", ".."} for part in parts):
            continue
        if root is not None:
            target = (root / candidate).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            # Explicit positive file ownership can name a file this worker is
            # meant to create. Missing directories remain ambiguous scope;
            # a bounded filename is sufficient even below a new directory.
            if not target.exists() and not Path(candidate).suffix:
                continue
        paths.append(candidate)
    return list(dict.fromkeys(paths))


def _serialise_declared_path_overlaps(units: dict[str, dict[str, Any]]) -> None:
    """Turn proven ownership overlap into a deterministic dependency edge."""

    from fleet.isolation import paths_overlap

    identifiers = sorted(units)
    for offset, first_id in enumerate(identifiers):
        first_paths = _declared_paths(units[first_id])
        if not first_paths:
            continue
        for second_id in identifiers[offset + 1 :]:
            second_paths = _declared_paths(units[second_id])
            if not second_paths or not any(
                paths_overlap(first, second)
                for first in first_paths
                for second in second_paths
            ):
                continue
            if _depends_on(units, first_id, second_id) or _depends_on(
                units, second_id, first_id
            ):
                continue
            dependencies = list(_ids(units[second_id].get("dependency_ids")))
            dependencies.append(first_id)
            units[second_id]["dependency_ids"] = list(dict.fromkeys(dependencies))
            units[second_id]["dependency_mode"] = "phase_barrier"


def _declared_paths(unit: dict[str, Any]) -> tuple[str, ...]:
    ownership = unit.get("file_ownership")
    if not isinstance(ownership, dict):
        return ()
    return _ids(ownership.get("declared_paths"))


def _depends_on(
    units: dict[str, dict[str, Any]],
    identifier: str,
    target: str,
    seen: set[str] | None = None,
) -> bool:
    if identifier == target:
        return True
    visited = set() if seen is None else set(seen)
    if identifier in visited:
        return False
    visited.add(identifier)
    return any(
        dependency == target or _depends_on(units, dependency, target, visited)
        for dependency in _ids(units[identifier].get("dependency_ids"))
        if dependency in units
    )


def derive_work_unit_views(
    policy: dict[str, Any],
    phases: list[dict[str, Any]],
    run_state: str,
) -> list[dict[str, Any]]:
    """Project durable leg state and receipts onto persisted work contracts."""

    units = policy.get("work_units") if isinstance(policy, dict) else None
    if not isinstance(units, list):
        return []
    legs_by_worker: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for phase in phases:
        for leg in phase.get("legs") or []:
            key = str(leg.get("worker_key") or "")
            legs_by_worker.setdefault(key, []).append((phase, leg))

    base_states: dict[str, str] = {}
    views: dict[str, dict[str, Any]] = {}
    for unit in units:
        if not isinstance(unit, dict):
            continue
        identifier = str(unit.get("id") or "")
        owned = legs_by_worker.get(str(unit.get("owner_worker_key") or ""), [])
        leg_states = [str(leg.get("state") or "queued") for _phase, leg in owned]
        completed = sum(state == "completed" for state in leg_states)
        base = _base_state(run_state, leg_states)
        base_states[identifier] = base
        receipts: list[dict[str, Any]] = []
        for phase, leg in owned:
            attempt = leg.get("current_attempt") or {}
            output = str(attempt.get("output_text") or "").strip()
            if str(attempt.get("state") or "") != "completed" or not output:
                continue
            receipts.append(
                {
                    "phase": str(phase.get("name") or ""),
                    "attempt_id": str(attempt.get("attempt_id") or ""),
                    "session_id": str(attempt.get("session_id") or ""),
                    "summary": output[:500],
                }
            )
        view = deepcopy(unit)
        view.update(
            {
                "state": base,
                "progress": {"completed": completed, "total": len(leg_states)},
                "evidence_receipts": receipts[-4:],
                "blocked_dependency_ids": [],
            }
        )
        views[identifier] = view

    def resolved(identifier: str, stack: set[str] | None = None) -> str:
        base = base_states.get(identifier, "queued")
        if base != "completed":
            return base
        active_stack = set() if stack is None else set(stack)
        if identifier in active_stack:
            return "failed"
        active_stack.add(identifier)
        dependencies = _ids(views[identifier].get("dependency_ids"))
        blocked = [dependency for dependency in dependencies if resolved(dependency, active_stack) != "completed"]
        views[identifier]["blocked_dependency_ids"] = blocked
        return "waiting_for_dependencies" if blocked else "completed"

    for identifier, view in views.items():
        view["state"] = resolved(identifier)
    return [views[str(unit.get("id"))] for unit in units if str(unit.get("id")) in views]


def _scope_delivery(
    title: str,
    description: str,
    declared_paths: tuple[str, ...] | list[str],
) -> tuple[list[str], dict[str, Any]]:
    """Decide per-unit delivery requirements deterministically at plan time.

    A unit with no deployable surface carries ``delivery=[]`` so the gate
    never parks library/tests/docs-only work on deployment evidence. Anything
    else keeps the full requirements. Ambiguous units fail closed: delivery
    is owed unless the task text plus declared paths affirmatively show
    library/tests/docs-only work with no deploy signal.
    """

    paths = [str(path).strip() for path in declared_paths if str(path).strip()]
    blob = f"{title}\n{description}\n" + "\n".join(paths)
    signals = sorted({_match.group(0).lower() for _match in _DEPLOY_TEXT_RE.finditer(blob)})
    deploy_paths = sorted(
        {path for path in paths if any(item.search(path) for item in _DEPLOY_PATH_RES)}
    )
    if signals or deploy_paths:
        detail = ", ".join(signals + [f"path:{path}" for path in deploy_paths])
        return list(_CODING_DELIVERY), {
            "deliverable": True,
            "reason": f"deploy signal in task text or declared paths ({detail}); delivery owed",
            "signals": signals + [f"path:{path}" for path in deploy_paths],
        }
    if paths:
        kinds = {_classify_path(path) for path in paths}
        if kinds <= {"docs"}:
            kind = "docs-only"
        elif kinds <= {"tests"}:
            kind = "tests-only"
        elif kinds <= {"docs", "tests"}:
            kind = "docs/tests-only"
        else:
            kind = "library-only"
        evidence = ", ".join(sorted(paths)[:4])
        return [], {
            "deliverable": False,
            "reason": (
                f"no deployable surface: {kind} declared paths ({evidence}); "
                "no service/deploy/endpoint/bridge signal in task text or declared paths"
            ),
            "signals": [],
        }
    task_text = f"{title}\n{description}"
    for kind, pattern, weak in _ONLY_TEXT_RES:
        match = pattern.search(task_text)
        if match:
            if weak and _CODE_WORK_RE.search(task_text):
                continue
            return [], {
                "deliverable": False,
                "reason": (
                    f"no deployable surface: task text is {kind} "
                    f"({match.group(0).strip().lower()}); no service/deploy/endpoint/bridge "
                    "signal in task text or declared paths"
                ),
                "signals": [],
            }
    return list(_CODING_DELIVERY), {
        "deliverable": True,
        "reason": (
            "no deployable-surface evidence either way; delivery owed by default "
            "(ambiguous scope fails closed)"
        ),
        "signals": [],
    }


def _classify_path(path: str) -> str:
    if _DOCS_PATH_RE.search(path):
        return "docs"
    if _TEST_PATH_RE.search(path):
        return "tests"
    return "code"


def _completion_contract(
    activity: str,
    description: str,
    *,
    title: str = "",
    declared_paths: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    # Follows the workstream limit rather than halving it again here: a
    # contract that states less than the unit it contracts for is how delivery
    # requirements fell off the end.
    outcome = f"Deliver and verify this bounded work unit: {description}"[:20_000]
    shared_constraints = [
        "preserve Serena's authority rules, native provider identity, and unrelated dirty work",
        "stay inside this logical scope and reconcile shared dependencies explicitly",
        "never claim a test, source, runtime result, or completion state that was not observed",
    ]
    shared_stops = [
        "a required dependency or file claim conflicts with another logical owner",
        "the requested result cannot be verified from the available evidence",
        "completion requires destructive, external, or scope-expanding authority not granted by the task",
    ]
    if activity == "coding":
        acceptance = [
            "the requested behavior is implemented within this work unit's scope",
            "the result integrates with dependency outputs without overwriting peer changes",
            "relevant focused tests or checks pass, or the exact blocker is recorded",
        ]
        # Delivery is tracked apart from the acceptance criteria above, and
        # deliberately so. Those three read as satisfied the moment code exists
        # and tests pass, which is how a run shipped nothing and was accepted as
        # complete: the worker implemented, handed deployment back to its
        # coordinator, and every criterion above was honestly true. Writing code
        # is not delivering it. Each requirement here must be answered on its
        # own terms with observed evidence, or explicitly deferred to an owner,
        # and a deferral keeps the whole run open until it is discharged.
        # Scoped per unit at plan time: library/tests/docs-only units carry no
        # delivery requirements, with the scoping decision recorded for audit.
        delivery, delivery_scope = _scope_delivery(
            title or description, description, declared_paths
        )
        evidence = [
            "changed paths, declared ownership, or an explicit no-change finding",
            "exact validation commands and observed outcomes",
            "remaining risks, dependency conflicts, and handoff notes",
        ]
    else:
        acceptance = [
            "claims stay within scope and distinguish observation from inference",
            "material findings are backed by repository evidence or attributable sources",
            "conflicts, uncertainty, and unresolved questions are explicit",
        ]
        evidence = [
            "the sources, files, commands, or observations actually inspected",
            "the material findings and how each follows from that evidence",
            "remaining uncertainty, contradictions, and follow-up limits",
        ]
        # A research unit delivers a report, which its required evidence already
        # covers. Nothing is deployed, so there is nothing to defer.
        delivery = []
        delivery_scope = {
            "deliverable": False,
            "reason": "research units deliver a report covered by required evidence; nothing is deployed",
            "signals": [],
        }
    return {
        "outcome": outcome,
        "acceptance_criteria": acceptance,
        "delivery_requirements": delivery,
        "delivery_scope": delivery_scope,
        "required_evidence": evidence,
        "constraints": shared_constraints,
        "stop_conditions": shared_stops,
    }


def _base_state(run_state: str, leg_states: list[str]) -> str:
    if run_state == "planned":
        return "planned"
    if run_state == "cancelled":
        return "cancelled"
    if leg_states and all(state == "completed" for state in leg_states):
        return "completed"
    if "waiting_for_capacity" in leg_states:
        return "waiting_for_capacity"
    if "waiting_for_resources" in leg_states:
        return "waiting_for_resources"
    if "waiting_for_input" in leg_states:
        return "waiting_for_input"
    if "running" in leg_states or "completed" in leg_states:
        return "in_progress"
    if run_state == "failed" or "failed" in leg_states:
        return "failed"
    return "queued"


def _ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _required_id_list(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not str(item).strip() for item in value):
        raise ValueError(f"Fleet work unit {name} must be a list of ids")
    entries = tuple(str(item).strip() for item in value)
    if len(entries) != len(set(entries)):
        raise ValueError(f"Fleet work unit {name} cannot contain duplicates")
    return entries


def _assert_acyclic(units: dict[str, dict[str, Any]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visited:
            return
        if identifier in visiting:
            raise ValueError("Fleet work-unit dependencies must be acyclic")
        visiting.add(identifier)
        for dependency in _ids(units[identifier].get("dependency_ids")):
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in units:
        visit(identifier)
