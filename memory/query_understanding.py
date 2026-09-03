"""Deterministic, privacy-safe understanding for local memory retrieval queries.

The planner is deliberately a pure transform. It does not call a model, read a
database, or retain conversation text. Callers provide a small user-only recent
context window and any known people, projects, entities, or aliases they are
allowed to use on that surface.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from datetime import time as datetime_time
from typing import Any

QUERY_UNDERSTANDING_VERSION = "deterministic-query-v1"

MAX_QUERY_CHARS = 2_000
MAX_CONTEXT_TURNS = 4
MAX_CONTEXT_TURN_CHARS = 700
MAX_CONTEXT_CHARS = 2_400
MAX_CATALOG_ITEMS = 256
MAX_ALIASES = 128
MAX_QUERY_VARIANTS = 6
MAX_TOPIC_TERMS = 8

_WORD = re.compile(r"[^\W_]+(?:['-][^\W_]+)*", re.UNICODE)
_CAPITALIZED = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9'_-]{1,})(?:\s+[A-Z][A-Za-z0-9'_-]{1,}){0,2}\b"
)
_ISO_DATE = re.compile(r"\b(20\d{2})-(0[1-9]|1[0-2])-([0-2]\d|3[01])\b")
_DEICTIC = frozenset(
    {"he", "her", "him", "it", "one", "she", "that", "them", "they", "this", "those"}
)
_QUESTION_OPENERS = frozenset(
    {"can", "did", "do", "does", "how", "is", "tell", "what", "when", "where", "which", "who", "why"}
)
_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "after",
        "all",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "before",
        "by",
        "can",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "him",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "she",
        "that",
        "the",
        "their",
        "them",
        "they",
        "this",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)
_DEFAULT_ALIASES = {
    "prefs": "preferences",
    "ref": "reference",
    "to do": "task",
    "todo": "task",
}
_TYPE_TERMS = {
    "commitment": frozenset(
        {"commitment", "deadline", "due", "ledger", "remind", "reminder", "task", "todo"}
    ),
    "correction": frozenset(
        {"correct", "correction", "incorrect", "outdated", "stale", "wrong"}
    ),
    "episode": frozenset(
        {"conversation", "happened", "remember", "session", "talked", "when"}
    ),
    "preference": frozenset(
        {"choose", "favorite", "favourite", "like", "prefer", "preference", "preferences"}
    ),
    "procedure": frozenset(
        {"how", "instruction", "procedure", "process", "steps", "workflow"}
    ),
    "semantic_fact": frozenset(
        {"address", "birthday", "email", "fact", "name", "number", "phone", "where", "who"}
    ),
}


@dataclass(frozen=True, slots=True)
class QueryVariant:
    original: str
    replacement: str
    reason: str
    confidence: float

    def safe_dict(self) -> dict[str, Any]:
        return {
            "original_sha256": _sha256(self.original.casefold()),
            "replacement_sha256": _sha256(self.replacement.casefold()),
            "reason": self.reason,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class TimeIntent:
    kind: str
    as_of: float | None = None
    since: float | None = None
    until: float | None = None
    explicit: bool = False

    @property
    def retrieval_intent(self) -> str:
        if self.kind == "conflict":
            return "conflict"
        if self.kind in {"historical", "range"}:
            return "historical"
        if self.kind == "all":
            return "all"
        return "current"


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """Ephemeral retrieval values plus a separately safe durable summary."""

    normalized_query: str
    topic: str
    terms: tuple[str, ...]
    people: tuple[str, ...]
    entities: tuple[str, ...]
    project: str
    time_intent: TimeIntent
    likely_record_types: tuple[str, ...]
    aliases: tuple[QueryVariant, ...]
    spelling_variants: tuple[QueryVariant, ...]
    query_variants: tuple[str, ...]
    rules_fired: tuple[str, ...]
    query_sha256: str
    context_sha256: str
    context_turn_count: int
    version: str = QUERY_UNDERSTANDING_VERSION

    @property
    def temporal_intent(self) -> str:
        return self.time_intent.retrieval_intent

    @property
    def as_of(self) -> float | None:
        return self.time_intent.as_of

    def retrieval_values(self) -> dict[str, Any]:
        """Return in-process values. Callers must not persist this object."""

        return {
            "project": self.project,
            "people": self.people,
            "entities": self.entities,
            "temporal_intent": self.temporal_intent,
            "as_of": self.as_of,
            "likely_record_types": self.likely_record_types,
            "query_variants": self.query_variants,
        }

    def to_dict(self) -> dict[str, Any]:
        """Return a receipt-safe summary with no raw query, names, or context."""

        value = {
            "version": self.version,
            "query_sha256": self.query_sha256,
            "context_sha256": self.context_sha256,
            "context_turn_count": self.context_turn_count,
            "topic_sha256": _sha256(self.topic.casefold()) if self.topic else "",
            "term_count": len(self.terms),
            "people_count": len(self.people),
            "people_sha256": [_sha256(value.casefold()) for value in self.people],
            "entity_count": len(self.entities),
            "entity_sha256": [_sha256(value.casefold()) for value in self.entities],
            "project_sha256": _sha256(self.project.casefold()) if self.project else "",
            "time_intent": asdict(self.time_intent),
            "temporal_intent": self.temporal_intent,
            "likely_record_types": list(self.likely_record_types),
            "alias_variants": [item.safe_dict() for item in self.aliases],
            "spelling_variants": [item.safe_dict() for item in self.spelling_variants],
            "query_variant_count": len(self.query_variants),
            "rules_fired": list(self.rules_fired),
        }
        value["profile_sha256"] = _sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"))
        )
        return value


def understand_query(
    query: str,
    *,
    recent_context: Sequence[str] = (),
    known_people: Sequence[str] = (),
    known_entities: Sequence[str] = (),
    known_projects: Sequence[str] = (),
    aliases: Mapping[str, str] | None = None,
    project_hint: str = "",
    now: datetime | float | None = None,
) -> QueryPlan:
    """Build one deterministic plan from a query and bounded user-only context."""

    normalized = _clean_text(query, MAX_QUERY_CHARS)
    context = _bounded_context(recent_context, normalized)
    people_catalog = _catalog(known_people)
    entity_catalog = _catalog(known_entities)
    project_catalog = _catalog(known_projects)
    alias_map = _alias_map(aliases)
    current_tokens = _tokens(normalized)
    current_terms = _informative(current_tokens)
    context_tokens = _tokens(" ".join(context))
    use_context = bool(context) and (
        len(current_terms) <= 3 or bool(set(current_tokens).intersection(_DEICTIC))
    )
    effective_text = " ".join((normalized, context[-1] if use_context else "")).strip()
    rules: list[str] = ["unicode_nfkc", "bounded_current_query"]
    if context:
        rules.append("bounded_recent_user_context")
    if use_context:
        rules.append("deictic_context_enrichment")

    alias_variants = _alias_variants(effective_text, alias_map)
    if alias_variants:
        rules.append("explicit_alias_expansion")
    canonical_text = _apply_variants(effective_text, alias_variants)

    vocabulary = _spelling_vocabulary(
        people_catalog,
        entity_catalog,
        project_catalog,
        tuple(alias_map),
        tuple(alias_map.values()),
    )
    spelling_variants = _spelling_variants(normalized, vocabulary)
    if spelling_variants:
        rules.append("bounded_spelling_variant")

    projects = _catalog_matches(canonical_text, project_catalog)
    excluded_people = {value.casefold() for value in (*projects, *entity_catalog)}
    people = _dedupe(
        (
            *_catalog_matches(canonical_text, people_catalog),
            *(
                value
                for value in _person_phrases(effective_text)
                if value.casefold() not in excluded_people
            ),
        )
    )
    explicit_hint = _clean_text(project_hint, 256)
    project = explicit_hint or (projects[0] if projects else _project_phrase(canonical_text))
    if project:
        rules.append("project_match")

    proper_entities = _proper_entities(effective_text)
    entities = _dedupe(
        (*_catalog_matches(canonical_text, entity_catalog), *projects, *proper_entities)
    )
    entities = tuple(value for value in entities if value.casefold() not in {p.casefold() for p in people})
    if people:
        rules.append("people_match")
    if entities:
        rules.append("entity_match")

    time_intent = _time_intent(normalized, _moment(now))
    if time_intent.explicit:
        rules.append(f"time_intent:{time_intent.kind}")

    type_terms = set(_tokens(canonical_text))
    if use_context:
        type_terms.update(context_tokens)
    likely_types = tuple(
        record_type
        for record_type, triggers in _TYPE_TERMS.items()
        if type_terms.intersection(triggers)
    )
    if not likely_types and normalized:
        # An untyped question is a hypothesis, not a hard filter. Multiple
        # likely types tell the retrieval facade to search across record kinds.
        likely_types = ("semantic_fact", "episode", "preference")
    if likely_types:
        rules.append("likely_record_types")

    topic_terms = list(current_terms)
    if use_context:
        topic_terms.extend(_informative(context_tokens))
    topic_terms = list(dict.fromkeys(topic_terms))[:MAX_TOPIC_TERMS]
    topic = " ".join((*([project] if project else []), *topic_terms)).strip()
    query_variants = _query_variants(
        normalized,
        topic=topic if use_context else "",
        aliases=alias_variants,
        spelling=spelling_variants,
    )
    if query_variants:
        rules.append("query_variants")

    return QueryPlan(
        normalized_query=normalized,
        topic=topic,
        terms=tuple(topic_terms),
        people=people,
        entities=entities,
        project=project,
        time_intent=time_intent,
        likely_record_types=likely_types,
        aliases=alias_variants,
        spelling_variants=spelling_variants,
        query_variants=query_variants,
        rules_fired=tuple(dict.fromkeys(rules)),
        query_sha256=_sha256(normalized),
        context_sha256=_sha256("\n".join(context)) if context else "",
        context_turn_count=len(context),
    )


def _bounded_context(values: Sequence[str], query: str) -> tuple[str, ...]:
    selected: list[str] = []
    total = 0
    for value in reversed(tuple(values)[-MAX_CONTEXT_TURNS:]):
        clean = _clean_text(value, MAX_CONTEXT_TURN_CHARS)
        if not clean or clean.casefold() == query.casefold():
            continue
        remaining = MAX_CONTEXT_CHARS - total
        if remaining <= 0:
            break
        clean = clean[:remaining]
        selected.append(clean)
        total += len(clean)
    return tuple(reversed(selected))


def _clean_text(value: object, limit: int) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    safe = "".join(
        " " if char.isspace() else char
        for char in normalized
        if char.isspace() or not unicodedata.category(char).startswith("C")
    )
    return " ".join(safe.split())[:limit]


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(item.casefold() for item in _WORD.findall(value))


def _informative(tokens: Sequence[str]) -> tuple[str, ...]:
    kept = tuple(token for token in tokens if len(token) > 1 and token not in _STOPWORDS)
    return tuple(dict.fromkeys(kept or tokens))


def _catalog(values: Sequence[str]) -> tuple[str, ...]:
    return _dedupe(
        _clean_text(item, 256) for item in tuple(values)[:MAX_CATALOG_ITEMS] if str(item).strip()
    )


def _alias_map(values: Mapping[str, str] | None) -> dict[str, str]:
    combined = dict(_DEFAULT_ALIASES)
    for alias, canonical in list((values or {}).items())[:MAX_ALIASES]:
        clean_alias = _clean_text(alias, 128).casefold()
        clean_canonical = _clean_text(canonical, 256)
        if clean_alias and clean_canonical:
            combined[clean_alias] = clean_canonical
    return dict(sorted(combined.items()))


def _alias_variants(text: str, aliases: Mapping[str, str]) -> tuple[QueryVariant, ...]:
    folded = text.casefold()
    return tuple(
        QueryVariant(alias, canonical, "explicit_alias", 1.0)
        for alias, canonical in aliases.items()
        if _contains_phrase(folded, alias) and alias != canonical.casefold()
    )


def _apply_variants(text: str, variants: Sequence[QueryVariant]) -> str:
    expanded = text
    for variant in variants:
        expanded = _replace_phrase(expanded, variant.original, variant.replacement)
    return expanded


def _catalog_matches(text: str, catalog: Sequence[str]) -> tuple[str, ...]:
    folded = text.casefold()
    return tuple(value for value in catalog if _contains_phrase(folded, value.casefold()))


def _proper_entities(text: str) -> tuple[str, ...]:
    candidates = []
    for match in _CAPITALIZED.findall(text):
        clean = _clean_text(match, 256)
        first = clean.split()[0].casefold() if clean else ""
        if clean and first not in _QUESTION_OPENERS:
            candidates.append(clean)
    return _dedupe(candidates)


def _person_phrases(text: str) -> tuple[str, ...]:
    candidates = re.findall(
        r"\b([A-Z][A-Za-z0-9'_-]{1,63})(?:['’]s)\b|"
        r"\b(?:spoke|talked|meeting|call)\s+(?:to|with)\s+"
        r"([A-Z][A-Za-z0-9'_-]{1,63})\b",
        text,
    )
    return _dedupe(value for pair in candidates for value in pair if value)


def _project_phrase(text: str) -> str:
    match = re.search(r"\bproject\s+([A-Za-z0-9][A-Za-z0-9'_-]{1,63})\b", text, re.IGNORECASE)
    return _clean_text(match.group(1), 256) if match else ""


def _spelling_vocabulary(*groups: Sequence[str]) -> tuple[str, ...]:
    words = []
    for group in groups:
        for value in group:
            words.extend(token for token in _tokens(value) if len(token) >= 4)
    return tuple(sorted(set(words)))[:MAX_CATALOG_ITEMS]


def _spelling_variants(query: str, vocabulary: Sequence[str]) -> tuple[QueryVariant, ...]:
    variants = []
    for token in _tokens(query)[:32]:
        if len(token) < 4 or token in vocabulary or token in _STOPWORDS:
            continue
        maximum = 1 if len(token) <= 5 else 2
        candidates = []
        for known in vocabulary:
            if abs(len(token) - len(known)) > maximum or token[0] != known[0]:
                continue
            distance = _damerau_levenshtein(token, known, maximum)
            if distance <= maximum:
                candidates.append((distance, known))
        if candidates:
            distance, replacement = min(candidates)
            confidence = round(1.0 - distance / max(len(token), len(replacement)), 3)
            if confidence >= 0.72:
                variants.append(QueryVariant(token, replacement, "spelling_candidate", confidence))
    return tuple(variants[:3])


def _query_variants(
    query: str,
    *,
    topic: str,
    aliases: Sequence[QueryVariant],
    spelling: Sequence[QueryVariant],
) -> tuple[str, ...]:
    variants = []
    if aliases:
        variants.append(_apply_variants(query, aliases))
    if spelling:
        variants.append(_apply_variants(query, spelling))
    if aliases and spelling:
        variants.append(_apply_variants(_apply_variants(query, aliases), spelling))
    if topic:
        variants.append(f"{query} {topic}")
    return tuple(
        value
        for value in _dedupe(_clean_text(item, MAX_QUERY_CHARS) for item in variants)
        if value.casefold() != query.casefold()
    )[:MAX_QUERY_VARIANTS]


def _time_intent(query: str, now: datetime) -> TimeIntent:
    folded = query.casefold()
    if re.search(r"\b(?:conflict|conflicting|contested|contradiction)\b", folded):
        return TimeIntent("conflict", explicit=True)
    if re.search(r"\b(?:all versions|entire history)\b", folded):
        return TimeIntent("all", explicit=True)

    day = now.date()
    if "yesterday" in folded:
        target = day - timedelta(days=1)
        return TimeIntent("historical", as_of=_day_end(target, now), explicit=True)
    if "last week" in folded:
        start = day - timedelta(days=day.weekday() + 7)
        end = start + timedelta(days=6)
        return TimeIntent(
            "range", as_of=_day_end(end, now), since=_day_start(start, now), until=_day_end(end, now), explicit=True
        )
    if "this week" in folded:
        start = day - timedelta(days=day.weekday())
        return TimeIntent(
            "range", as_of=now.timestamp(), since=_day_start(start, now), until=now.timestamp(), explicit=True
        )
    if re.search(r"\b(?:tomorrow|next week|upcoming|future)\b", folded):
        return TimeIntent("future", explicit=True)
    if re.search(r"\b(?:now|today|current|currently|latest|active)\b", folded):
        return TimeIntent("current", explicit=True)

    date_match = _ISO_DATE.search(folded)
    if date_match:
        try:
            target = datetime.strptime(date_match.group(0), "%Y-%m-%d").date()
        except ValueError:
            target = None
        if target is not None:
            start = _day_start(target, now)
            end = _day_end(target, now)
            if re.search(r"\b(?:since|after)\b", folded):
                return TimeIntent("range", as_of=now.timestamp(), since=start, until=now.timestamp(), explicit=True)
            if re.search(r"\b(?:until|before)\b", folded):
                return TimeIntent("range", as_of=end, until=end, explicit=True)
            return TimeIntent("historical", as_of=end, explicit=True)

    if re.search(r"\b(?:before|earlier|former|historical|previous|previously|used to)\b", folded):
        return TimeIntent("historical", explicit=True)
    if re.search(r"\b(?:recent|recently|lately)\b", folded):
        return TimeIntent("recent", explicit=True)
    return TimeIntent("unspecified")


def _moment(value: datetime | float | None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if value is not None:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    return datetime.now().astimezone()


def _day_start(value, now: datetime) -> float:
    return datetime.combine(value, datetime_time.min, tzinfo=now.tzinfo).timestamp()


def _day_end(value, now: datetime) -> float:
    return datetime.combine(value, datetime_time.max, tzinfo=now.tzinfo).timestamp()


def _contains_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.IGNORECASE) is not None


def _replace_phrase(text: str, original: str, replacement: str) -> str:
    return re.sub(
        rf"(?<!\w){re.escape(original)}(?!\w)",
        lambda _match: replacement,
        text,
        flags=re.IGNORECASE,
    )


def _damerau_levenshtein(left: str, right: str, maximum: int) -> int:
    if left == right:
        return 0
    if abs(len(left) - len(right)) > maximum:
        return maximum + 1
    previous_previous: list[int] | None = None
    previous = list(range(len(right) + 1))
    for row_index, left_char in enumerate(left, start=1):
        current = [row_index]
        row_minimum = row_index
        for column_index, right_char in enumerate(right, start=1):
            cost = int(left_char != right_char)
            value = min(
                current[column_index - 1] + 1,
                previous[column_index] + 1,
                previous[column_index - 1] + cost,
            )
            if (
                previous_previous is not None
                and row_index > 1
                and column_index > 1
                and left_char == right[column_index - 2]
                and left[row_index - 2] == right_char
            ):
                value = min(value, previous_previous[column_index - 2] + 1)
            current.append(value)
            row_minimum = min(row_minimum, value)
        if row_minimum > maximum:
            return maximum + 1
        previous_previous, previous = previous, current
    return previous[-1]


def _dedupe(values) -> tuple[str, ...]:
    selected: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = str(value or "").strip()
        folded = clean.casefold()
        if clean and folded not in seen:
            seen.add(folded)
            selected.append(clean)
    return tuple(selected)


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


__all__ = [
    "QUERY_UNDERSTANDING_VERSION",
    "QueryPlan",
    "QueryVariant",
    "TimeIntent",
    "understand_query",
]
