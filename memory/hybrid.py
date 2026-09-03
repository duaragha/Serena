"""Private local candidate generation for Serena memory retrieval.

The three channels stay deliberately separate: structured exact matches,
SQLite FTS5/BM25, and dense vectors from an explicitly staged local model.
Runtime model loading never accepts a Hub identifier and never downloads.
When the model is absent or fails, exact and FTS5 retrieval continue and the
degradation is exposed in diagnostics instead of inventing a semantic score.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import time
import unicodedata
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

CANDIDATE_GENERATION_VERSION = "local-hybrid-candidates-v1"
VECTOR_CACHE_VERSION = 1
MAX_CHANNEL_CANDIDATES = 64
MAX_EMBED_BATCH = 64
MIN_DENSE_SCORE = 0.25
DEFAULT_CACHE_PATH = (
    Path.home() / ".local" / "state" / "serena" / "memory-retrieval-cache.sqlite3"
)

_WORD = re.compile(r"[^\W_]+(?:['-][^\W_]+)*", re.UNICODE)
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "you",
        "your",
    }
)


class EmbeddingUnavailable(RuntimeError):
    """The configured local model cannot currently produce embeddings."""


@dataclass(frozen=True, slots=True)
class EmbeddingMetadata:
    model_id: str
    model_version: str
    model_sha256: str
    dimensions: int
    normalized: bool
    runtime: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LocalEmbeddingProvider(Protocol):
    """Small provider seam for a genuine local dense embedding runtime."""

    @property
    def metadata(self) -> EmbeddingMetadata: ...

    def encode_query(self, text: str) -> Sequence[float]: ...

    def encode_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


class SentenceTransformerEmbeddingProvider:
    """Load a Sentence Transformers model from a local directory only."""

    def __init__(self, model_path: str | Path, *, device: str = "cpu") -> None:
        self.model_path = Path(model_path).expanduser().resolve()
        self.device = str(device or "cpu")
        self._model: Any = None
        self._metadata: EmbeddingMetadata | None = None

    @property
    def metadata(self) -> EmbeddingMetadata:
        self._load()
        assert self._metadata is not None
        return self._metadata

    def encode_query(self, text: str) -> Sequence[float]:
        self._load()
        method = getattr(self._model, "encode_query", None) or self._model.encode
        try:
            encoded = method(
                str(text),
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            return _vector_row(encoded)
        except Exception as exc:
            raise EmbeddingUnavailable(f"local query embedding failed: {type(exc).__name__}") from exc

    def encode_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        self._load()
        method = getattr(self._model, "encode_document", None) or self._model.encode
        try:
            encoded = method(
                list(texts),
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            return [_vector_row(row) for row in encoded]
        except Exception as exc:
            raise EmbeddingUnavailable(
                f"local document embedding failed: {type(exc).__name__}"
            ) from exc

    def _load(self) -> None:
        if self._model is not None:
            return
        if not self.model_path.is_dir():
            raise EmbeddingUnavailable("configured local embedding model directory is missing")

        # These are defense in depth around local_files_only. The loader never
        # receives a model identifier, so a cache miss cannot turn into a call.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
        os.environ.setdefault("DO_NOT_TRACK", "1")
        try:
            from importlib.metadata import PackageNotFoundError, version

            from sentence_transformers import SentenceTransformer

            runtime_version = version("sentence-transformers")
        except (ImportError, PackageNotFoundError) as exc:
            raise EmbeddingUnavailable(
                "sentence-transformers is not installed in this environment"
            ) from exc
        try:
            model = SentenceTransformer(
                str(self.model_path),
                device=self.device,
                local_files_only=True,
                trust_remote_code=False,
            )
            dimensions = int(model.get_sentence_embedding_dimension() or 0)
        except Exception as exc:
            raise EmbeddingUnavailable(f"local embedding model load failed: {type(exc).__name__}") from exc
        if dimensions <= 0:
            raise EmbeddingUnavailable("local embedding model did not declare a dimension")

        manifest = _read_model_manifest(self.model_path)
        digest = _directory_sha256(self.model_path)
        model_id = str(manifest.get("model_id") or self.model_path.name)[:256]
        model_version = str(manifest.get("model_version") or digest[:16])[:256]
        self._model = model
        self._metadata = EmbeddingMetadata(
            model_id=model_id,
            model_version=model_version,
            model_sha256=digest,
            dimensions=dimensions,
            normalized=True,
            runtime=f"sentence-transformers:{runtime_version}",
        )


@dataclass(frozen=True, slots=True)
class HybridDocument:
    record_id: str
    content: str
    project: str = ""
    people: tuple[str, ...] = ()
    record_type: str = ""
    legacy_type: str = ""
    legacy_id: int | None = None
    identifiers: tuple[str, ...] = ()
    ledger_text: str = ""
    updated_at: float = 0.0

    @property
    def document_key_text(self) -> str:
        return "\n".join(
            (
                self.content,
                self.project,
                " ".join(self.people),
                " ".join(self.identifiers),
                self.ledger_text,
                self.record_type,
                self.legacy_type,
            )
        )

    @property
    def text_sha256(self) -> str:
        return _sha256(self.document_key_text)


@dataclass(frozen=True, slots=True)
class HybridRequest:
    query: str
    query_variants: tuple[str, ...] = ()
    project: str = ""
    people: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    limit: int = MAX_CHANNEL_CANDIDATES


@dataclass(frozen=True, slots=True)
class HybridCandidate:
    record_id: str
    exact_score: float = 0.0
    lexical_score: float = 0.0
    dense_score: float = 0.0
    project_score: float = 0.0
    people_score: float = 0.0
    entity_score: float = 0.0
    bm25: float | None = None
    lexical_rank: int | None = None
    dense_rank: int | None = None
    reasons: tuple[str, ...] = ()

    @property
    def fused_score(self) -> float:
        return round(
            0.18 * self.exact_score
            + 0.38 * self.lexical_score
            + 0.34 * self.dense_score
            + 0.04 * self.project_score
            + 0.03 * self.people_score
            + 0.03 * self.entity_score,
            6,
        )

    def score_dict(self) -> dict[str, float]:
        scores = {
            "exact_score": self.exact_score,
            "lexical_score": self.lexical_score,
            "project": self.project_score,
            "people": self.people_score,
            "entity_score": self.entity_score,
        }
        if self.dense_score > 0:
            scores["dense_score"] = self.dense_score
        return scores


@dataclass(frozen=True, slots=True)
class CandidateGenerationResult:
    candidates: tuple[HybridCandidate, ...]
    diagnostics: dict[str, Any]

    def score_map(self) -> dict[str, dict[str, float]]:
        return {candidate.record_id: candidate.score_dict() for candidate in self.candidates}


class HybridCandidateGenerator:
    """Generate exact, FTS5/BM25, and local dense candidates."""

    def __init__(
        self,
        cache_path: str | Path | None = None,
        *,
        embedding_provider: LocalEmbeddingProvider | None = None,
        model_path: str | Path | None = None,
    ) -> None:
        configured_cache = os.environ.get("SERENA_MEMORY_RETRIEVAL_CACHE", "").strip()
        self.cache_path: str | Path = cache_path or configured_cache or DEFAULT_CACHE_PATH
        configured_model = os.environ.get("SERENA_MEMORY_EMBEDDING_MODEL", "").strip()
        explicit_model = model_path or configured_model
        self.embedding_provider = embedding_provider
        self._cache_status = "not_opened"
        self._cache_fallback_reason = ""
        if self.embedding_provider is None and explicit_model:
            self.embedding_provider = SentenceTransformerEmbeddingProvider(explicit_model)

    def prepare(
        self,
        documents: Sequence[HybridDocument],
        *,
        authority: str,
    ) -> dict[str, Any]:
        """Synchronize lexical and vector caches without running a query."""

        with self._connect() as connection:
            self._ensure_schema(connection)
            sync = self._sync_documents(connection, documents, authority=authority)
            dense = self._prepare_dense(connection, documents, authority=authority)
        return {
            "candidate_generation_version": CANDIDATE_GENERATION_VERSION,
            "authority": authority,
            "document_count": len(documents),
            "cache_status": self._cache_status,
            "cache_fallback_reason": self._cache_fallback_reason,
            **sync,
            **dense,
        }

    def generate(
        self,
        documents: Sequence[HybridDocument],
        request: HybridRequest,
        *,
        authority: str,
    ) -> CandidateGenerationResult:
        clean_query = " ".join(str(request.query or "").split())[:2_000]
        if not clean_query:
            return CandidateGenerationResult(
                (),
                {
                    "candidate_generation_version": CANDIDATE_GENERATION_VERSION,
                    "authority": authority,
                    "query_sha256": _sha256(""),
                    "document_count": len(documents),
                    "candidate_count": 0,
                    "semantic_status": "not_requested",
                    "fallback_reason": "empty_query",
                },
            )
        clean_limit = min(MAX_CHANNEL_CANDIDATES, max(1, int(request.limit)))
        with self._connect() as connection:
            self._ensure_schema(connection)
            sync = self._sync_documents(connection, documents, authority=authority)
            exact = self._exact_scores(documents, request)
            lexical = self._lexical_scores(
                connection,
                documents,
                request,
                authority=authority,
                limit=clean_limit,
            )
            dense, dense_diagnostics = self._dense_scores(
                connection,
                documents,
                request,
                authority=authority,
                limit=clean_limit,
            )

        by_id: dict[str, dict[str, Any]] = {}
        for record_id, values in exact.items():
            _merge_candidate_values(by_id.setdefault(record_id, {}), values)
        for record_id, values in lexical.items():
            _merge_candidate_values(by_id.setdefault(record_id, {}), values)
        for record_id, values in dense.items():
            _merge_candidate_values(by_id.setdefault(record_id, {}), values)

        candidates = []
        for record_id, values in by_id.items():
            reasons = tuple(dict.fromkeys(str(value) for value in values.get("reasons", ())))
            candidates.append(
                HybridCandidate(
                    record_id=record_id,
                    exact_score=_unit(values.get("exact_score")),
                    lexical_score=_unit(values.get("lexical_score")),
                    dense_score=_unit(values.get("dense_score")),
                    project_score=_unit(values.get("project_score")),
                    people_score=_unit(values.get("people_score")),
                    entity_score=_unit(values.get("entity_score")),
                    bm25=_optional_float(values.get("bm25")),
                    lexical_rank=_optional_int(values.get("lexical_rank")),
                    dense_rank=_optional_int(values.get("dense_rank")),
                    reasons=reasons,
                )
            )
        candidates.sort(key=lambda item: (-item.fused_score, item.record_id))
        bounded = tuple(candidates[:clean_limit])
        diagnostics = {
            "candidate_generation_version": CANDIDATE_GENERATION_VERSION,
            "authority": authority,
            "query_sha256": _sha256(clean_query),
            "document_count": len(documents),
            "candidate_count": len(bounded),
            "exact_candidate_count": len(exact),
            "lexical_candidate_count": len(lexical),
            "dense_candidate_count": len(dense),
            "channel_limit": clean_limit,
            "cache_status": self._cache_status,
            "cache_fallback_reason": self._cache_fallback_reason,
            **sync,
            **dense_diagnostics,
        }
        return CandidateGenerationResult(bounded, diagnostics)

    def _connect(self) -> sqlite3.Connection:
        if str(self.cache_path) == ":memory:":
            connection = sqlite3.connect(":memory:")
            self._cache_status = "memory"
            self._cache_fallback_reason = ""
        else:
            path = Path(self.cache_path).expanduser()
            try:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                connection = sqlite3.connect(path, timeout=10)
            except (OSError, sqlite3.Error) as exc:
                connection = sqlite3.connect(":memory:")
                self._cache_status = "memory_fallback"
                self._cache_fallback_reason = type(exc).__name__
            else:
                self._cache_status = "persistent"
                self._cache_fallback_reason = ""
                if os.name != "nt":
                    with suppress(OSError):
                        path.chmod(0o600)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_hybrid_documents (
                document_key TEXT PRIMARY KEY,
                authority TEXT NOT NULL,
                record_id TEXT NOT NULL,
                text_sha256 TEXT NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(authority, record_id)
            );
            CREATE INDEX IF NOT EXISTS memory_hybrid_documents_authority_idx
                ON memory_hybrid_documents(authority, record_id);
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_hybrid_fts USING fts5(
                document_key UNINDEXED,
                content,
                project,
                people,
                identifiers,
                ledger,
                tokenize = 'unicode61 remove_diacritics 2'
            );
            CREATE TABLE IF NOT EXISTS memory_hybrid_vectors (
                document_key TEXT PRIMARY KEY,
                authority TEXT NOT NULL,
                record_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                model_version TEXT NOT NULL,
                model_sha256 TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                text_sha256 TEXT NOT NULL,
                normalized INTEGER NOT NULL,
                vector BLOB NOT NULL,
                cache_version INTEGER NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS memory_hybrid_vectors_model_idx
                ON memory_hybrid_vectors(
                    authority, model_id, model_version, model_sha256, dimensions
                );
            """
        )

    def _sync_documents(
        self,
        connection: sqlite3.Connection,
        documents: Sequence[HybridDocument],
        *,
        authority: str,
    ) -> dict[str, int]:
        existing = {
            str(row["record_id"]): (str(row["document_key"]), str(row["text_sha256"]))
            for row in connection.execute(
                "SELECT document_key, record_id, text_sha256 FROM memory_hybrid_documents "
                "WHERE authority = ?",
                (authority,),
            )
        }
        current_ids = {document.record_id for document in documents}
        changed = 0
        unchanged = 0
        for document in documents:
            key = _document_key(authority, document.record_id)
            current = existing.get(document.record_id)
            if current == (key, document.text_sha256):
                unchanged += 1
                continue
            connection.execute("DELETE FROM memory_hybrid_fts WHERE document_key = ?", (key,))
            connection.execute(
                "INSERT INTO memory_hybrid_fts("
                "document_key, content, project, people, identifiers, ledger"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    key,
                    document.content,
                    document.project,
                    " ".join(document.people),
                    " ".join(document.identifiers),
                    document.ledger_text,
                ),
            )
            connection.execute(
                "INSERT INTO memory_hybrid_documents("
                "document_key, authority, record_id, text_sha256, updated_at"
                ") VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(document_key) DO UPDATE SET "
                "authority = excluded.authority, record_id = excluded.record_id, "
                "text_sha256 = excluded.text_sha256, updated_at = excluded.updated_at",
                (key, authority, document.record_id, document.text_sha256, document.updated_at),
            )
            changed += 1

        stale = [record_id for record_id in existing if record_id not in current_ids]
        for record_id in stale:
            key = existing[record_id][0]
            connection.execute("DELETE FROM memory_hybrid_fts WHERE document_key = ?", (key,))
            connection.execute("DELETE FROM memory_hybrid_vectors WHERE document_key = ?", (key,))
            connection.execute("DELETE FROM memory_hybrid_documents WHERE document_key = ?", (key,))
        connection.commit()
        return {
            "indexed_document_count": changed,
            "unchanged_document_count": unchanged,
            "purged_document_count": len(stale),
        }

    @staticmethod
    def _exact_scores(
        documents: Sequence[HybridDocument], request: HybridRequest
    ) -> dict[str, dict[str, Any]]:
        query = _normalize(request.query)
        wanted_project = _normalize(request.project)
        wanted_people = tuple(_normalize(value) for value in request.people if _normalize(value))
        wanted_entities = tuple(
            _normalize(value) for value in request.entities if _normalize(value)
        )
        results: dict[str, dict[str, Any]] = {}
        for document in documents:
            content = _normalize(document.content)
            project = _normalize(document.project)
            people = tuple(_normalize(value) for value in document.people)
            identifiers = tuple(
                _normalize(value)
                for value in (document.record_id, *document.identifiers)
                if _normalize(value)
            )
            ledger = _normalize(document.ledger_text)
            searchable = " ".join((content, project, *people, *identifiers, ledger))
            exact_score = 0.0
            project_score = 0.0
            people_score = 0.0
            entity_score = 0.0
            reasons: list[str] = []
            if query and query in identifiers:
                exact_score = 1.0
                reasons.append("exact_identifier")
            if document.legacy_type == "ledger" and query and _contains_phrase(ledger, query):
                exact_score = max(exact_score, 0.98)
                reasons.append("exact_ledger")
            if wanted_project and project == wanted_project:
                project_score = 1.0
                exact_score = max(exact_score, 0.95)
                reasons.append("exact_project")
            matched_people = sum(
                1
                for person in wanted_people
                if person in people or _contains_phrase(searchable, person)
            )
            if wanted_people:
                people_score = matched_people / len(wanted_people)
                if matched_people:
                    reasons.append("exact_person")
            matched_entities = sum(
                1 for entity in wanted_entities if _contains_phrase(searchable, entity)
            )
            if wanted_entities:
                entity_score = matched_entities / len(wanted_entities)
                if matched_entities:
                    reasons.append("exact_entity")
            if query and _contains_phrase(content, query):
                exact_score = max(exact_score, 0.85)
                reasons.append("exact_phrase")
            if max(exact_score, project_score, people_score, entity_score) > 0:
                results[document.record_id] = {
                    "exact_score": exact_score,
                    "project_score": project_score,
                    "people_score": people_score,
                    "entity_score": entity_score,
                    "reasons": reasons,
                }
        return results

    @staticmethod
    def _lexical_scores(
        connection: sqlite3.Connection,
        documents: Sequence[HybridDocument],
        request: HybridRequest,
        *,
        authority: str,
        limit: int,
    ) -> dict[str, dict[str, Any]]:
        terms = _query_terms(
            " ".join((request.query, *request.query_variants[:6], request.project, *request.people))
        )
        if not terms:
            return {}
        match = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
        rows = connection.execute(
            "SELECT d.record_id, "
            "bm25(memory_hybrid_fts, 0.0, 1.0, 3.0, 3.0, 4.0, 4.0) AS score "
            "FROM memory_hybrid_fts "
            "JOIN memory_hybrid_documents AS d "
            "ON d.document_key = memory_hybrid_fts.document_key "
            "WHERE memory_hybrid_fts MATCH ? AND d.authority = ? "
            "ORDER BY score, d.record_id LIMIT ?",
            (match, authority, limit),
        ).fetchall()
        by_id = {document.record_id: document for document in documents}
        results = {}
        for index, row in enumerate(rows, start=1):
            record_id = str(row["record_id"])
            document = by_id.get(record_id)
            document_terms = set(_WORD.findall(_normalize(document.document_key_text))) if document else set()
            matched = sum(1 for term in terms if term in document_terms)
            coverage = matched / len(terms)
            if len(terms) > 1 and coverage <= 0.5:
                continue
            results[record_id] = {
                "lexical_score": round(
                    coverage * (1.0 / (1.0 + 0.18 * (index - 1))), 6
                ),
                "bm25": float(row["score"]),
                "lexical_rank": index,
                "reasons": ["fts5_bm25", f"lexical_coverage:{coverage:.3f}"],
            }
        return results

    def _dense_scores(
        self,
        connection: sqlite3.Connection,
        documents: Sequence[HybridDocument],
        request: HybridRequest,
        *,
        authority: str,
        limit: int,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        prepared = self._prepare_dense(connection, documents, authority=authority)
        if prepared["semantic_status"] != "available":
            return {}, prepared
        provider = self.embedding_provider
        assert provider is not None
        metadata = provider.metadata
        try:
            query_vector = _normalized_vector(provider.encode_query(request.query), metadata.dimensions)
        except EmbeddingUnavailable as exc:
            return {}, {
                **prepared,
                "semantic_status": "unavailable",
                "fallback_reason": _safe_error(exc),
            }
        rows = connection.execute(
            "SELECT record_id, vector FROM memory_hybrid_vectors "
            "WHERE authority = ? AND model_id = ? AND model_version = ? "
            "AND model_sha256 = ? AND dimensions = ? AND cache_version = ?",
            (
                authority,
                metadata.model_id,
                metadata.model_version,
                metadata.model_sha256,
                metadata.dimensions,
                VECTOR_CACHE_VERSION,
            ),
        ).fetchall()
        scored = []
        for row in rows:
            vector = _unpack_vector(bytes(row["vector"]), metadata.dimensions)
            similarity = sum(left * right for left, right in zip(query_vector, vector, strict=True))
            if similarity >= MIN_DENSE_SCORE:
                scored.append((float(similarity), str(row["record_id"])))
        scored.sort(key=lambda item: (-item[0], item[1]))
        results = {
            record_id: {
                "dense_score": round(max(0.0, min(1.0, similarity)), 6),
                "dense_rank": index,
                "reasons": ["local_dense_embedding"],
            }
            for index, (similarity, record_id) in enumerate(scored[:limit], start=1)
        }
        return results, prepared

    def _prepare_dense(
        self,
        connection: sqlite3.Connection,
        documents: Sequence[HybridDocument],
        *,
        authority: str,
    ) -> dict[str, Any]:
        if self.embedding_provider is None:
            return {
                "semantic_status": "unavailable",
                "fallback_reason": "local_model_not_configured",
                "embedding_model": {},
                "vector_cache_hit_count": 0,
                "vector_cache_miss_count": len(documents),
            }
        try:
            metadata = self.embedding_provider.metadata
        except EmbeddingUnavailable as exc:
            return {
                "semantic_status": "unavailable",
                "fallback_reason": _safe_error(exc),
                "embedding_model": {},
                "vector_cache_hit_count": 0,
                "vector_cache_miss_count": len(documents),
            }
        existing = {
            str(row["record_id"]): row
            for row in connection.execute(
                "SELECT * FROM memory_hybrid_vectors WHERE authority = ?",
                (authority,),
            )
        }
        stale = []
        cache_hits = 0
        for document in documents:
            row = existing.get(document.record_id)
            valid = bool(
                row is not None
                and str(row["model_id"]) == metadata.model_id
                and str(row["model_version"]) == metadata.model_version
                and str(row["model_sha256"]) == metadata.model_sha256
                and int(row["dimensions"]) == metadata.dimensions
                and str(row["text_sha256"]) == document.text_sha256
                and int(row["normalized"]) == int(metadata.normalized)
                and int(row["cache_version"]) == VECTOR_CACHE_VERSION
                and len(bytes(row["vector"])) == metadata.dimensions * 4
            )
            if valid:
                cache_hits += 1
            else:
                stale.append(document)
        try:
            for start in range(0, len(stale), MAX_EMBED_BATCH):
                batch = stale[start : start + MAX_EMBED_BATCH]
                vectors = self.embedding_provider.encode_documents(
                    [document.document_key_text for document in batch]
                )
                if len(vectors) != len(batch):
                    raise EmbeddingUnavailable("local model returned the wrong batch size")
                for document, vector in zip(batch, vectors, strict=True):
                    normalized = _normalized_vector(vector, metadata.dimensions)
                    connection.execute(
                        "INSERT INTO memory_hybrid_vectors("
                        "document_key, authority, record_id, model_id, model_version, "
                        "model_sha256, dimensions, text_sha256, normalized, vector, "
                        "cache_version, created_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(document_key) DO UPDATE SET "
                        "authority = excluded.authority, record_id = excluded.record_id, "
                        "model_id = excluded.model_id, model_version = excluded.model_version, "
                        "model_sha256 = excluded.model_sha256, dimensions = excluded.dimensions, "
                        "text_sha256 = excluded.text_sha256, normalized = excluded.normalized, "
                        "vector = excluded.vector, cache_version = excluded.cache_version, "
                        "created_at = excluded.created_at",
                        (
                            _document_key(authority, document.record_id),
                            authority,
                            document.record_id,
                            metadata.model_id,
                            metadata.model_version,
                            metadata.model_sha256,
                            metadata.dimensions,
                            document.text_sha256,
                            int(metadata.normalized),
                            _pack_vector(normalized),
                            VECTOR_CACHE_VERSION,
                            time.time(),
                        ),
                    )
            connection.commit()
        except EmbeddingUnavailable as exc:
            connection.rollback()
            return {
                "semantic_status": "unavailable",
                "fallback_reason": _safe_error(exc),
                "embedding_model": metadata.to_dict(),
                "vector_cache_hit_count": cache_hits,
                "vector_cache_miss_count": len(stale),
            }
        return {
            "semantic_status": "available",
            "fallback_reason": "",
            "embedding_model": metadata.to_dict(),
            "vector_cache_hit_count": cache_hits,
            "vector_cache_miss_count": len(stale),
        }


def v2_documents(records: Sequence[Any]) -> tuple[HybridDocument, ...]:
    documents = []
    for record in records:
        legacy_id = getattr(record, "legacy_id", None)
        legacy_type = str(getattr(record, "legacy_type", "") or "")
        identifiers = [str(getattr(record, "record_id", ""))]
        if legacy_id is not None:
            identifiers.extend((str(legacy_id), f"{legacy_type}:{legacy_id}"))
        source = getattr(record, "source", {})
        ledger_text = ""
        if legacy_type == "ledger" and isinstance(source, Mapping):
            ledger_text = " ".join(
                str(source.get(name) or "")
                for name in ("ledger_key", "goal", "facts", "decision", "promise", "risk")
            )
        documents.append(
            HybridDocument(
                record_id=str(record.record_id),
                content=str(record.content),
                project=str(record.project or ""),
                people=tuple(str(value) for value in record.people),
                record_type=str(record.record_type),
                legacy_type=legacy_type,
                legacy_id=legacy_id,
                identifiers=tuple(identifiers),
                ledger_text=ledger_text,
                updated_at=float(record.updated_at),
            )
        )
    return tuple(documents)


def legacy_documents(
    records: Sequence[Mapping[str, Any]],
    *,
    ledger_fields: Sequence[str],
) -> tuple[HybridDocument, ...]:
    documents = []
    for record in records:
        legacy_type = str(record.get("type") or "general")
        legacy_id = _optional_int(record.get("id"))
        content = str(record.get("content") or "")
        record_id = (
            f"legacy:{legacy_type}:{legacy_id}"
            if legacy_id is not None
            else f"legacy:{legacy_type}:{_sha256(content)[:16]}"
        )
        ledger_text = " ".join(str(record.get(field) or "") for field in ledger_fields)
        identifiers = tuple(
            value
            for value in (
                record_id,
                str(legacy_id) if legacy_id is not None else "",
                str(record.get("ledger_key") or ""),
                str(record.get("filename") or ""),
            )
            if value
        )
        documents.append(
            HybridDocument(
                record_id=record_id,
                content=content,
                project=str(record.get("project") or ""),
                record_type=str(record.get("record_type") or ""),
                legacy_type=legacy_type,
                legacy_id=legacy_id,
                identifiers=identifiers,
                ledger_text=ledger_text,
                updated_at=_legacy_timestamp(record.get("updated_at")),
            )
        )
    return tuple(documents)


def _query_terms(value: str) -> tuple[str, ...]:
    words = [_normalize(word) for word in _WORD.findall(value)]
    kept = [word for word in words if len(word) > 1 and word not in _STOPWORDS]
    return tuple(dict.fromkeys((kept or words)[:32]))


def _merge_candidate_values(target: dict[str, Any], values: Mapping[str, Any]) -> None:
    reasons = [str(value) for value in target.get("reasons", ())]
    reasons.extend(str(value) for value in values.get("reasons", ()))
    target.update({key: value for key, value in values.items() if key != "reasons"})
    target["reasons"] = list(dict.fromkeys(reasons))


def _contains_phrase(searchable: str, phrase: str) -> bool:
    wanted = _WORD.findall(_normalize(phrase))
    available = _WORD.findall(_normalize(searchable))
    if not wanted:
        return False
    width = len(wanted)
    return any(
        available[index : index + width] == wanted
        for index in range(len(available) - width + 1)
    )


def _normalize(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(normalized.split())[:4_000]


def _document_key(authority: str, record_id: str) -> str:
    return f"{authority}:{record_id}"


def _normalized_vector(values: Sequence[float], dimensions: int) -> tuple[float, ...]:
    try:
        vector = tuple(float(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise EmbeddingUnavailable("local model returned a non-numeric vector") from exc
    if len(vector) != dimensions or any(not math.isfinite(value) for value in vector):
        raise EmbeddingUnavailable("local model returned an invalid vector dimension")
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        raise EmbeddingUnavailable("local model returned an empty vector")
    return tuple(value / norm for value in vector)


def _pack_vector(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(value: bytes, dimensions: int) -> tuple[float, ...]:
    if len(value) != dimensions * 4:
        raise EmbeddingUnavailable("cached vector dimension mismatch")
    return tuple(struct.unpack(f"<{dimensions}f", value))


def _vector_row(value: Any) -> tuple[float, ...]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, Sequence) and value and isinstance(value[0], Sequence):
        value = value[0]
    if not isinstance(value, Sequence):
        raise EmbeddingUnavailable("local model returned an unsupported vector value")
    return tuple(float(item) for item in value)


def _read_model_manifest(path: Path) -> dict[str, Any]:
    manifest_path = path / "serena-embedding-model.json"
    if not manifest_path.is_file():
        return {}
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EmbeddingUnavailable("local embedding manifest is invalid") from exc
    return dict(value) if isinstance(value, Mapping) else {}


def _directory_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(item for item in path.rglob("*") if item.is_file())
    if not files:
        raise EmbeddingUnavailable("local embedding model directory is empty")
    for item in files:
        relative = item.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        try:
            with item.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            raise EmbeddingUnavailable("local embedding model could not be fingerprinted") from exc
    return digest.hexdigest()


def _legacy_timestamp(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _unit(value: object) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, score)) if math.isfinite(score) else 0.0


def _optional_float(value: object) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score if math.isfinite(score) else None


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _safe_error(error: BaseException) -> str:
    return f"{type(error).__name__}:{str(error)[:160]}"


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


__all__ = [
    "CANDIDATE_GENERATION_VERSION",
    "CandidateGenerationResult",
    "EmbeddingMetadata",
    "EmbeddingUnavailable",
    "HybridCandidate",
    "HybridCandidateGenerator",
    "HybridDocument",
    "HybridRequest",
    "LocalEmbeddingProvider",
    "SentenceTransformerEmbeddingProvider",
    "legacy_documents",
    "v2_documents",
]
