from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from memory.feedback import classify_feedback
from memory.v2 import MemoryV2Store, source_receipt


def _source(text: str, locator: str) -> dict:
    return source_receipt(
        kind="chat_turn",
        locator=locator,
        source_text=text,
        session_id="feedback-test",
        surface="chat",
    )


def _add(store: MemoryV2Store, content: str) -> str:
    proposal = store.create_proposal(
        operation="add",
        candidate={
            "record_type": "semantic_fact",
            "content": content,
            "confidence": 0.9,
            "sensitivity": "personal",
        },
        source=_source(content, f"add:{content}"),
    )
    approved = store.approve_proposal(proposal["proposal_id"], reviewer="Raghav")
    return str(approved["applied_record_id"])


@pytest.mark.parametrize(
    ("words", "corrected", "kind", "rule"),
    [
        ("irrelevant", "", "relevance", "explicit_relevance"),
        ("that is not relevant", "", "relevance", "explicit_relevance"),
        ("wrong", "", "relevance", "bare_wrong_safe_default"),
        ("wrong", "the port is 9000", "relevance", "bare_wrong_safe_default"),
        ("that fact is wrong", "", "factual_correction", "explicit_factual"),
        ("wrong, the port is 9000", "the port is 9000", "factual_correction", "corrected_content"),
        ("undo that relevance feedback", "", "revoke", "explicit_revoke"),
    ],
)
def test_explicit_feedback_language_is_classified_safely(
    words: str,
    corrected: str,
    kind: str,
    rule: str,
) -> None:
    intent = classify_feedback(words, corrected_content=corrected)

    assert intent is not None
    assert intent.kind == kind
    assert intent.rule == rule


def test_relevance_feedback_is_receipt_bound_private_reversible_and_affects_ranking(
    tmp_path,
) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    _add(store, "Atlas deployment channel uses blue.")
    _add(store, "Atlas deployment channel uses green.")
    query = "Atlas deployment channel color"
    initial = store.retrieve_with_receipt(query, limit=2, surface="private")
    initial_ids = [item["record"]["record_id"] for item in initial["hits"]]
    bad_id, expected_id = initial_ids

    feedback = store.record_relevance_feedback(
        initial["receipt"]["receipt_id"],
        bad_id,
        reason="that result is irrelevant",
        source=_source("that result is irrelevant", "feedback:1"),
    )

    assert feedback["kind"] == "relevance"
    assert feedback["state"] == "active"
    assert feedback["record_id"] == bad_id
    assert feedback["query_sha256"] == initial["receipt"]["query_sha256"]
    assert store.get_record(bad_id) is not None

    reranked = store.retrieve_with_receipt(query, limit=2, surface="private")
    assert reranked["hits"][0]["record"]["record_id"] == expected_id
    penalized = next(item for item in reranked["hits"] if item["record"]["record_id"] == bad_id)
    assert penalized["components"]["negative_feedback_penalty"] == 0.12
    assert any(reason.startswith("relevance_feedback:") for reason in penalized["reasons"])
    assert reranked["receipt"]["filters"]["active_feedback_count"] == 1

    report = store.evaluate_retrieval(
        [
            {
                "name": "known irrelevant result",
                "query": query,
                "surface": "private",
                "expected_record_ids": [expected_id],
            }
        ],
        limit=1,
    )
    assert report["feedback_case_count"] == 1
    assert report["false_positive_rate"] == 0.0
    assert report["cases"][0]["negative_record_ids"] == [bad_id]
    assert report["cases"][0]["negative_feedback_ids"] == [feedback["feedback_id"]]

    with sqlite3.connect(store.path) as connection:
        persisted = "".join(
            str(value or "")
            for value in connection.execute(
                "SELECT reason_sha256, source_json, query_sha256 "
                "FROM memory_retrieval_feedback WHERE feedback_id = ?",
                (feedback["feedback_id"],),
            ).fetchone()
        ).casefold()
    assert query.casefold() not in persisted
    assert "that result is irrelevant" not in persisted

    revoked = store.revoke_relevance_feedback(
        feedback["feedback_id"],
        source=_source("undo that relevance feedback", "feedback:revoke"),
    )
    assert revoked["state"] == "revoked"
    assert store.retrieval_feedback(state="active", kind="relevance") == []
    restored = store.retrieve_with_receipt(query, limit=2, surface="private")
    assert restored["hits"][0]["record"]["record_id"] == bad_id


def test_factual_feedback_creates_a_reviewable_proposal_without_mutating_memory(
    tmp_path,
) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    record_id = _add(store, "Atlas deployment port is 7000.")
    receipt = store.retrieve_with_receipt("Atlas deployment port", limit=1)

    result = store.propose_factual_correction(
        receipt["receipt"]["receipt_id"],
        record_id,
        corrected_content="Atlas deployment port is 9000.",
        reason="that fact is wrong",
        source=_source("that fact is wrong; it is 9000", "feedback:fact"),
    )

    assert store.get_record(record_id).content == "Atlas deployment port is 7000."  # type: ignore[union-attr]
    assert result["feedback"]["kind"] == "factual_correction"
    assert result["feedback"]["proposal_id"] == result["proposal"]["proposal_id"]
    assert result["proposal"]["state"] == "proposed"
    assert result["proposal"]["operation"] == "update"
    assert result["proposal"]["diff"]["after"]["content"] == "Atlas deployment port is 9000."
    assert store.retrieval_feedback(kind="relevance") == []

    reranked = store.retrieve_with_receipt("Atlas deployment port", limit=1)
    assert reranked["hits"][0]["components"]["negative_feedback_penalty"] == 0.0
    with pytest.raises(RuntimeError, match="reviewed through their proposal"):
        store.revoke_relevance_feedback(
            result["feedback"]["feedback_id"],
            source=_source("undo that correction", "feedback:bad-revoke"),
        )


def test_feedback_rejects_records_that_were_not_returned_by_the_receipt(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    returned_id = _add(store, "Atlas deployment port is 7000.")
    other_id = _add(store, "Jenna prefers tea.")
    receipt = store.retrieve_with_receipt("Atlas deployment port", limit=1)
    assert receipt["hits"][0]["record"]["record_id"] == returned_id

    with pytest.raises(ValueError, match="was not returned"):
        store.record_relevance_feedback(
            receipt["receipt"]["receipt_id"],
            other_id,
            source=_source("irrelevant", "feedback:invalid-target"),
        )


def test_feedback_tools_are_exposed_with_separate_operations() -> None:
    from core.brain_memory_tools import MEMORY_TOOL_NAMES

    names = {name.rsplit("__", 1)[-1] for name in MEMORY_TOOL_NAMES}
    assert {
        "record_memory_feedback",
        "list_memory_feedback",
        "revoke_memory_feedback",
    }.issubset(names)


def test_feedback_tool_uses_grounded_user_language_and_persisted_receipt(
    monkeypatch,
    tmp_path,
) -> None:
    from core import brain_laptop_tools, brain_memory_tools

    path = tmp_path / "memory.sqlite3"
    monkeypatch.setenv("SERENA_MEMORY_V2_DB_PATH", str(path))
    monkeypatch.setattr(brain_laptop_tools, "_RECENT_TURNS", [])
    monkeypatch.setattr(
        brain_memory_tools,
        "authorize",
        lambda *_args, **_kwargs: SimpleNamespace(allowed=True, reason="authorized"),
    )

    async def immediate(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(brain_memory_tools.asyncio, "to_thread", immediate)
    store = MemoryV2Store(path)
    record_id = _add(store, "Atlas deployment channel uses blue.")
    receipt = store.retrieve_with_receipt("Atlas deployment channel", limit=1)
    previous = brain_laptop_tools.set_current_turn(
        {
            "text": "tell me about Atlas",
            "protocol": "voice",
            "call_id": "call-feedback",
            "turn_id": "call-feedback:0",
        }
    )
    brain_laptop_tools.reset_current_turn(previous)
    token = brain_laptop_tools.set_current_turn(
        {
            "text": "wrong",
            "protocol": "voice",
            "call_id": "call-feedback",
            "turn_id": "call-feedback:1",
        }
    )
    try:
        response = asyncio.run(
            brain_memory_tools.record_memory_feedback.handler(
                {
                    "receipt_id": receipt["receipt"]["receipt_id"],
                    "record_id": record_id,
                    "corrected_content": "Atlas deployment channel uses green.",
                    "reason": "",
                }
            )
        )
    finally:
        brain_laptop_tools.reset_current_turn(token)

    assert response["content"][0]["text"].startswith("RELEVANCE FEEDBACK RECORDED")
    feedback = store.retrieval_feedback(kind="relevance")
    assert len(feedback) == 1
    assert feedback[0]["record_id"] == record_id
    assert store.proposals() == []
    assert store.get_record(record_id).content == "Atlas deployment channel uses blue."  # type: ignore[union-attr]
    with sqlite3.connect(path) as connection:
        source = json.loads(
            connection.execute(
                "SELECT source_json FROM memory_retrieval_feedback WHERE feedback_id = ?",
                (feedback[0]["feedback_id"],),
            ).fetchone()[0]
        )
    assert source["feedback_classifier_version"] == "retrieval-feedback-intent-v2"
    assert source["feedback_classifier_rule"] == "bare_wrong_safe_default"
