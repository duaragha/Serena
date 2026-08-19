"""Serena can reach everything she knows, not just what fits in her context.

Only active tasks, loops and ledgers are injected each turn, which left the
other memories and the whole knowledge base unreachable: asked about something
she had researched, she said she had nothing, and she was not lying, she simply
could not look.

These pin the retrieval quality too. Out loud, a confident wrong answer is
worse than a miss, so a query whose distinctive word matches nothing has to
come back empty rather than returning whatever shared a filler word.
"""

from __future__ import annotations

import pytest

from core.brain_tools import (
    BRAIN_TOOL_NAMES,
    _label,
    _matcher,
    _search_knowledge,
    _search_memory,
    _terms,
    _weights,
)


@pytest.fixture(autouse=True)
def _legacy_authority_by_default(monkeypatch, request):
    if request.node.name != "test_active_v2_is_the_normal_search_authority_with_a_receipt":
        monkeypatch.setattr("memory.v2.MemoryV2Store.authority_is_active", lambda *args: False)


def test_the_recall_tools_are_exposed_to_the_brain() -> None:
    names = {name.rsplit("__", 1)[-1] for name in BRAIN_TOOL_NAMES}
    assert {"search_memory", "search_knowledge", "read_knowledge"} <= names


@pytest.mark.parametrize(
    "query,expected",
    [
        ("what does he think about the phev tracker", ["think", "phev", "tracker"]),
        ("HOW IS THE Battery", ["battery"]),
        ("the and of", ["the", "and", "of"]),  # nothing left, so keep it all
    ],
)
def test_filler_words_are_dropped_but_never_everything(query, expected) -> None:
    assert _terms(query) == expected


def test_short_terms_must_match_whole_words() -> None:
    """A bare "bar" inside "barcode" is not a hit."""
    assert _matcher("bar")("the type bar is nice") is True
    assert _matcher("bar")("scan the barcode") is False
    # Longer terms stay substrings: "the phev tracker" must reach full_tracker.
    assert _matcher("tracker")("src/full_tracker/app.py") is True


def test_a_term_in_everything_carries_less_weight_than_a_rare_one() -> None:
    corpus = ["the type of thing", "type it out", "typing types", "phev battery"]
    weights = _weights(["type", "phev"], corpus)
    assert weights["phev"] > weights["type"] > 0


def test_a_query_that_matches_nothing_says_so(monkeypatch) -> None:
    monkeypatch.setattr(
        "memory.store.list_memories",
        lambda *a, **k: [
            {"id": 1, "type": "project", "content": "Google Ads item_type_keyword "
             "mapping for the wall art rollout", "updated_at": "2026-07-01"},
        ],
    )
    out = _search_memory("outlander regenerative braking")
    assert "nothing in memory" in out


def test_a_shared_filler_word_is_not_an_answer(monkeypatch) -> None:
    """The live failure: asking about the "type bar" returned a Google Ads note
    because it happened to contain the word "type"."""
    monkeypatch.setattr(
        "memory.store.list_memories",
        lambda *a, **k: [
            {"id": 1, "type": "project", "content": "Ads mapping: item_type_keyword, "
             "recommended_rooms, and the other type fields", "updated_at": "2026-07-01"},
            {"id": 2, "type": "project", "content": "type checking with mypy is on "
             "for this repo", "updated_at": "2026-07-02"},
            {"id": 3, "type": "reference", "content": "the type of coffee he likes",
             "updated_at": "2026-07-03"},
        ],
    )
    assert "nothing in memory" in _search_memory("type bar")


def test_a_real_match_is_returned_with_its_id_and_type(monkeypatch) -> None:
    monkeypatch.setattr(
        "memory.store.list_memories",
        lambda *a, **k: [
            {"id": 7, "type": "feedback", "content": "Raghav dislikes the automatic "
             "yeah backchannel before every answer", "updated_at": "2026-07-01"},
            {"id": 8, "type": "project", "content": "unrelated shopify metafields",
             "updated_at": "2026-07-02"},
        ],
    )
    out = _search_memory("backchannel")
    assert "[7]" in out and "(feedback)" in out
    assert "shopify" not in out


def test_active_v2_is_the_normal_search_authority_with_a_receipt(
    monkeypatch, tmp_path
) -> None:
    from memory.v2 import MemoryV2Store, source_receipt

    path = tmp_path / "memory-v2.sqlite3"
    monkeypatch.setenv("SERENA_MEMORY_V2_DB_PATH", str(path))
    store = MemoryV2Store(path)
    proposal = store.propose_candidate(
        content="Raghav prefers durable compact memory tools.",
        record_type="preference",
        source=source_receipt(
            kind="test", locator="test:brain-search", source_text="durable compact"
        ),
    )
    store.approve_proposal(proposal["proposal_id"], reviewer="Raghav")
    store.activate_authority(actor="Raghav")

    out = _search_memory("compact memory")

    assert "durable compact memory tools" in out
    assert "retrieval receipt:" in out
    assert len(store.retrieval_receipts()) == 1


def test_knowledge_search_finds_the_topic_that_is_about_the_thing() -> None:
    out = _search_knowledge("outlander phev")
    assert "outlander-phev" in out


def test_an_empty_knowledge_query_lists_what_is_there() -> None:
    out = _search_knowledge("")
    assert "knowledge topics" in out


def test_a_slug_shaped_title_is_not_echoed_twice() -> None:
    assert _label({"slug": "voice-dictation", "title": "voice-dictation"}) == (
        "voice-dictation"
    )
    assert _label(
        {"slug": "linux-bluetooth", "title": "Linux Bluetooth & Audio", "description": ""}
    ) == "linux-bluetooth: Linux Bluetooth & Audio"
