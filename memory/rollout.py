"""Isolated memory shadow migration, evaluation, canary, and rollback."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from memory.evaluation import (
    EvaluationReport,
    RegressionCorpus,
    evaluate_regression_corpus,
    write_evaluation_report,
)
from memory.hybrid import LocalEmbeddingProvider, SentenceTransformerEmbeddingProvider

ROLLOUT_STATE_VERSION = 1
SHADOW_MIGRATION_RECEIPT_VERSION = "memory-shadow-migration-v1"
ROLLOUT_MODES = frozenset({"off", "shadow", "canary"})


@dataclass(frozen=True, slots=True)
class ShadowMigrationReceipt:
    receipt_id: str
    destination: str
    source_record_count: int
    imported_record_count: int
    existing_record_count: int
    candidate_record_count: int
    authority_active: bool
    candidate_sha256: str
    cache: dict[str, Any]
    created_at: float
    receipt_version: str = SHADOW_MIGRATION_RECEIPT_VERSION

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["cache"] = dict(self.cache)
        return value


@dataclass(frozen=True, slots=True)
class RetrievalRolloutState:
    mode: str = "off"
    candidate_db: str = ""
    canary_percent: float = 0.0
    salt: str = ""
    revision: int = 0
    rollback_reason_sha256: str = ""
    updated_at: float = 0.0
    version: int = ROLLOUT_STATE_VERSION

    def __post_init__(self) -> None:
        if self.version != ROLLOUT_STATE_VERSION:
            raise ValueError("unsupported memory retrieval rollout state version")
        if self.mode not in ROLLOUT_MODES:
            raise ValueError("invalid memory retrieval rollout mode")
        if not 0.0 <= self.canary_percent <= 100.0:
            raise ValueError("canary percent must be between zero and one hundred")
        if self.mode in {"shadow", "canary"} and not self.candidate_db:
            raise ValueError("candidate database is required for shadow or canary mode")

    def select_variant(self, request_key: str) -> str:
        """Choose deterministically; shadow mode always serves the baseline."""

        if self.mode != "canary" or self.canary_percent <= 0:
            return "baseline"
        digest = hashlib.sha256(f"{self.salt}\0{request_key}".encode()).digest()
        bucket = int.from_bytes(digest[:8], "big") / float(2**64)
        return "candidate" if bucket * 100.0 < self.canary_percent else "baseline"

    @property
    def shadow_enabled(self) -> bool:
        return self.mode == "shadow"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RetrievalRolloutController:
    """Persist a default-off rollout pointer without changing memory data."""

    def __init__(self, state_path: str | Path) -> None:
        self.state_path = Path(state_path).expanduser()

    def load(self) -> RetrievalRolloutState:
        if not self.state_path.is_file():
            return RetrievalRolloutState()
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("memory retrieval rollout state is invalid") from exc
        if not isinstance(value, dict):
            raise ValueError("memory retrieval rollout state must be an object")
        return RetrievalRolloutState(**value)

    def candidate_path(self, state: RetrievalRolloutState | None = None) -> Path | None:
        """Resolve and revalidate a configured candidate before every use."""

        current = self.load() if state is None else state
        if current.mode == "off":
            return None
        return _guard_candidate_path(current.candidate_db, require_existing=True)

    def configure(
        self,
        candidate_db: str | Path,
        *,
        mode: str,
        canary_percent: float = 0.0,
        salt: str = "",
        now: float | None = None,
    ) -> RetrievalRolloutState:
        clean_mode = str(mode or "").strip().casefold()
        if clean_mode not in {"shadow", "canary"}:
            raise ValueError("rollout configure mode must be shadow or canary")
        candidate = _guard_candidate_path(candidate_db, require_existing=True)
        from memory.v2 import MemoryV2Store

        if MemoryV2Store.authority_is_active(candidate):
            raise ValueError("an active memory authority cannot be used as a rollout candidate")
        previous = self.load()
        state = RetrievalRolloutState(
            mode=clean_mode,
            candidate_db=str(candidate),
            canary_percent=float(canary_percent) if clean_mode == "canary" else 0.0,
            salt=str(salt or uuid.uuid4().hex)[:256],
            revision=previous.revision + 1,
            updated_at=time.time() if now is None else float(now),
        )
        self._write(state)
        return state

    def rollback(self, *, reason: str, now: float | None = None) -> RetrievalRolloutState:
        previous = self.load()
        state = RetrievalRolloutState(
            mode="off",
            candidate_db=previous.candidate_db,
            canary_percent=0.0,
            salt=previous.salt,
            revision=previous.revision + 1,
            rollback_reason_sha256=_sha256(" ".join(str(reason or "").split())),
            updated_at=time.time() if now is None else float(now),
        )
        self._write(state)
        return state

    def _write(self, state: RetrievalRolloutState) -> None:
        _write_private(
            self.state_path,
            json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n",
        )


def shadow_migrate_legacy(
    destination: str | Path,
    *,
    legacy_records: list[dict[str, Any]] | None = None,
    embedding_provider: LocalEmbeddingProvider | None = None,
    embedding_model_path: str | Path | None = None,
) -> ShadowMigrationReceipt:
    """Copy legacy records into an inactive candidate database only."""

    candidate = _guard_candidate_path(destination, require_existing=False)
    from memory.v2 import MemoryV2Store

    if candidate.exists() and MemoryV2Store.authority_is_active(candidate):
        raise ValueError("shadow migration refuses an active candidate database")
    if legacy_records is None:
        from memory.store import list_memories

        legacy_records = list_memories()
    source = [dict(record) for record in legacy_records]
    provider = embedding_provider
    if provider is None and embedding_model_path:
        provider = SentenceTransformerEmbeddingProvider(embedding_model_path)
    store = MemoryV2Store(
        candidate,
        embedding_provider=provider,
    )
    migrated = store.migrate_legacy(source)
    cache = store.prepare_hybrid_cache()
    if MemoryV2Store.authority_is_active(candidate):
        raise RuntimeError("shadow migration unexpectedly activated the candidate authority")
    return ShadowMigrationReceipt(
        receipt_id=str(uuid.uuid4()),
        destination=str(candidate),
        source_record_count=len(source),
        imported_record_count=int(migrated.get("imported") or 0),
        existing_record_count=int(migrated.get("existing") or 0),
        candidate_record_count=len(store.records(include_inactive=True)),
        authority_active=False,
        candidate_sha256=_file_sha256(candidate),
        cache=cache,
        created_at=time.time(),
    )


def evaluate_shadow_database(
    database: str | Path,
    corpus: RegressionCorpus,
    *,
    report_path: str | Path,
    top_k: int = 5,
    embedding_provider: LocalEmbeddingProvider | None = None,
    embedding_model_path: str | Path | None = None,
) -> EvaluationReport:
    """Evaluate an inactive candidate DB and write a local versioned report."""

    candidate = _guard_candidate_path(database, require_existing=True)
    from memory.v2 import RETRIEVAL_RANKING_VERSION, MemoryV2Store

    if MemoryV2Store.authority_is_active(candidate):
        raise ValueError("shadow evaluation refuses an active memory authority")
    provider = embedding_provider
    if provider is None and embedding_model_path:
        provider = SentenceTransformerEmbeddingProvider(embedding_model_path)
    store = MemoryV2Store(
        candidate,
        embedding_provider=provider,
    )

    def retrieve(case: Any, limit: int) -> dict[str, Any]:
        return store.retrieve_with_receipt(case.query, limit=limit, surface=case.surface)

    model_metadata: dict[str, Any] = {"status": "not_configured"}
    if provider is not None:
        try:
            model_metadata = {"status": "available", **provider.metadata.to_dict()}
        except Exception:
            model_metadata = {"status": "unavailable"}
    report = evaluate_regression_corpus(
        corpus,
        retrieve,
        top_k=top_k,
        retrieval_version="shadow-memory-v2",
        ranking_version=RETRIEVAL_RANKING_VERSION,
        model_metadata=model_metadata,
    )
    write_evaluation_report(report_path, report)
    return report


def _guard_candidate_path(value: str | Path, *, require_existing: bool) -> Path:
    candidate = Path(value).expanduser().resolve()
    from memory.v2 import DEFAULT_DB_PATH

    protected = {DEFAULT_DB_PATH.expanduser().resolve()}
    configured = os.environ.get("SERENA_MEMORY_V2_DB_PATH", "").strip()
    if configured:
        protected.add(Path(configured).expanduser().resolve())
    if candidate in protected:
        raise ValueError("candidate database must not be the live Memory v2 path")
    for live in protected:
        if candidate.exists() and live.exists():
            try:
                if candidate.samefile(live):
                    raise ValueError("candidate database aliases the live Memory v2 path")
            except OSError:
                pass
    if require_existing and not candidate.is_file():
        raise ValueError("candidate database does not exist")
    return candidate


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeError("candidate database could not be fingerprinted") from exc
    return digest.hexdigest()


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


__all__ = [
    "ROLLOUT_STATE_VERSION",
    "SHADOW_MIGRATION_RECEIPT_VERSION",
    "RetrievalRolloutController",
    "RetrievalRolloutState",
    "ShadowMigrationReceipt",
    "evaluate_shadow_database",
    "shadow_migrate_legacy",
]
