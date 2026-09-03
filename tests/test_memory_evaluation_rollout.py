from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from memory.evaluation import (
    RegressionCase,
    RegressionCorpus,
    evaluate_regression_corpus,
    load_regression_corpus,
    write_evaluation_report,
    write_regression_corpus,
)
from memory.hybrid import EmbeddingMetadata
from memory.retrieval import retrieve_memory
from memory.rollout import (
    RetrievalRolloutController,
    evaluate_shadow_database,
    shadow_migrate_legacy,
)
from memory.v2 import MemoryV2Store


class SmallProvider:
    def __init__(self) -> None:
        self._metadata = EmbeddingMetadata(
            model_id="rollout-fixture",
            model_version="v1",
            model_sha256="c" * 64,
            dimensions=2,
            normalized=True,
            runtime="test",
        )

    @property
    def metadata(self):
        return self._metadata

    @staticmethod
    def encode_query(_text):
        return (1.0, 0.0)

    @staticmethod
    def encode_documents(texts):
        return [(1.0, 0.0) for _text in texts]


def _corpus() -> RegressionCorpus:
    return RegressionCorpus(
        corpus_id="private-memory-v1",
        cases=(
            RegressionCase(
                case_id="positive",
                query="Atlas deployment channel",
                expected_record_ids=("atlas",),
            ),
            RegressionCase(
                case_id="negative",
                query="nonexistent zephyr answer",
                expect_no_answer=True,
            ),
        ),
    )


def test_private_jsonl_corpus_round_trips_and_report_omits_raw_queries(tmp_path) -> None:
    corpus_path = write_regression_corpus(tmp_path / "corpus.jsonl", _corpus())
    loaded = load_regression_corpus(corpus_path)

    def retrieve(case, _limit):
        hits = (
            [{"record": {"record_id": "atlas", "content": "green channel"}}]
            if case.case_id == "positive"
            else []
        )
        return {"hits": hits, "receipt": {"receipt_id": f"receipt-{case.case_id}"}}

    report = evaluate_regression_corpus(
        loaded,
        retrieve,
        retrieval_version="test-retrieval",
        ranking_version="test-ranking",
        now=123.0,
    )
    report_path = write_evaluation_report(tmp_path / "report.json", report)
    encoded = report_path.read_text(encoding="utf-8")

    assert report.recall_at_k == 1.0
    assert report.mean_reciprocal_rank == 1.0
    assert report.precision_at_k == 1.0
    assert report.false_positive_rate == 0.0
    assert report.no_answer_accuracy == 1.0
    assert report.context_budget_pass_rate == 1.0
    assert "Atlas deployment channel" not in encoded
    assert "nonexistent zephyr answer" not in encoded
    assert json.loads(encoded)["corpus_sha256"] == loaded.sha256
    if hasattr(corpus_path.stat(), "st_mode"):
        assert corpus_path.stat().st_mode & 0o077 == 0


def test_negative_false_positive_and_context_flooding_are_measured() -> None:
    corpus = RegressionCorpus(
        corpus_id="failure-corpus",
        cases=(
            RegressionCase(
                case_id="positive",
                query="Atlas",
                expected_record_ids=("atlas",),
                max_context_records=1,
                max_context_characters=20,
                max_context_tokens=5,
            ),
            RegressionCase(
                case_id="negative",
                query="nothing",
                expect_no_answer=True,
            ),
        ),
    )

    def retrieve(_case, _limit):
        return {
            "hits": [
                {"record": {"record_id": "atlas", "content": "x" * 100}},
                {"record": {"record_id": "flood", "content": "y" * 100}},
            ]
        }

    report = evaluate_regression_corpus(
        corpus,
        retrieve,
        retrieval_version="test",
        ranking_version="test",
    )

    assert report.false_positive_rate == 1.0
    assert report.no_answer_accuracy == 0.0
    assert report.context_budget_pass_rate == 0.5
    assert report.flooding_rate == 0.5


def test_shadow_migration_enriches_isolated_db_without_touching_live_path(
    tmp_path, monkeypatch
) -> None:
    live = tmp_path / "live.sqlite3"
    MemoryV2Store(live)
    before = hashlib.sha256(live.read_bytes()).hexdigest()
    monkeypatch.setenv("SERENA_MEMORY_V2_DB_PATH", str(live))
    candidate = tmp_path / "candidate.sqlite3"
    records = [
        {
            "id": 1,
            "type": "project",
            "content": "Atlas deploys through the green channel.",
            "updated_at": "2026-08-27 10:00:00",
        }
    ]

    receipt = shadow_migrate_legacy(
        candidate,
        legacy_records=records,
        embedding_provider=SmallProvider(),
    )

    after = hashlib.sha256(live.read_bytes()).hexdigest()
    assert before == after
    assert receipt.destination == str(candidate.resolve())
    assert receipt.source_record_count == 1
    assert receipt.imported_record_count == 1
    assert receipt.candidate_record_count == 1
    assert receipt.authority_active is False
    assert receipt.receipt_version == "memory-shadow-migration-v1"
    assert receipt.cache["semantic_status"] == "available"
    assert MemoryV2Store.authority_is_active(candidate) is False
    assert MemoryV2Store(candidate).get_record("legacy:project:1") is not None

    with pytest.raises(ValueError, match="live Memory v2 path"):
        shadow_migrate_legacy(live, legacy_records=records)


def test_shadow_evaluation_writes_versioned_report_and_candidate_receipts(tmp_path) -> None:
    candidate = tmp_path / "candidate.sqlite3"
    shadow_migrate_legacy(
        candidate,
        legacy_records=[
            {
                "id": 1,
                "type": "project",
                "content": "Atlas deploys through the green channel.",
            }
        ],
    )
    corpus = RegressionCorpus(
        corpus_id="shadow-v1",
        cases=(
            RegressionCase(
                case_id="positive",
                query="Atlas green channel",
                expected_record_ids=("legacy:project:1",),
            ),
            RegressionCase(
                case_id="negative",
                query="zephyr unobtainium",
                expect_no_answer=True,
            ),
        ),
    )

    report = evaluate_shadow_database(
        candidate,
        corpus,
        report_path=tmp_path / "evaluation.json",
        top_k=3,
    )

    assert report.recall_at_k == 1.0
    assert report.false_positive_rate == 0.0
    assert report.report_version == "memory-retrieval-evaluation-v1"
    assert report.model_metadata == {"status": "not_configured"}
    assert len(MemoryV2Store(candidate).retrieval_receipts()) == 2
    assert (tmp_path / "evaluation.json").is_file()


def test_canary_is_deterministic_and_rollback_keeps_candidate_data(tmp_path) -> None:
    candidate = tmp_path / "candidate.sqlite3"
    MemoryV2Store(candidate)
    state_path = tmp_path / "rollout.json"
    controller = RetrievalRolloutController(state_path)

    state = controller.configure(
        candidate,
        mode="canary",
        canary_percent=50.0,
        salt="stable-test-salt",
        now=10.0,
    )
    selections = [state.select_variant(f"request-{index}") for index in range(100)]

    assert selections == [state.select_variant(f"request-{index}") for index in range(100)]
    assert {"baseline", "candidate"}.issubset(set(selections))
    before = candidate.read_bytes()
    rolled_back = controller.rollback(reason="candidate regression", now=11.0)
    assert rolled_back.mode == "off"
    assert rolled_back.select_variant("request-1") == "baseline"
    assert rolled_back.rollback_reason_sha256
    assert candidate.read_bytes() == before
    assert controller.load() == rolled_back


def test_canonical_retrieval_honours_shadow_canary_and_rollback(
    tmp_path, monkeypatch
) -> None:
    candidate = tmp_path / "candidate.sqlite3"
    shadow_migrate_legacy(
        candidate,
        legacy_records=[
            {
                "id": 1,
                "type": "project",
                "content": "Cedar candidate memory is available.",
            }
        ],
    )
    state_path = tmp_path / "rollout.json"
    controller = RetrievalRolloutController(state_path)
    monkeypatch.setenv("SERENA_MEMORY_RETRIEVAL_ROLLOUT", str(state_path))
    monkeypatch.setenv("SERENA_MEMORY_RETRIEVAL_CACHE", ":memory:")
    monkeypatch.setattr(
        "memory.v2.MemoryV2Store.authority_is_active",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr("memory.store.list_memories", lambda *_args, **_kwargs: [])

    controller.configure(candidate, mode="shadow", salt="stable")
    shadow = retrieve_memory("Cedar candidate memory")
    assert shadow.authority == "legacy-markdown"
    assert shadow.hits == ()
    assert shadow.receipt["rollout"]["mode"] == "shadow"
    assert shadow.receipt["rollout"]["candidate_receipt_id"]

    controller.configure(candidate, mode="canary", canary_percent=100, salt="stable")
    canary = retrieve_memory("Cedar candidate memory")
    assert canary.authority == "memory-v2-candidate"
    assert canary.hits[0].record_id == "legacy:project:1"
    assert canary.receipt["rollout"]["variant"] == "candidate"

    controller.rollback(reason="regression")
    baseline = retrieve_memory("Cedar candidate memory")
    assert baseline.authority == "legacy-markdown"
    assert baseline.hits == ()
    assert "rollout" not in baseline.receipt


def test_tampered_rollout_pointer_cannot_open_the_live_v2_database(
    tmp_path, monkeypatch
) -> None:
    live = tmp_path / "live.sqlite3"
    MemoryV2Store(live)
    before = hashlib.sha256(live.read_bytes()).hexdigest()
    state_path = tmp_path / "rollout.json"
    state_path.write_text(
        json.dumps(
            {
                "mode": "canary",
                "candidate_db": str(live),
                "canary_percent": 100.0,
                "salt": "tampered",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SERENA_MEMORY_V2_DB_PATH", str(live))
    monkeypatch.setenv("SERENA_MEMORY_RETRIEVAL_ROLLOUT", str(state_path))
    monkeypatch.setenv("SERENA_MEMORY_RETRIEVAL_CACHE", ":memory:")
    monkeypatch.setattr("memory.store.list_memories", lambda *_args, **_kwargs: [])

    result = retrieve_memory("candidate query")

    assert result.authority == "legacy-markdown"
    assert result.receipt["rollout"]["variant"] == "baseline"
    assert result.receipt["rollout"]["fallback_reason"] == "candidate_error"
    assert hashlib.sha256(live.read_bytes()).hexdigest() == before


def test_report_packer_metrics_accept_context_pack_objects() -> None:
    report = evaluate_regression_corpus(
        _corpus(),
        lambda case, _limit: {
            "hits": (
                [{"record": {"record_id": "atlas", "content": "green"}}]
                if case.case_id == "positive"
                else []
            )
        },
        retrieval_version="test",
        ranking_version="test",
        packer=lambda _case, _response: SimpleNamespace(
            selected_record_ids=("atlas",),
            character_count=20,
            token_count=5,
        ),
    )

    assert report.context_budget_pass_rate == 1.0
