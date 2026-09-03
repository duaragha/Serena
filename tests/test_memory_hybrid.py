from __future__ import annotations

import importlib.metadata
import sqlite3
import sys
import types

from memory.hybrid import (
    EmbeddingMetadata,
    EmbeddingUnavailable,
    HybridCandidateGenerator,
    HybridDocument,
    HybridRequest,
    SentenceTransformerEmbeddingProvider,
)
from memory.v2 import MemoryV2Store, source_receipt


class KeywordDenseProvider:
    def __init__(self, *, version: str = "fixture-v1") -> None:
        self.document_calls = 0
        self._metadata = EmbeddingMetadata(
            model_id="fixture-semantic-model",
            model_version=version,
            model_sha256=("a" if version == "fixture-v1" else "b") * 64,
            dimensions=3,
            normalized=True,
            runtime="test-local-runtime",
        )

    @property
    def metadata(self) -> EmbeddingMetadata:
        return self._metadata

    def encode_query(self, text: str) -> tuple[float, ...]:
        return self._vector(text)

    def encode_documents(self, texts):
        self.document_calls += len(texts)
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> tuple[float, ...]:
        value = text.casefold()
        if any(term in value for term in ("automobile", "car", "compact", "vehicle")):
            return (1.0, 0.0, 0.0)
        if any(term in value for term in ("atlas", "deploy", "release")):
            return (0.0, 1.0, 0.0)
        return (0.0, 0.0, 1.0)


class TemporarilyUnavailableProvider:
    @property
    def metadata(self) -> EmbeddingMetadata:
        raise EmbeddingUnavailable("local runtime temporarily unavailable")

    def encode_query(self, text: str):
        raise AssertionError(f"query encoding should not run: {text}")

    def encode_documents(self, texts):
        raise AssertionError(f"document encoding should not run: {texts}")


def test_exact_ledger_project_entity_and_fts5_candidates(tmp_path) -> None:
    generator = HybridCandidateGenerator(tmp_path / "cache.sqlite3")
    documents = (
        HybridDocument(
            record_id="legacy:ledger:7",
            content="Atlas release ledger tracks the green deployment channel.",
            project="Atlas",
            people=("Raghav",),
            legacy_type="ledger",
            legacy_id=7,
            identifiers=("atlas-release", "7"),
            ledger_text="atlas-release goal ship the green deployment channel",
        ),
        HybridDocument(record_id="other", content="Coffee grinder notes."),
    )

    result = generator.generate(
        documents,
        HybridRequest(
            query="atlas-release",
            project="Atlas",
            entities=("Raghav",),
        ),
        authority="test",
    )

    assert result.candidates[0].record_id == "legacy:ledger:7"
    assert result.candidates[0].exact_score >= 0.95
    assert result.candidates[0].lexical_score > 0
    assert result.candidates[0].project_score == 1.0
    assert result.candidates[0].entity_score == 1.0
    assert "exact_ledger" in result.candidates[0].reasons
    assert result.diagnostics["semantic_status"] == "unavailable"
    assert result.diagnostics["fallback_reason"] == "local_model_not_configured"


def test_real_dense_provider_seam_finds_a_paraphrase_without_lexical_overlap(tmp_path) -> None:
    provider = KeywordDenseProvider()
    generator = HybridCandidateGenerator(
        tmp_path / "cache.sqlite3",
        embedding_provider=provider,
    )
    documents = (
        HybridDocument(record_id="car", content="Raghav prefers compact automobiles."),
        HybridDocument(record_id="therapy", content="Therapy is scheduled on Thursday."),
    )

    result = generator.generate(
        documents,
        HybridRequest(query="which small car does he like"),
        authority="test",
    )

    assert result.candidates[0].record_id == "car"
    assert result.candidates[0].dense_score > 0.99
    assert result.candidates[0].lexical_score == 0.0
    assert result.diagnostics["semantic_status"] == "available"
    assert result.diagnostics["embedding_model"]["model_version"] == "fixture-v1"


def test_temporarily_unavailable_model_degrades_to_exact_and_fts(tmp_path) -> None:
    generator = HybridCandidateGenerator(
        tmp_path / "cache.sqlite3",
        embedding_provider=TemporarilyUnavailableProvider(),
    )
    documents = (
        HybridDocument(record_id="atlas", content="Atlas deployment ledger."),
        HybridDocument(record_id="coffee", content="Coffee grinder notes."),
    )

    result = generator.generate(
        documents,
        HybridRequest(query="atlas deployment"),
        authority="test",
    )

    assert result.candidates[0].record_id == "atlas"
    assert result.candidates[0].lexical_score > 0
    assert result.candidates[0].dense_score == 0
    assert result.diagnostics["semantic_status"] == "unavailable"
    assert result.diagnostics["fallback_reason"].startswith("EmbeddingUnavailable:")


def test_unwritable_persistent_cache_degrades_to_memory(tmp_path) -> None:
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("block cache directory creation", encoding="utf-8")
    generator = HybridCandidateGenerator(blocking_file / "cache.sqlite3")

    result = generator.generate(
        (HybridDocument(record_id="atlas", content="Atlas deployment ledger."),),
        HybridRequest(query="atlas deployment"),
        authority="test",
    )

    assert result.candidates[0].record_id == "atlas"
    assert result.candidates[0].lexical_score > 0
    assert result.diagnostics["cache_status"] == "memory_fallback"
    assert result.diagnostics["cache_fallback_reason"] == "FileExistsError"


def test_vector_cache_reuses_and_safely_invalidates_content_and_model_versions(tmp_path) -> None:
    cache = tmp_path / "cache.sqlite3"
    first_provider = KeywordDenseProvider(version="fixture-v1")
    first = HybridCandidateGenerator(cache, embedding_provider=first_provider)
    documents = (HybridDocument(record_id="atlas", content="Atlas release process."),)
    request = HybridRequest(query="deployment plan")

    first.generate(documents, request, authority="test")
    first.generate(documents, request, authority="test")
    assert first_provider.document_calls == 1

    changed = (HybridDocument(record_id="atlas", content="Atlas release process changed."),)
    first.generate(changed, request, authority="test")
    assert first_provider.document_calls == 2

    second_provider = KeywordDenseProvider(version="fixture-v2")
    second = HybridCandidateGenerator(cache, embedding_provider=second_provider)
    second.generate(changed, request, authority="test")
    assert second_provider.document_calls == 1

    with sqlite3.connect(cache) as connection:
        row = connection.execute(
            "SELECT model_version, model_sha256, dimensions, text_sha256, cache_version "
            "FROM memory_hybrid_vectors WHERE record_id = 'atlas'"
        ).fetchone()
    assert row is not None
    assert row[0] == "fixture-v2"
    assert row[1] == "b" * 64
    assert row[2] == 3
    assert len(row[3]) == 64
    assert row[4] == 1


def test_sentence_transformer_loader_requires_a_local_path_and_disables_network(
    tmp_path, monkeypatch
) -> None:
    model_path = tmp_path / "local-model"
    model_path.mkdir()
    (model_path / "config.json").write_text('{"model_type":"fixture"}', encoding="utf-8")
    captured = {}

    class FakeModel:
        def __init__(self, path, **kwargs):
            captured["path"] = path
            captured.update(kwargs)

        @staticmethod
        def get_sentence_embedding_dimension():
            return 2

        @staticmethod
        def encode_query(_text, **_kwargs):
            return [1.0, 0.0]

        @staticmethod
        def encode_document(texts, **_kwargs):
            return [[1.0, 0.0] for _text in texts]

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = FakeModel
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "fixture-runtime")
    provider = SentenceTransformerEmbeddingProvider(model_path)

    assert provider.metadata.dimensions == 2
    assert captured["path"] == str(model_path.resolve())
    assert captured["local_files_only"] is True
    assert captured["trust_remote_code"] is False
    assert captured["device"] == "cpu"
    assert provider.encode_query("query") == (1.0, 0.0)


def test_v2_primary_path_uses_dense_candidates_and_receipts_model_metadata(tmp_path) -> None:
    provider = KeywordDenseProvider()
    store = MemoryV2Store(tmp_path / "memory.sqlite3", embedding_provider=provider)
    proposal = store.propose_candidate(
        content="Raghav prefers compact automobiles.",
        record_type="preference",
        source=source_receipt(
            kind="test",
            locator="test:dense",
            source_text="Raghav prefers compact automobiles.",
        ),
    )
    approved = store.approve_proposal(proposal["proposal_id"], reviewer="Raghav")

    response = store.retrieve_with_receipt("which small car does he like", limit=3)

    assert response["hits"][0]["record"]["record_id"] == approved["applied_record_id"]
    assert response["hits"][0]["semantic_score"] > 0.99
    details = response["receipt"]["filters"]["candidate_generation"]
    assert details["semantic_status"] == "available"
    assert details["embedding_model"]["model_id"] == "fixture-semantic-model"
    assert any(
        reason.startswith("dense_semantic:") for reason in response["hits"][0]["reasons"]
    )
