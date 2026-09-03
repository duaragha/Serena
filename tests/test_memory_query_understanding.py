from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from memory.query_understanding import understand_query
from memory.retrieval import retrieve_memory


def test_query_plan_is_deterministic_bounded_and_receipt_safe() -> None:
    now = datetime(2026, 8, 27, 14, 30, tzinfo=timezone.utc)
    context = [
        "discarded context zero",
        "discarded context one",
        "we discussed something else",
        "Jenna owns the Atlas release",
        "Project Atlas rollout is blocked",
        "Jenna asked about the Atlas rollout",
    ]
    kwargs = {
        "recent_context": context,
        "known_people": ("Jenna",),
        "known_projects": ("Atlas",),
        "now": now,
    }

    first = understand_query("What about that rollout yesterday?", **kwargs)
    second = understand_query("What about that rollout yesterday?", **kwargs)

    assert first == second
    assert first.context_turn_count == 4
    assert first.people == ("Jenna",)
    assert first.project == "Atlas"
    assert first.time_intent.kind == "historical"
    assert datetime.fromtimestamp(first.as_of or 0, tz=timezone.utc).date().isoformat() == "2026-08-26"
    assert len(first.to_dict()["profile_sha256"]) == 64
    assert "bounded_recent_user_context" in first.rules_fired
    assert "deictic_context_enrichment" in first.rules_fired

    durable = json.dumps(first.to_dict(), sort_keys=True).casefold()
    for raw in ("jenna", "atlas", "rollout", "discarded context"):
        assert raw not in durable
    assert first.retrieval_values()["people"] == ("Jenna",)


def test_alias_and_typo_variants_preserve_the_original_query() -> None:
    plan = understand_query(
        "show phev rolluot prefs",
        known_projects=("PHEV Tracker",),
        known_entities=("rollout",),
        aliases={"phev": "PHEV Tracker"},
        now=0.0,
    )

    assert plan.normalized_query == "show phev rolluot prefs"
    assert plan.project == "PHEV Tracker"
    assert any(item.original == "phev" and item.replacement == "PHEV Tracker" for item in plan.aliases)
    assert any(item.original == "rolluot" and item.replacement == "rollout" for item in plan.spelling_variants)
    assert plan.likely_record_types == ("preference",)
    assert "show phev rolluot prefs" not in plan.query_variants
    assert any("PHEV Tracker" in variant for variant in plan.query_variants)
    assert any("rollout" in variant for variant in plan.query_variants)


def test_people_time_and_record_type_are_inspectable() -> None:
    plan = understand_query(
        "What was Jenna's phone number before 2026-08-05?",
        known_people=("Jenna",),
        now=datetime(2026, 8, 27, tzinfo=timezone.utc),
    )

    assert plan.people == ("Jenna",)
    assert plan.time_intent.kind == "range"
    assert plan.time_intent.until == plan.as_of
    assert plan.temporal_intent == "historical"
    assert plan.likely_record_types == ("semantic_fact",)

    inferred = understand_query("What is Jenna's phone number?", now=0.0)
    assert inferred.people == ("Jenna",)
    assert "Jenna" not in inferred.entities


def test_previous_user_turn_is_bound_to_the_current_turn(monkeypatch) -> None:
    from core import brain_laptop_tools

    monkeypatch.setattr(brain_laptop_tools, "_RECENT_TURNS", [])
    first = brain_laptop_tools.set_current_turn(
        {
            "text": "tell me about Atlas",
            "protocol": "voice",
            "call_id": "call-1",
            "turn_id": "call-1:1",
        }
    )
    brain_laptop_tools.reset_current_turn(first)
    second = brain_laptop_tools.set_current_turn(
        {
            "text": "what about that rollout",
            "protocol": "voice",
            "call_id": "call-1",
            "turn_id": "call-1:2",
        }
    )
    try:
        assert brain_laptop_tools.previous_user_turn_text() == "tell me about Atlas"
    finally:
        brain_laptop_tools.reset_current_turn(second)


def test_previous_user_turn_does_not_cross_call_boundaries(monkeypatch) -> None:
    from core import brain_laptop_tools

    monkeypatch.setattr(brain_laptop_tools, "_RECENT_TURNS", [])
    first = brain_laptop_tools.set_current_turn(
        {
            "text": "Jenna owns the Atlas rollout",
            "protocol": "voice",
            "call_id": "call-1",
            "turn_id": "call-1:1",
        }
    )
    brain_laptop_tools.reset_current_turn(first)
    second = brain_laptop_tools.set_current_turn(
        {
            "text": "what about that?",
            "protocol": "voice",
            "call_id": "call-2",
            "turn_id": "call-2:1",
        }
    )
    try:
        assert brain_laptop_tools.previous_user_turn_text() == ""
    finally:
        brain_laptop_tools.reset_current_turn(second)


def test_canonical_legacy_retrieval_uses_bounded_context_without_persisting_it(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "memory.v2.MemoryV2Store.authority_is_active",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "memory.store.list_memories",
        lambda *_args, **_kwargs: [
            {
                "id": 7,
                "type": "feedback",
                "content": "Raghav dislikes the automatic yeah backchannel.",
                "filename": "007-backchannel.md",
            }
        ],
    )

    result = retrieve_memory(
        "what about that?",
        recent_context=("we were discussing the automatic yeah backchannel",),
    )

    assert [hit.record_id for hit in result.hits] == ["legacy:feedback:7"]
    receipt = json.dumps(result.receipt, sort_keys=True).casefold()
    assert "automatic yeah backchannel" not in receipt
    assert result.receipt["query_understanding"]["context_turn_count"] == 1


def test_active_v2_persists_only_the_safe_query_plan(monkeypatch, tmp_path) -> None:
    from memory.v2 import MemoryV2Store, source_receipt

    path = tmp_path / "memory.sqlite3"
    monkeypatch.setenv("SERENA_MEMORY_V2_DB_PATH", str(path))
    store = MemoryV2Store(path)
    proposal = store.create_proposal(
        operation="add",
        candidate={
            "record_type": "preference",
            "content": "Raghav prefers concise Atlas updates.",
        },
        source=source_receipt(
            kind="test",
            locator="query-plan:test",
            source_text="Raghav prefers concise Atlas updates.",
        ),
    )
    store.approve_proposal(proposal["proposal_id"], reviewer="Raghav")
    store.activate_authority(actor="Raghav")

    result = retrieve_memory(
        "what about those updates?",
        recent_context=("we were discussing Raghav's private Atlas preferences",),
    )
    persisted = store.retrieval_receipts()[0]["filters"]["query_understanding"]

    assert result.authority == "memory-v2"
    assert persisted["context_turn_count"] == 1
    assert persisted["version"].startswith("deterministic-query-")
    encoded = json.dumps(persisted, sort_keys=True).casefold()
    for raw in ("raghav", "atlas", "private", "preferences"):
        assert raw not in encoded


def test_frontdoor_passes_only_prior_user_messages_as_recent_context() -> None:
    from core.frontdoor import _recent_user_queries

    history = [
        {"role": "user", "text": "tell me about Atlas"},
        {"role": "assistant", "text": "model-generated text must not become query context"},
        {"role": "user", "text": "what about that rollout?"},
    ]

    assert _recent_user_queries(history) == ("tell me about Atlas",)
    assert _recent_user_queries(history, limit=0) == ()


def test_frontdoor_does_not_fall_back_to_process_global_turn_text(monkeypatch) -> None:
    from core import brain_daemon

    captured: dict = {}

    def fake_pack(_query: str, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(text="packed")

    monkeypatch.setattr(
        "core.brain_laptop_tools.previous_user_turn_text",
        lambda: "assistant-bearing rendered prompt",
    )
    monkeypatch.setattr("memory.retrieval.pack_memory_context", fake_pack)

    assert brain_daemon._memory_context_block("Atlas status", "frontdoor") == "packed"
    assert captured["recent_context"] == ()
