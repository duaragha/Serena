from __future__ import annotations

import json
import sqlite3
import time
from itertools import count

from memory.reranker import (
    MAX_RERANK_CANDIDATES,
    RANKING_VERSION,
    CandidateSignals,
    rerank_candidates,
    resolve_temporal_intent,
)
from memory.v2 import MemoryRecord, MemoryV2Store, source_receipt

_SOURCE_IDS = count(1)


def _source(text: str) -> dict:
    return source_receipt(
        kind="chat_turn",
        locator=f"test:{next(_SOURCE_IDS)}",
        source_text=text,
        session_id="reranker-test",
        surface="chat",
    )


def _add(
    store: MemoryV2Store,
    content: str,
    *,
    record_type: str = "semantic_fact",
    confidence: float = 0.9,
    sensitivity: str = "personal",
    project: str = "",
    people: tuple[str, ...] = (),
    valid_from: float | None = None,
    valid_until: float | None = None,
) -> str:
    proposal = store.create_proposal(
        operation="add",
        candidate={
            "record_type": record_type,
            "content": content,
            "confidence": confidence,
            "sensitivity": sensitivity,
            "project": project,
            "people": list(people),
            "valid_from": valid_from,
            "valid_until": valid_until,
        },
        source=_source(content),
    )
    approved = store.approve_proposal(proposal["proposal_id"], reviewer="Raghav")
    return str(approved["applied_record_id"])


def _set_updated_at(store: MemoryV2Store, record_ids: tuple[str, ...], updated_at: float) -> None:
    with sqlite3.connect(store.path) as connection:
        connection.executemany(
            "UPDATE memory_records SET updated_at = ? WHERE record_id = ?",
            [(updated_at, record_id) for record_id in record_ids],
        )


def _rank_record(
    record_id: str,
    *,
    content: str,
    record_type: str = "semantic_fact",
) -> MemoryRecord:
    return MemoryRecord(
        record_id=record_id,
        record_type=record_type,
        content=content,
        project="",
        people=(),
        source={},
        confidence=1.0,
        sensitivity="personal",
        valid_from=None,
        valid_until=None,
        retention_until=None,
        status="current",
        legacy_id=None,
        legacy_type=None,
        created_at=1_000.0,
        updated_at=1_000.0,
        forgotten_at=None,
    )


def test_current_corrections_and_active_state_outrank_stale_recall(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    now = time.time()
    stale_id = _add(
        store,
        "Atlas deployment port for Raghav is 7000.",
        confidence=1.0,
        project="Atlas",
        people=("Raghav",),
    )
    correction_id = _add(
        store,
        "Correction: Atlas deployment port for Raghav is now 8000.",
        record_type="correction",
        project="Atlas",
        people=("Raghav",),
    )
    active_id = _add(
        store,
        "Active Atlas migration ledger tracks deployment port 9000 for Raghav.",
        record_type="commitment",
        project="Atlas",
        people=("Raghav",),
    )
    _set_updated_at(store, (stale_id,), now - 5 * 365 * 86_400)

    hits = store.retrieve(
        "current Atlas deployment port for Raghav",
        project="Atlas",
        people=("Raghav",),
        entities=("Atlas",),
        active_record_ids=(active_id,),
        now=now,
    )

    order = [hit.record.record_id for hit in hits]
    assert order.index(correction_id) < order.index(stale_id)
    assert order.index(active_id) < order.index(stale_id)
    correction = next(hit for hit in hits if hit.record.record_id == correction_id)
    active = next(hit for hit in hits if hit.record.record_id == active_id)
    assert correction.components["correction"] > 0
    assert correction.components["project_raw"] == 1.0
    assert correction.components["people_raw"] == 1.0
    assert correction.components["entity_raw"] == 1.0
    assert "correction_priority" in correction.reasons
    assert "active_state_priority" in active.reasons


def test_temporal_intent_controls_superseded_and_contested_records(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    old_id = _add(store, "Atlas service used port 7000 before the migration.")
    proposal = store.create_proposal(
        operation="supersede",
        target_record_id=old_id,
        candidate={
            "record_type": "semantic_fact",
            "content": "Atlas service currently uses port 8000 after migration.",
            "confidence": 0.95,
            "sensitivity": "personal",
        },
        source=_source("Atlas service currently uses port 8000 after migration."),
    )
    approved = store.approve_proposal(proposal["proposal_id"], reviewer="Raghav")
    current_id = str(approved["applied_record_id"])

    current_hits = store.retrieve("which port does Atlas currently use")
    historical_hits = store.retrieve("which port did Atlas use before")

    assert old_id not in {hit.record.record_id for hit in current_hits}
    assert current_id in {hit.record.record_id for hit in current_hits}
    assert historical_hits[0].record.record_id == old_id
    assert historical_hits[0].record.status == "superseded"

    conflict = store.create_proposal(
        operation="contradict",
        target_record_id=current_id,
        candidate={
            "record_type": "correction",
            "content": "Atlas may instead use port 8100; this is contested.",
            "confidence": 0.6,
            "sensitivity": "personal",
        },
        source=_source("Atlas may instead use port 8100; this is contested."),
    )
    store.approve_proposal(conflict["proposal_id"], reviewer="Raghav")

    assert store.retrieve("Atlas port") == []
    contested = store.retrieve(
        "contested conflict about the Atlas port", include_superseded=False
    )
    assert contested
    assert {hit.record.status for hit in contested} == {"contested"}


def test_explicit_current_language_wins_over_ambiguous_used_term() -> None:
    assert resolve_temporal_intent("what database is currently used by Serena") == "current"
    assert resolve_temporal_intent("what database was used before Serena") == "historical"


def test_as_of_validity_and_type_specific_recency_are_explainable(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    now = time.time()
    expired_id = _add(
        store,
        "Atlas archived release window.",
        record_type="episode",
        valid_from=now - 100,
        valid_until=now - 10,
    )
    current_id = _add(store, "Atlas current release window.", record_type="episode")
    future_id = _add(
        store,
        "Atlas future release window.",
        record_type="episode",
        valid_from=now + 10,
    )
    preference_id = _add(
        store,
        "Atlas preference tracks the green deployment channel.",
        record_type="preference",
    )
    commitment_id = _add(
        store,
        "Atlas commitment tracks the production deployment channel.",
        record_type="commitment",
    )
    _set_updated_at(store, (preference_id, commitment_id), now - 365 * 86_400)

    current = {hit.record.record_id for hit in store.retrieve("Atlas release window", now=now)}
    historical = {
        hit.record.record_id
        for hit in store.retrieve(
            "Atlas release window before",
            temporal_intent="historical",
            as_of=now - 50,
            now=now,
        )
    }
    recency_hits = {
        hit.record.record_id: hit
        for hit in store.retrieve("Atlas deployment channel", now=now, limit=10)
    }

    assert current_id in current
    assert expired_id not in current
    assert future_id not in current
    assert expired_id in historical
    assert future_id not in historical
    assert recency_hits[preference_id].components["recency"] > recency_hits[commitment_id].components[
        "recency"
    ]


def test_superseding_replacement_starts_at_approval_time(tmp_path, monkeypatch) -> None:
    clock = {"now": 1_000.0}
    monkeypatch.setattr("memory.v2.time.time", lambda: clock["now"])
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    old_id = _add(store, "Atlas uses port 7000.")
    clock["now"] = 2_000.0
    proposal = store.create_proposal(
        operation="supersede",
        target_record_id=old_id,
        candidate={
            "record_type": "semantic_fact",
            "content": "Atlas uses port 8000.",
            "confidence": 0.95,
            "sensitivity": "personal",
        },
        source=_source("Atlas uses port 8000."),
    )
    clock["now"] = 3_000.0
    approved = store.approve_proposal(proposal["proposal_id"], reviewer="Raghav")
    current_id = str(approved["applied_record_id"])

    before = store.retrieve(
        "Atlas port",
        temporal_intent="historical",
        as_of=2_500.0,
        now=4_000.0,
    )
    after = store.retrieve(
        "Atlas port",
        temporal_intent="historical",
        as_of=3_500.0,
        now=4_000.0,
    )

    assert {hit.record.record_id for hit in before} == {old_id}
    assert current_id in {hit.record.record_id for hit in after}
    assert store.get_record(current_id).valid_from == 3_000.0  # type: ignore[union-attr]


def test_deduplication_diversification_and_receipt_components(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    first_id = _add(store, "Atlas deployment status is green.", project="Atlas")
    duplicate_id = _add(store, "Atlas deployment status is green.", project="Atlas")
    second_id = _add(store, "Atlas deployment health remains stable.", project="Atlas")

    result = store.retrieve_with_receipt(
        "Atlas deployment status",
        project="Atlas",
        people=("Raghav",),
        limit=10,
    )

    returned_ids = [item["record"]["record_id"] for item in result["hits"]]
    assert len({first_id, duplicate_id}.intersection(returned_ids)) == 1
    assert second_id in returned_ids
    assert result["receipt"]["ranking_version"] == RANKING_VERSION
    assert result["receipt"]["filters"]["deduplicated_count"] == 1
    assert result["receipt"]["filters"]["candidate_limit"] == MAX_RERANK_CANDIDATES
    assert result["receipt"]["filters"]["backend"] == "deterministic_local_fallback"
    assert result["receipt"]["filters"]["people_count"] == 1
    assert "Raghav" not in json.dumps(result["receipt"]["filters"])
    assert all("components" in item for item in result["receipt"]["returned"])
    assert any(item["components"]["diversity_penalty"] > 0 for item in result["hits"][1:])


def test_peer_candidate_scores_use_dense_signal_with_deterministic_fallback(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    dense_id = _add(store, "The preferred editor is Helix.")

    assert store.retrieve("quasar nebula") == []
    result = store.retrieve_with_receipt(
        "quasar nebula",
        candidate_scores={dense_id: {"dense_score": 0.93}},
    )

    assert result["hits"][0]["record"]["record_id"] == dense_id
    assert result["hits"][0]["semantic_score"] == 0.93
    assert result["receipt"]["filters"]["backend"] == "hybrid_candidates"
    assert any(
        reason.startswith("dense_semantic:") for reason in result["hits"][0]["reasons"]
    )


def test_invalid_dense_scores_fail_to_deterministic_fallback(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    record_id = _add(store, "The preferred editor is Helix.")

    for invalid in (None, float("nan"), float("inf"), "not-a-score"):
        result = store.retrieve_with_receipt(
            "quasar nebula",
            candidate_scores={record_id: {"dense_score": invalid}},
        )
        assert result["hits"] == []
        assert result["receipt"]["filters"]["backend"] == "deterministic_local_fallback"
        assert result["receipt"]["filters"]["fallback_reason"] == "invalid_dense_scores"
        assert result["receipt"]["filters"]["invalid_dense_count"] == 1


def test_short_entity_aliases_require_token_phrase_boundaries(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    unrelated_id = _add(store, "The office chair is comfortable.")
    matching_id = _add(store, "The local AI model runs offline.")

    hits = store.retrieve("quasar nebula", entities=("AI",), limit=10)
    returned = {hit.record.record_id for hit in hits}

    assert matching_id in returned
    assert unrelated_id not in returned


def test_active_and_correction_candidates_survive_the_candidate_bound() -> None:
    stale = [
        CandidateSignals(
            record=_rank_record(f"stale-{index}", content=f"stale memory {index}"),
            literal=1.0,
            semantic=1.0,
        )
        for index in range(MAX_RERANK_CANDIDATES)
    ]
    correction = CandidateSignals(
        record=_rank_record(
            "correction",
            content="corrected memory",
            record_type="correction",
        ),
        literal=0.01,
    )
    active = CandidateSignals(
        record=_rank_record("active", content="active memory"),
        literal=0.01,
    )

    result = rerank_candidates(
        [*stale, correction, active],
        query="memory",
        now=1_000.0,
        limit=MAX_RERANK_CANDIDATES,
        active_record_ids=("active",),
    )
    returned = {ranked.candidate.record.record_id for ranked in result.ranked}

    assert result.bounded_count == MAX_RERANK_CANDIDATES
    assert "correction" in returned
    assert "active" in returned


def test_direct_reranker_rejects_non_finite_candidate_signals() -> None:
    candidate = CandidateSignals(
        record=_rank_record("invalid", content="unrelated memory"),
        semantic=float("nan"),
    )

    result = rerank_candidates([candidate], query="quasar", now=1_000.0, limit=1)

    assert result.ranked == ()
    assert result.filtered_counts == {"no_relevance": 1}
