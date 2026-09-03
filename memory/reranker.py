"""Deterministic, state-aware ranking policy for local memory retrieval.

Candidate generation is deliberately outside this module. Exact, lexical, and
dense retrievers can all supply bounded candidates, while this policy applies
the same privacy, temporal, state, confidence, and diversity rules to them.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

RANKING_VERSION = "state-aware-reranker-v4"
MAX_RERANK_CANDIDATES = 128

COMPONENT_WEIGHTS: dict[str, float] = {
    "literal": 0.24,
    "semantic": 0.20,
    "exact": 0.08,
    "project": 0.08,
    "people": 0.06,
    "entity": 0.06,
    "recency": 0.05,
    "temporal": 0.05,
    "state": 0.10,
    "correction": 0.08,
}

_TYPE_HALF_LIFE_DAYS = {
    "commitment": 30.0,
    "correction": 90.0,
    "episode": 180.0,
    "preference": 730.0,
    "semantic_fact": 1_095.0,
    "procedure": 1_460.0,
}
_CURRENT_TERMS = frozenset({"active", "current", "currently", "latest", "now", "today"})
_HISTORICAL_TERMS = frozenset(
    {"before", "earlier", "former", "historical", "previous", "previously", "used"}
)
_CONFLICT_TERMS = frozenset(
    {"conflict", "conflicting", "contest", "contested", "contradict", "contradiction"}
)
_WORD = re.compile(r"[a-z0-9]+")


class RankableRecord(Protocol):
    record_id: str
    record_type: str
    content: str
    project: str
    people: Sequence[str]
    confidence: float
    sensitivity: str
    valid_from: float | None
    valid_until: float | None
    retention_until: float | None
    status: str
    legacy_type: str | None
    updated_at: float


@dataclass(frozen=True, slots=True)
class CandidateSignals:
    record: RankableRecord
    literal: float = 0.0
    semantic: float = 0.0
    exact: float = 0.0
    project: float = 0.0
    people: float = 0.0
    entity: float = 0.0
    relevance_penalty: float = 0.0


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    candidate: CandidateSignals
    score: float
    components: dict[str, float]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RerankResult:
    ranked: tuple[RankedCandidate, ...]
    temporal_intent: str
    input_count: int
    eligible_count: int
    bounded_count: int
    deduplicated_count: int
    filtered_counts: dict[str, int]

    def receipt_details(self) -> dict[str, Any]:
        return {
            "temporal_intent": self.temporal_intent,
            "input_count": self.input_count,
            "eligible_count": self.eligible_count,
            "bounded_count": self.bounded_count,
            "deduplicated_count": self.deduplicated_count,
            "filtered_counts": dict(sorted(self.filtered_counts.items())),
            "component_weights": dict(COMPONENT_WEIGHTS),
            "candidate_limit": MAX_RERANK_CANDIDATES,
        }


def resolve_temporal_intent(
    query: str,
    explicit: str | None = None,
    *,
    as_of: float | None = None,
) -> str:
    """Resolve the small ranking intent vocabulary without retaining query text."""

    if explicit:
        clean = str(explicit).strip().lower().replace("-", "_")
        aliases = {"past": "historical", "history": "historical", "latest": "current"}
        clean = aliases.get(clean, clean)
        if clean not in {"current", "historical", "conflict", "all"}:
            raise ValueError("invalid temporal intent")
        return clean
    if as_of is not None:
        return "historical"
    terms = _tokens(query)
    if terms.intersection(_CONFLICT_TERMS):
        return "conflict"
    if terms.intersection(_CURRENT_TERMS):
        return "current"
    if terms.intersection(_HISTORICAL_TERMS):
        return "historical"
    return "current"


def rerank_candidates(
    candidates: Sequence[CandidateSignals],
    *,
    query: str,
    now: float,
    limit: int,
    surface: str = "private",
    temporal_intent: str | None = None,
    as_of: float | None = None,
    include_contested: bool = False,
    include_superseded: bool | None = None,
    active_record_ids: Sequence[str] = (),
) -> RerankResult:
    """Filter, score, deduplicate, and diversify an already-local candidate set."""

    intent = resolve_temporal_intent(query, temporal_intent, as_of=as_of)
    active_ids = {str(value) for value in active_record_ids if str(value)}
    filtered: Counter[str] = Counter()
    eligible: list[CandidateSignals] = []
    for candidate in candidates:
        reason = _filter_reason(
            candidate.record,
            now=now,
            surface=surface,
            temporal_intent=intent,
            as_of=as_of,
            include_contested=include_contested,
            include_superseded=include_superseded,
        )
        if reason:
            filtered[reason] += 1
            continue
        if not _has_relevance(candidate):
            filtered["no_relevance"] += 1
            continue
        eligible.append(candidate)

    eligible.sort(key=lambda candidate: _candidate_bound_key(candidate, active_ids=active_ids))
    bounded = eligible[:MAX_RERANK_CANDIDATES]
    scored = [
        _score(candidate, now=now, temporal_intent=intent, active_ids=active_ids)
        for candidate in bounded
    ]
    scored.sort(key=_rank_key)
    deduplicated = _deduplicate(scored)
    selected = _diversify(deduplicated, limit=max(1, int(limit)))
    return RerankResult(
        ranked=tuple(selected),
        temporal_intent=intent,
        input_count=len(candidates),
        eligible_count=len(eligible),
        bounded_count=len(bounded),
        deduplicated_count=max(0, len(scored) - len(deduplicated)),
        filtered_counts=dict(filtered),
    )


def _filter_reason(
    record: RankableRecord,
    *,
    now: float,
    surface: str,
    temporal_intent: str,
    as_of: float | None,
    include_contested: bool,
    include_superseded: bool | None,
) -> str:
    if surface != "private" and record.sensitivity != "public":
        return "sensitivity"
    if record.status in {"forgotten", "retracted"}:
        return "inactive_state"
    allow_contested = include_contested or temporal_intent in {"conflict", "all"}
    if record.status == "contested" and not allow_contested:
        return "contested"
    allow_superseded = (
        temporal_intent in {"historical", "conflict", "all"}
        if include_superseded is None
        else bool(include_superseded)
    )
    if record.status == "superseded" and not allow_superseded:
        return "superseded"
    if record.status not in {"current", "superseded", "contested"}:
        return "inactive_state"
    if record.retention_until is not None and record.retention_until <= now:
        return "retention"

    reference_time = now if as_of is None else float(as_of)
    if record.valid_from is not None and record.valid_from > reference_time:
        return "not_yet_valid"
    if (
        record.valid_until is not None
        and record.valid_until <= reference_time
        and (temporal_intent not in {"historical", "conflict", "all"} or as_of is not None)
    ):
        return "expired"
    return ""


def _has_relevance(candidate: CandidateSignals) -> bool:
    return max(
        candidate.literal,
        candidate.semantic,
        candidate.exact,
        candidate.project,
        candidate.people,
        candidate.entity,
    ) > 0.0


def _candidate_bound_key(
    candidate: CandidateSignals,
    *,
    active_ids: set[str],
) -> tuple[int, float, float, str]:
    record = candidate.record
    priority = int(record.record_type == "correction" and record.status == "current")
    priority += int(_is_active(record, active_ids))
    retrieval_score = (
        0.34 * _unit(candidate.literal)
        + 0.34 * _unit(candidate.semantic)
        + 0.12 * _unit(candidate.exact)
        + 0.08 * _unit(candidate.project)
        + 0.06 * _unit(candidate.people)
        + 0.06 * _unit(candidate.entity)
    )
    retrieval_score = max(0.0, retrieval_score - _unit(candidate.relevance_penalty))
    return (-priority, -retrieval_score, -record.updated_at, record.record_id)


def _score(
    candidate: CandidateSignals,
    *,
    now: float,
    temporal_intent: str,
    active_ids: set[str],
) -> RankedCandidate:
    record = candidate.record
    active = _is_active(record, active_ids)
    state = _state_score(record, temporal_intent=temporal_intent, active=active)
    correction = 1.0 if record.record_type == "correction" and record.status == "current" else 0.0
    recency = _recency_score(record, now=now)
    temporal = _temporal_score(record.status, temporal_intent)
    raw = {
        "literal": _unit(candidate.literal),
        "semantic": _unit(candidate.semantic),
        "exact": _unit(candidate.exact),
        "project": _unit(candidate.project),
        "people": _unit(candidate.people),
        "entity": _unit(candidate.entity),
        "recency": recency,
        "temporal": temporal,
        "state": state,
        "correction": correction,
    }
    components = {
        name: round(COMPONENT_WEIGHTS[name] * value, 6) for name, value in raw.items()
    }
    confidence = _unit(record.confidence)
    confidence_multiplier = 0.55 + 0.45 * confidence
    pre_feedback = sum(components.values()) * confidence_multiplier
    relevance_penalty = _unit(candidate.relevance_penalty)
    pre_diversity = max(0.0, pre_feedback - relevance_penalty)
    components["confidence"] = round(confidence, 6)
    components["confidence_multiplier"] = round(confidence_multiplier, 6)
    components["pre_feedback"] = round(pre_feedback, 6)
    components["negative_feedback_penalty"] = round(relevance_penalty, 6)
    components["pre_diversity"] = round(pre_diversity, 6)
    components["diversity_penalty"] = 0.0
    reasons = [
        f"{name}:{value:.3f}"
        for name, value in raw.items()
        if value > 0.0
    ]
    reasons.append(f"confidence:{confidence:.3f}")
    if active:
        reasons.append("active_state_priority")
    if correction:
        reasons.append("correction_priority")
    if relevance_penalty:
        reasons.append(f"negative_relevance_feedback:-{relevance_penalty:.3f}")
    return RankedCandidate(
        candidate=candidate,
        score=round(pre_diversity, 6),
        components=components,
        reasons=tuple(reasons),
    )


def _is_active(record: RankableRecord, active_ids: set[str]) -> bool:
    return record.record_id in active_ids or record.legacy_type in {"ledger", "task", "loop"}


def _state_score(record: RankableRecord, *, temporal_intent: str, active: bool) -> float:
    if temporal_intent == "historical":
        if record.status == "superseded":
            return 0.9
        if record.status == "current":
            return 0.55 if not active else 0.8
        return 0.25
    if record.status == "contested":
        return 0.2
    if record.status == "superseded":
        return 0.15
    if record.record_type == "correction":
        return 1.0
    if active:
        return 0.95
    if record.record_type == "commitment":
        return 0.8
    return 0.4


def _temporal_score(status: str, intent: str) -> float:
    if intent == "historical":
        return {"superseded": 1.0, "current": 0.55, "contested": 0.3}.get(status, 0.0)
    if intent == "conflict":
        return {"contested": 1.0, "superseded": 0.65, "current": 0.55}.get(status, 0.0)
    if intent == "all":
        return {"current": 1.0, "superseded": 0.6, "contested": 0.4}.get(status, 0.0)
    return 1.0 if status == "current" else 0.0


def _recency_score(record: RankableRecord, *, now: float) -> float:
    half_life = _TYPE_HALF_LIFE_DAYS.get(record.record_type, 365.0)
    age_days = max(0.0, (now - record.updated_at) / 86_400.0)
    return math.exp(-math.log(2.0) * age_days / half_life)


def _deduplicate(scored: Sequence[RankedCandidate]) -> list[RankedCandidate]:
    selected: list[RankedCandidate] = []
    selected_tokens: list[set[str]] = []
    seen_exact: set[str] = set()
    for ranked in scored:
        tokens = _tokens(ranked.candidate.record.content)
        exact_key = " ".join(sorted(tokens))
        if exact_key in seen_exact:
            continue
        if any(_jaccard(tokens, prior) >= 0.9 for prior in selected_tokens):
            continue
        selected.append(ranked)
        selected_tokens.append(tokens)
        seen_exact.add(exact_key)
    return selected


def _diversify(scored: Sequence[RankedCandidate], *, limit: int) -> list[RankedCandidate]:
    remaining = list(scored)
    selected: list[RankedCandidate] = []
    while remaining and len(selected) < limit:
        best: RankedCandidate | None = None
        best_key: tuple[float, float, str] | None = None
        for ranked in remaining:
            record = ranked.candidate.record
            content_similarity = max(
                (
                    _jaccard(_tokens(record.content), _tokens(item.candidate.record.content))
                    for item in selected
                ),
                default=0.0,
            )
            same_project = bool(
                record.project
                and any(
                    record.project.casefold() == item.candidate.record.project.casefold()
                    for item in selected
                )
            )
            same_type = any(record.record_type == item.candidate.record.record_type for item in selected)
            penalty = 0.10 * content_similarity + 0.015 * same_project + 0.01 * same_type
            adjusted = max(0.0, ranked.score - penalty)
            key = (-adjusted, -record.updated_at, record.record_id)
            if best_key is None or key < best_key:
                components = dict(ranked.components)
                components["diversity_penalty"] = round(penalty, 6)
                best = replace(ranked, score=round(adjusted, 6), components=components)
                best_key = key
        assert best is not None
        selected.append(best)
        chosen_id = best.candidate.record.record_id
        remaining = [item for item in remaining if item.candidate.record.record_id != chosen_id]
    return selected


def _rank_key(ranked: RankedCandidate) -> tuple[float, float, str]:
    record = ranked.candidate.record
    return (-ranked.score, -record.updated_at, record.record_id)


def _tokens(value: str) -> set[str]:
    return set(_WORD.findall(str(value or "").casefold()))


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left.intersection(right)) / len(left.union(right))


def _unit(value: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(numeric):
        return 0.0
    return max(0.0, min(1.0, numeric))
