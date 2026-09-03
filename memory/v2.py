"""Typed, source-backed memory with local hybrid retrieval and reviewed changes.

Markdown memories remain readable during migration. This store adds the state
that flat files cannot represent: immutable versions, validity, confidence,
relationships, retention, proposal review, and retrieval receipts. Candidate
extraction can create proposals, but only ``approve_proposal`` changes records.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from memory.reranker import (
    RANKING_VERSION,
    CandidateSignals,
    rerank_candidates,
)

DEFAULT_DB_PATH = Path.home() / ".local" / "state" / "serena" / "memory-v2.sqlite3"
SCHEMA_VERSION = 3

RECORD_TYPES = frozenset(
    {"semantic_fact", "episode", "procedure", "commitment", "preference", "correction"}
)
SENSITIVITY_LEVELS = frozenset({"public", "personal", "sensitive"})
RECORD_STATES = frozenset({"current", "superseded", "contested", "forgotten", "retracted"})
RELATION_KINDS = frozenset({"supersedes", "contradicts", "derived_from"})
PROPOSAL_OPERATIONS = frozenset({"add", "update", "supersede", "contradict", "forget", "retain"})
PROPOSAL_STATES = frozenset({"proposed", "approved", "rejected", "rolled_back"})

MAX_CONTENT_CHARS = 8_000
MAX_SOURCE_CHARS = 16_000
MAX_PROPOSALS = 1_000
MAX_RETRIEVAL_RESULTS = 50
MAX_RECEIPTS = 10_000
RETRIEVAL_RANKING_VERSION = RANKING_VERSION
RETRIEVAL_FEEDBACK_VERSION = "receipt-bound-feedback-v1"
FEEDBACK_KINDS = frozenset({"relevance", "factual_correction"})
FEEDBACK_STATES = frozenset({"active", "revoked", "resolved"})

_LEGACY_TYPES = frozenset(
    {"task", "ledger", "loop", "feedback", "user", "project", "reference", "general"}
)
_V2_LEGACY_TYPE_MAP = {
    "commitment": "loop",
    "correction": "feedback",
    "semantic_fact": "user",
    "procedure": "reference",
    "preference": "user",
    "episode": "general",
}

_WORDS = re.compile(r"[a-z0-9]+")
_LEGACY_TYPE_MAP = {
    "task": "commitment",
    "ledger": "commitment",
    "loop": "commitment",
    "feedback": "correction",
    "user": "semantic_fact",
    "project": "semantic_fact",
    "reference": "procedure",
    "general": "episode",
}


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    record_id: str
    record_type: str
    content: str
    project: str
    people: tuple[str, ...]
    source: dict[str, Any]
    confidence: float
    sensitivity: str
    valid_from: float | None
    valid_until: float | None
    retention_until: float | None
    status: str
    legacy_id: int | None
    legacy_type: str | None
    created_at: float
    updated_at: float
    forgotten_at: float | None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["people"] = list(self.people)
        return value


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    record: MemoryRecord
    score: float
    literal_score: float
    semantic_score: float
    components: dict[str, float]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "record": self.record.to_dict(),
            "score": self.score,
            "literal_score": self.literal_score,
            "semantic_score": self.semantic_score,
            "components": dict(self.components),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class RetrievalReceipt:
    receipt_id: str
    query_sha256: str
    surface: str
    project: str
    record_type: str | None
    ranking_version: str
    filters: dict[str, Any]
    returned: tuple[dict[str, Any], ...]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["filters"] = dict(self.filters)
        value["returned"] = list(self.returned)
        return value


@dataclass(frozen=True, slots=True)
class RetrievalFeedback:
    feedback_id: str
    receipt_id: str
    record_id: str
    kind: str
    label: str
    query_sha256: str
    surface: str
    ranking_version: str
    reason_sha256: str
    source_sha256: str
    proposal_id: str | None
    state: str
    created_at: float
    updated_at: float
    revoked_at: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryV2Store:
    """SQLite authority for v2 records and reviewed proposal application."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        embedding_provider: Any = None,
        embedding_model_path: str | Path | None = None,
    ) -> None:
        configured = os.environ.get("SERENA_MEMORY_V2_DB_PATH", "").strip()
        self.path = Path(path or configured or DEFAULT_DB_PATH).expanduser()
        self._embedding_provider = embedding_provider
        self._embedding_model_path = embedding_model_path
        self._initialize()
        from core.control_plane import SurfaceOutbox

        self._outbox = SurfaceOutbox("memory", self.path)

    def _stage_event(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        proposal_id: str,
        lifecycle_state: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        self._outbox.stage_event(
            connection,
            event_type=event_type,
            lifecycle_state=lifecycle_state,
            job_id=proposal_id,
            authority="memory-v2",
            payload=dict(payload or {}),
        )

    def flush_control_outbox(self, control_store: Any = None) -> int:
        return self._outbox.flush(control_store)

    def pending_control_events(self) -> int:
        return self._outbox.pending()

    @classmethod
    def authority_is_active(cls, path: Path | None = None) -> bool:
        """Inspect activation without creating a database when none exists."""

        configured = os.environ.get("SERENA_MEMORY_V2_DB_PATH", "").strip()
        candidate = Path(path or configured or DEFAULT_DB_PATH).expanduser().resolve()
        if not candidate.is_file():
            return False
        try:
            uri = f"{candidate.as_uri()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=2) as connection:
                row = connection.execute(
                    "SELECT value FROM memory_meta WHERE key = 'authority_state'"
                ).fetchone()
        except sqlite3.Error:
            return False
        return bool(row and str(row[0]) == "active")

    def activate_authority(self, *, actor: str) -> dict[str, Any]:
        """Explicitly make v2 the normal recall authority after migration."""

        reviewer = _required_text(actor, "activation actor", 256)
        activated_at = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = dict(
                connection.execute(
                    "SELECT key, value FROM memory_meta "
                    "WHERE key IN ('authority_state', 'authority_actor', 'authority_activated_at')"
                ).fetchall()
            )
            if existing.get("authority_state") == "active":
                return {
                    "authority": "memory-v2",
                    "state": "active",
                    "actor": existing.get("authority_actor", ""),
                    "activated_at": float(existing.get("authority_activated_at") or 0.0),
                    "already_active": True,
                }
            connection.executemany(
                "INSERT OR REPLACE INTO memory_meta(key, value) VALUES (?, ?)",
                (
                    ("authority_state", "active"),
                    ("authority_actor", reviewer),
                    ("authority_activated_at", str(activated_at)),
                ),
            )
            self._stage_event(
                connection,
                event_type="authority.activated",
                proposal_id="memory-authority",
                lifecycle_state="active",
                payload={"actor": reviewer},
            )
        return {
            "authority": "memory-v2",
            "state": "active",
            "actor": reviewer,
            "activated_at": activated_at,
            "already_active": False,
        }

    def is_authoritative(self) -> bool:
        return self.authority_is_active(self.path)

    def migrate_legacy(self, records: Iterable[Mapping[str, Any]]) -> dict[str, int]:
        """Idempotently import legacy Markdown projections without changing them."""

        imported = 0
        existing = 0
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in records:
                legacy_id = _integer(item.get("id"))
                legacy_type = str(item.get("type") or "general").strip().lower()
                content = _content(item.get("content"))
                if legacy_id is None or not content:
                    continue
                record_id = f"legacy:{legacy_type}:{legacy_id}"
                found = connection.execute(
                    "SELECT 1 FROM memory_records WHERE record_id = ?", (record_id,)
                ).fetchone()
                if found is not None:
                    existing += 1
                    continue
                source = {
                    "kind": "legacy_markdown",
                    "locator": str(item.get("filename") or record_id),
                    "session_id": str(item.get("source_session_id") or ""),
                    "agent": str(item.get("source_agent") or "unknown"),
                    "title": str(item.get("source_title") or ""),
                    "message_timestamp": str(item.get("source_message_timestamp") or ""),
                    "content_sha256": _sha256(content),
                }
                created = _timestamp(item.get("created_at")) or now
                updated = _timestamp(item.get("updated_at")) or created
                self._insert_record(
                    connection,
                    record_id=record_id,
                    record_type=_LEGACY_TYPE_MAP.get(legacy_type, "episode"),
                    content=content,
                    source=source,
                    confidence=0.75 if source["session_id"] else 0.5,
                    sensitivity="personal",
                    project="",
                    people=(),
                    valid_from=created,
                    valid_until=None,
                    retention_until=None,
                    status="current",
                    legacy_id=legacy_id,
                    legacy_type=legacy_type,
                    created_at=created,
                    updated_at=updated,
                )
                imported += 1
        return {"imported": imported, "existing": existing}

    def migrate_current_legacy_store(self) -> dict[str, int]:
        from memory.store import list_memories

        return self.migrate_legacy(list_memories())

    def records(
        self,
        *,
        include_inactive: bool = False,
        record_type: str | None = None,
    ) -> list[MemoryRecord]:
        clauses = [] if include_inactive else ["status = 'current'"]
        params: list[object] = []
        if record_type is not None:
            clean_type = _record_type(record_type)
            clauses.append("record_type = ?")
            params.append(clean_type)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_records" + where + " ORDER BY updated_at DESC",
                tuple(params),
            ).fetchall()
        return [_record_from_row(row) for row in rows]

    def get_record(self, record_id: str) -> MemoryRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM memory_records WHERE record_id = ?", (_identifier(record_id),)
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    def relations(self, record_id: str) -> list[dict[str, Any]]:
        clean_id = _identifier(record_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source_record_id, target_record_id, kind, created_at
                FROM memory_relations
                WHERE source_record_id = ? OR target_record_id = ?
                ORDER BY created_at, kind
                """,
                (clean_id, clean_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def propose_candidate(
        self,
        *,
        content: str,
        record_type: str,
        source: Mapping[str, Any],
        confidence: float = 0.75,
        sensitivity: str = "personal",
        project: str = "",
        people: Sequence[str] = (),
        valid_from: float | None = None,
        valid_until: float | None = None,
        retention_until: float | None = None,
        contradicts: str | None = None,
    ) -> dict[str, Any]:
        """Consolidate one extracted candidate into a reviewable operation."""

        clean_content = _content(content)
        if contradicts:
            operation = "contradict"
            target = _identifier(contradicts)
        else:
            hits = self.retrieve(
                clean_content, limit=1, record_type=record_type, record_receipt=False
            )
            best = hits[0] if hits else None
            if best is not None and best.semantic_score >= 0.72:
                operation = "supersede"
                target = best.record.record_id
            else:
                operation = "add"
                target = None
        return self.create_proposal(
            operation=operation,
            target_record_id=target,
            candidate={
                "record_type": record_type,
                "content": clean_content,
                "confidence": confidence,
                "sensitivity": sensitivity,
                "project": project,
                "people": list(people),
                "valid_from": valid_from,
                "valid_until": valid_until,
                "retention_until": retention_until,
            },
            source=source,
        )

    def create_proposal(
        self,
        *,
        operation: str,
        source: Mapping[str, Any],
        candidate: Mapping[str, Any] | None = None,
        target_record_id: str | None = None,
    ) -> dict[str, Any]:
        clean_operation = str(operation or "").strip().lower()
        if clean_operation not in PROPOSAL_OPERATIONS:
            raise ValueError("unsupported memory proposal operation")
        clean_source = _source(source)
        target_id = _identifier(target_record_id) if target_record_id else None
        current = self.get_record(target_id) if target_id else None
        if clean_operation != "add" and current is None:
            raise KeyError(f"unknown memory record {target_id or '(missing)'}")
        clean_candidate = _candidate(
            candidate or {},
            required=clean_operation in {"add", "update", "supersede", "contradict"},
        )
        if clean_operation == "retain":
            until = clean_candidate.get("retention_until")
            if until is None:
                raise ValueError("retain proposals require retention_until")
        proposal_key = _sha256(
            _json(
                {
                    "operation": clean_operation,
                    "target": target_id,
                    "candidate": clean_candidate,
                    "source": clean_source,
                }
            )
        )
        now = time.time()
        proposal_id = str(uuid.uuid4())
        diff = _proposal_diff(clean_operation, current, clean_candidate)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM memory_proposals WHERE proposal_key = ?", (proposal_key,)
            ).fetchone()
            if existing is not None:
                return _proposal_from_row(existing)
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM memory_proposals WHERE state = 'proposed'"
            ).fetchone()
            if int(count["count"] or 0) >= MAX_PROPOSALS:
                raise RuntimeError("memory proposal queue is full")
            connection.execute(
                """
                INSERT INTO memory_proposals(
                    proposal_id, proposal_key, operation, target_record_id,
                    candidate_json, source_json, diff_json, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)
                """,
                (
                    proposal_id,
                    proposal_key,
                    clean_operation,
                    target_id,
                    _json(clean_candidate),
                    _json(clean_source),
                    _json(diff),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM memory_proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            assert row is not None
            self._stage_event(
                connection,
                event_type="proposal.created",
                proposal_id=proposal_id,
                lifecycle_state="proposed",
                payload={
                    "operation": clean_operation,
                    "target_record_id": target_id,
                    "summary": f"review memory {clean_operation} proposal",
                },
            )
            proposal = _proposal_from_row(row)
        with suppress(Exception):
            from core.plugin_loader import emit_plugin_hook

            emit_plugin_hook(
                "memory.proposal.created",
                {
                    "proposal_id": str(proposal["proposal_id"]),
                    "operation": str(proposal["operation"]),
                    "target_record_id": str(proposal.get("target_record_id") or ""),
                    "state": str(proposal["state"]),
                },
            )
        return proposal

    def proposals(self, *, state: str = "proposed", limit: int = 100) -> list[dict[str, Any]]:
        clean_state = str(state or "").strip().lower()
        if clean_state not in PROPOSAL_STATES:
            raise ValueError("invalid memory proposal state")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_proposals WHERE state = ? ORDER BY created_at DESC LIMIT ?",
                (clean_state, min(500, max(1, int(limit)))),
            ).fetchall()
        return [_proposal_from_row(row) for row in rows]

    def approve_proposal(
        self,
        proposal_id: str,
        *,
        reviewer: str,
        reason: str = "explicitly approved",
    ) -> dict[str, Any]:
        """Apply one reviewed proposal and retain enough state to roll it back."""

        clean_id = _identifier(proposal_id)
        clean_reviewer = _required_text(reviewer, "reviewer", 256)
        clean_reason = _required_text(reason, "reason", 1_000)
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_proposal(connection, clean_id)
            if str(row["state"]) != "proposed":
                raise ValueError("only proposed memory changes can be approved")
            proposal = _proposal_from_row(row)
            rollback, applied_id = self._apply_proposal(connection, proposal, now=now)
            connection.execute(
                """
                UPDATE memory_proposals
                SET state = 'approved', reviewer = ?, decision_reason = ?,
                    applied_record_id = ?, rollback_json = ?, reviewed_at = ?, updated_at = ?
                WHERE proposal_id = ? AND state = 'proposed'
                """,
                (
                    clean_reviewer,
                    clean_reason,
                    applied_id,
                    _json(rollback),
                    now,
                    now,
                    clean_id,
                ),
            )
            self._stage_event(
                connection,
                event_type="proposal.approved",
                proposal_id=clean_id,
                lifecycle_state="approved",
                payload={"operation": proposal["operation"], "applied_record_id": applied_id},
            )
            return _proposal_from_row(self._require_proposal(connection, clean_id))

    def reject_proposal(
        self,
        proposal_id: str,
        *,
        reviewer: str,
        reason: str,
    ) -> dict[str, Any]:
        clean_id = _identifier(proposal_id)
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_proposal(connection, clean_id)
            if str(row["state"]) != "proposed":
                raise ValueError("only proposed memory changes can be rejected")
            connection.execute(
                """
                UPDATE memory_proposals
                SET state = 'rejected', reviewer = ?, decision_reason = ?,
                    reviewed_at = ?, updated_at = ?
                WHERE proposal_id = ?
                """,
                (
                    _required_text(reviewer, "reviewer", 256),
                    _required_text(reason, "reason", 1_000),
                    now,
                    now,
                    clean_id,
                ),
            )
            self._stage_event(
                connection,
                event_type="proposal.rejected",
                proposal_id=clean_id,
                lifecycle_state="rejected",
            )
            return _proposal_from_row(self._require_proposal(connection, clean_id))

    def rollback_proposal(
        self,
        proposal_id: str,
        *,
        reviewer: str,
        reason: str,
    ) -> dict[str, Any]:
        clean_id = _identifier(proposal_id)
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_proposal(connection, clean_id)
            if str(row["state"]) != "approved":
                raise ValueError("only approved memory changes can be rolled back")
            rollback = _load_json(row["rollback_json"], {})
            applied_id = str(row["applied_record_id"] or "")
            if applied_id:
                connection.execute(
                    "UPDATE memory_records SET status = 'retracted', updated_at = ? "
                    "WHERE record_id = ?",
                    (now, applied_id),
                )
            previous = rollback.get("previous")
            if isinstance(previous, dict) and previous.get("record_id"):
                connection.execute(
                    """
                    UPDATE memory_records
                    SET status = ?, forgotten_at = ?, retention_until = ?, valid_until = ?,
                        updated_at = ?
                    WHERE record_id = ?
                    """,
                    (
                        previous.get("status") or "current",
                        previous.get("forgotten_at"),
                        previous.get("retention_until"),
                        previous.get("valid_until"),
                        now,
                        str(previous["record_id"]),
                    ),
                )
            connection.execute(
                """
                UPDATE memory_proposals
                SET state = 'rolled_back', reviewer = ?, decision_reason = ?, updated_at = ?
                WHERE proposal_id = ?
                """,
                (
                    _required_text(reviewer, "reviewer", 256),
                    _required_text(reason, "reason", 1_000),
                    now,
                    clean_id,
                ),
            )
            self._stage_event(
                connection,
                event_type="proposal.rolled_back",
                proposal_id=clean_id,
                lifecycle_state="rolled_back",
                payload={"applied_record_id": applied_id or None},
            )
            return _proposal_from_row(self._require_proposal(connection, clean_id))

    def retention_proposals(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """Create forget proposals for expired retention, never mutate silently."""

        current = time.time() if now is None else float(now)
        due = [
            record
            for record in self.records()
            if record.retention_until is not None and record.retention_until <= current
        ]
        proposals = []
        for record in due:
            proposals.append(
                self.create_proposal(
                    operation="forget",
                    target_record_id=record.record_id,
                    source={
                        "kind": "retention_policy",
                        "locator": f"record:{record.record_id}",
                        "content_sha256": _sha256(record.content),
                    },
                )
            )
        return proposals

    def prepare_hybrid_cache(self) -> dict[str, Any]:
        """Build local FTS and dense caches without activating memory authority."""

        from memory.hybrid import HybridCandidateGenerator, v2_documents

        records = self.records(include_inactive=True)
        generator = HybridCandidateGenerator(
            self.path,
            embedding_provider=self._embedding_provider,
            model_path=self._embedding_model_path,
        )
        return generator.prepare(v2_documents(records), authority="memory-v2")

    def retrieve(
        self,
        query: str,
        *,
        limit: int = 8,
        surface: str = "private",
        project: str = "",
        people: Sequence[str] = (),
        entities: Sequence[str] = (),
        record_type: str | None = None,
        now: float | None = None,
        temporal_intent: str | None = None,
        as_of: float | None = None,
        include_contested: bool = False,
        include_superseded: bool | None = None,
        active_record_ids: Sequence[str] = (),
        candidate_scores: Mapping[str, Mapping[str, float]] | None = None,
        query_variants: Sequence[str] = (),
        query_understanding: Mapping[str, Any] | None = None,
        record_receipt: bool = True,
    ) -> list[RetrievalHit]:
        """Apply one state-aware policy to bounded local retrieval candidates."""

        clean_query = " ".join(str(query or "").split())
        if not clean_query:
            return []
        current = time.time() if now is None else float(now)
        hits, ranking_details = self._rank_retrieval(
            clean_query,
            limit=limit,
            surface=surface,
            project=project,
            people=people,
            entities=entities,
            record_type=record_type,
            now=current,
            temporal_intent=temporal_intent,
            as_of=as_of,
            include_contested=include_contested,
            include_superseded=include_superseded,
            active_record_ids=active_record_ids,
            candidate_scores=candidate_scores,
            query_variants=query_variants,
            query_understanding=query_understanding,
        )
        if record_receipt:
            self._record_retrieval(
                query=clean_query,
                surface=surface,
                project=project,
                people=people,
                record_type=record_type,
                hits=hits,
                ranking_details=ranking_details,
                created_at=current,
            )
        return hits

    def _rank_retrieval(
        self,
        clean_query: str,
        *,
        limit: int,
        surface: str,
        project: str,
        people: Sequence[str],
        entities: Sequence[str],
        record_type: str | None,
        now: float,
        temporal_intent: str | None,
        as_of: float | None,
        include_contested: bool,
        include_superseded: bool | None,
        active_record_ids: Sequence[str],
        candidate_scores: Mapping[str, Mapping[str, float]] | None,
        query_variants: Sequence[str],
        query_understanding: Mapping[str, Any] | None,
    ) -> tuple[list[RetrievalHit], dict[str, Any]]:
        clean_variants = tuple(
            dict.fromkeys(
                " ".join(str(value or "").split())[:2_000]
                for value in tuple(query_variants)[:6]
                if str(value or "").strip()
            )
        )
        expanded_query = " ".join((clean_query, *clean_variants))
        q_words = _tokens(expanded_query)
        query_folded = clean_query.casefold()
        wanted_people = {str(person).strip().casefold() for person in people if str(person).strip()}
        wanted_entities = [str(entity).strip().casefold() for entity in entities if str(entity).strip()]
        wanted_project = str(project or "").strip().casefold()
        records = self.records(include_inactive=True, record_type=record_type)
        candidate_generation: dict[str, Any] = {}
        if candidate_scores is None:
            from memory.hybrid import HybridCandidateGenerator, HybridRequest, v2_documents

            generated = HybridCandidateGenerator(
                self.path,
                embedding_provider=self._embedding_provider,
                model_path=self._embedding_model_path,
            ).generate(
                v2_documents(records),
                HybridRequest(
                    query=clean_query,
                    query_variants=clean_variants,
                    project=project,
                    people=tuple(str(value) for value in people),
                    entities=tuple(str(value) for value in entities),
                    limit=MAX_RETRIEVAL_RESULTS,
                ),
                authority="memory-v2",
            )
            supplied_scores: Mapping[str, Mapping[str, float]] = generated.score_map()
            candidate_generation = generated.diagnostics
        else:
            supplied_scores = candidate_scores
        feedback_penalties = self._feedback_penalties(_sha256(clean_query), surface)
        candidates: list[CandidateSignals] = []
        dense_record_ids: set[str] = set()
        invalid_dense_count = 0
        for record in records:
            words = _tokens(record.content)
            raw_external = supplied_scores.get(record.record_id, {})
            external = raw_external if isinstance(raw_external, Mapping) else {}
            dense_present = any(key in external for key in ("dense", "dense_score"))
            dense_score = _optional_external_score(external, "dense", "dense_score")
            has_dense = dense_score is not None
            if has_dense:
                dense_record_ids.add(record.record_id)
            elif dense_present:
                invalid_dense_count += 1
            literal = max(
                _overlap_score(q_words, words),
                _external_score(external, "literal", "lexical", "lexical_score"),
            )
            semantic_score = dense_score if dense_score is not None else 0.0
            record_folded = record.content.casefold()
            exact = 1.0 if query_folded in record_folded else 0.0
            if not exact and len(q_words) <= 6 and q_words and q_words.issubset(words):
                exact = 0.8
            exact = max(exact, _external_score(external, "exact", "exact_score"))

            record_project = record.project.strip().casefold()
            project_score = 1.0 if wanted_project and record_project == wanted_project else 0.0
            if record_project:
                project_score = max(project_score, _overlap_score(q_words, _tokens(record_project)))
            project_score = max(project_score, _external_score(external, "project"))

            record_people = {person.strip().casefold() for person in record.people if person.strip()}
            people_score = _overlap_score(wanted_people, record_people) if wanted_people else 0.0
            if record_people:
                people_words = set().union(*(_tokens(person) for person in record_people))
                people_score = max(people_score, _overlap_score(q_words, people_words))
            people_score = max(people_score, _external_score(external, "people"))

            searchable = " ".join((record_folded, record_project, *record_people))
            entity_matches = sum(
                1 for entity in wanted_entities if _contains_entity_phrase(searchable, entity)
            )
            entity_score = entity_matches / len(wanted_entities) if wanted_entities else 0.0
            entity_score = max(entity_score, _external_score(external, "entity", "entity_score"))
            relevance_penalty = feedback_penalties.get(record.record_id, (0.0, ()))[0]
            candidates.append(
                CandidateSignals(
                    record=record,
                    literal=literal,
                    semantic=semantic_score,
                    exact=exact,
                    project=project_score,
                    people=people_score,
                    entity=entity_score,
                    relevance_penalty=relevance_penalty,
                )
            )

        result = rerank_candidates(
            candidates,
            query=clean_query,
            now=now,
            limit=min(MAX_RETRIEVAL_RESULTS, max(1, int(limit))),
            surface=surface,
            temporal_intent=temporal_intent,
            as_of=as_of,
            include_contested=include_contested,
            include_superseded=include_superseded,
            active_record_ids=active_record_ids,
        )
        dense_supplied = bool(dense_record_ids)
        backend = "hybrid_candidates" if dense_supplied else "deterministic_local_fallback"
        hits = []
        for ranked in result.ranked:
            candidate = ranked.candidate
            record = candidate.record
            assert isinstance(record, MemoryRecord)
            components = dict(ranked.components)
            components.update(
                {
                    "literal_raw": round(candidate.literal, 6),
                    "semantic_raw": round(candidate.semantic, 6),
                    "exact_raw": round(candidate.exact, 6),
                    "project_raw": round(candidate.project, 6),
                    "people_raw": round(candidate.people, 6),
                    "entity_raw": round(candidate.entity, 6),
                }
            )
            reasons = list(ranked.reasons)
            if candidate.semantic > 0:
                reasons.append(f"dense_semantic:{candidate.semantic:.3f}")
            if record.record_id in feedback_penalties:
                reasons.extend(
                    f"relevance_feedback:{feedback_id}"
                    for feedback_id in feedback_penalties[record.record_id][1]
                )
            hits.append(
                RetrievalHit(
                    record=record,
                    score=ranked.score,
                    literal_score=round(candidate.literal, 6),
                    semantic_score=round(candidate.semantic, 6),
                    components=components,
                    reasons=tuple(reasons),
                )
            )
        details = result.receipt_details()
        generated_fallback = str(candidate_generation.get("fallback_reason") or "")
        details.update(
            {
                "backend": backend,
                "fallback_reason": (
                    "invalid_dense_scores"
                    if invalid_dense_count
                    else generated_fallback
                    if generated_fallback
                    else ""
                    if dense_supplied
                    else "dense_scores_unavailable"
                ),
                "invalid_dense_count": invalid_dense_count,
                "entity_count": len(wanted_entities),
                "active_record_count": len({str(value) for value in active_record_ids}),
                "as_of": as_of,
                "include_contested": bool(include_contested),
                "include_superseded": include_superseded,
                "query_variant_count": len(clean_variants),
                "query_variant_sha256": [_sha256(value) for value in clean_variants],
                "feedback_version": RETRIEVAL_FEEDBACK_VERSION,
                "active_feedback_count": sum(
                    len(feedback_ids) for _penalty, feedback_ids in feedback_penalties.values()
                ),
                "query_understanding": dict(query_understanding or {}),
                "candidate_generation": candidate_generation,
            }
        )
        return hits, details

    def retrieve_with_receipt(self, query: str, **filters: Any) -> dict[str, Any]:
        """Retrieve once and return the exact durable receipt for what was exposed."""

        filters.pop("record_receipt", None)
        clean_query = " ".join(str(query or "").split())
        receipt_time = filters.get("now")
        current = time.time() if receipt_time is None else float(receipt_time)
        if clean_query:
            hits, ranking_details = self._rank_retrieval(
                clean_query,
                limit=int(filters.get("limit", 8)),
                surface=str(filters.get("surface") or "private"),
                project=str(filters.get("project") or ""),
                people=filters.get("people") or (),
                entities=filters.get("entities") or (),
                record_type=filters.get("record_type"),
                now=current,
                temporal_intent=filters.get("temporal_intent"),
                as_of=filters.get("as_of"),
                include_contested=bool(filters.get("include_contested", False)),
                include_superseded=filters.get("include_superseded"),
                active_record_ids=filters.get("active_record_ids") or (),
                candidate_scores=filters.get("candidate_scores"),
                query_variants=filters.get("query_variants") or (),
                query_understanding=filters.get("query_understanding"),
            )
        else:
            hits = []
            ranking_details = {"backend": "empty_query", "temporal_intent": "current"}
        receipt = self._record_retrieval(
            query=clean_query,
            surface=str(filters.get("surface") or "private"),
            project=str(filters.get("project") or ""),
            people=filters.get("people") or (),
            record_type=filters.get("record_type"),
            hits=hits,
            ranking_details=ranking_details,
            created_at=current,
        )
        return {"hits": [hit.to_dict() for hit in hits], "receipt": receipt.to_dict()}

    def retrieval_receipts(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_retrieval_receipts "
                "ORDER BY created_at DESC, receipt_id DESC LIMIT ?",
                (min(1_000, max(1, int(limit))),),
            ).fetchall()
        return [_retrieval_receipt_from_row(row).to_dict() for row in rows]

    def retrieval_feedback(
        self,
        *,
        state: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clean_state = str(state or "").strip().lower()
        clean_kind = str(kind or "").strip().lower()
        if clean_state and clean_state not in FEEDBACK_STATES:
            raise ValueError("invalid retrieval feedback state")
        if clean_kind and clean_kind not in FEEDBACK_KINDS:
            raise ValueError("invalid retrieval feedback kind")
        clauses = []
        values: list[object] = []
        if clean_state:
            clauses.append("state = ?")
            values.append(clean_state)
        if clean_kind:
            clauses.append("kind = ?")
            values.append(clean_kind)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(min(1_000, max(1, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_retrieval_feedback"
                + where
                + " ORDER BY created_at DESC, feedback_id DESC LIMIT ?",
                values,
            ).fetchall()
        return [_retrieval_feedback_from_row(row).to_dict() for row in rows]

    def record_relevance_feedback(
        self,
        receipt_id: str,
        record_id: str,
        *,
        reason: str = "",
        source: Mapping[str, Any],
        now: float | None = None,
    ) -> dict[str, Any]:
        """Record reversible relevance failure for one returned record only."""

        clean_receipt_id = _required_text(receipt_id, "retrieval receipt id", 256)
        clean_record_id = _required_text(record_id, "retrieval feedback record id", 256)
        created_at = time.time() if now is None else float(now)
        clean_source = _source(source)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = _require_retrieval_target(connection, clean_receipt_id, clean_record_id)
            feedback = self._insert_feedback(
                connection,
                receipt=receipt,
                record_id=clean_record_id,
                kind="relevance",
                label="irrelevant",
                reason=reason,
                source=clean_source,
                proposal_id=None,
                created_at=created_at,
            )
            self._stage_event(
                connection,
                event_type="retrieval.feedback.recorded",
                proposal_id=feedback.feedback_id,
                lifecycle_state="active",
                payload={
                    "feedback_kind": feedback.kind,
                    "receipt_id": feedback.receipt_id,
                    "record_id": feedback.record_id,
                    "query_sha256": feedback.query_sha256,
                },
            )
        return feedback.to_dict()

    def propose_factual_correction(
        self,
        receipt_id: str,
        record_id: str,
        *,
        corrected_content: str,
        source: Mapping[str, Any],
        reason: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        """Create a reviewable correction proposal and leave canonical memory alone."""

        clean_receipt_id = _required_text(receipt_id, "retrieval receipt id", 256)
        clean_record_id = _required_text(record_id, "retrieval feedback record id", 256)
        clean_content = _content(corrected_content)
        if not clean_content:
            raise ValueError("corrected memory content is required")
        clean_source = _source(source)
        with self._connect() as connection:
            receipt = _require_retrieval_target(connection, clean_receipt_id, clean_record_id)
            row = self._record_row(connection, clean_record_id)
            if row is None:
                raise KeyError(f"unknown memory record {clean_record_id}")
            record = _record_from_row(row)
            if record.status in {"forgotten", "retracted"}:
                raise RuntimeError("inactive memory records cannot receive factual corrections")
        proposal = self.create_proposal(
            operation="update",
            target_record_id=clean_record_id,
            candidate={
                "record_type": record.record_type,
                "content": clean_content,
                "confidence": record.confidence,
                "sensitivity": record.sensitivity,
                "project": record.project,
                "people": list(record.people),
                "valid_from": record.valid_from,
                "valid_until": record.valid_until,
                "retention_until": record.retention_until,
            },
            source=clean_source,
        )
        created_at = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = _require_retrieval_target(connection, clean_receipt_id, clean_record_id)
            feedback = self._insert_feedback(
                connection,
                receipt=receipt,
                record_id=clean_record_id,
                kind="factual_correction",
                label="incorrect_fact",
                reason=reason,
                source=clean_source,
                proposal_id=str(proposal["proposal_id"]),
                created_at=created_at,
            )
            self._stage_event(
                connection,
                event_type="retrieval.correction.proposed",
                proposal_id=feedback.feedback_id,
                lifecycle_state="proposed",
                payload={
                    "feedback_kind": feedback.kind,
                    "receipt_id": feedback.receipt_id,
                    "record_id": feedback.record_id,
                    "proposal_id": feedback.proposal_id,
                },
            )
        return {"feedback": feedback.to_dict(), "proposal": proposal}

    def revoke_relevance_feedback(
        self,
        feedback_id: str,
        *,
        source: Mapping[str, Any],
        now: float | None = None,
    ) -> dict[str, Any]:
        """Revoke a relevance judgment without deleting its audit history."""

        clean_id = _required_text(feedback_id, "retrieval feedback id", 256)
        clean_source = _source(source)
        revoked_at = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM memory_retrieval_feedback WHERE feedback_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown retrieval feedback {clean_id}")
            feedback = _retrieval_feedback_from_row(row)
            if feedback.kind != "relevance":
                raise RuntimeError("factual corrections are reviewed through their proposal")
            if feedback.state == "revoked":
                return feedback.to_dict()
            connection.execute(
                "UPDATE memory_retrieval_feedback SET state = 'revoked', updated_at = ?, "
                "revoked_at = ?, revocation_source_sha256 = ? WHERE feedback_id = ?",
                (revoked_at, revoked_at, _sha256(_json(clean_source)), clean_id),
            )
            self._stage_event(
                connection,
                event_type="retrieval.feedback.revoked",
                proposal_id=clean_id,
                lifecycle_state="revoked",
                payload={"receipt_id": feedback.receipt_id, "record_id": feedback.record_id},
            )
            updated = connection.execute(
                "SELECT * FROM memory_retrieval_feedback WHERE feedback_id = ?",
                (clean_id,),
            ).fetchone()
            assert updated is not None
            return _retrieval_feedback_from_row(updated).to_dict()

    def _insert_feedback(
        self,
        connection: sqlite3.Connection,
        *,
        receipt: sqlite3.Row,
        record_id: str,
        kind: str,
        label: str,
        reason: str,
        source: Mapping[str, Any],
        proposal_id: str | None,
        created_at: float,
    ) -> RetrievalFeedback:
        source_json = _json(source)
        source_sha256 = _sha256(source_json)
        feedback_key = _sha256(
            _json(
                {
                    "kind": kind,
                    "label": label,
                    "proposal_id": proposal_id,
                    "receipt_id": str(receipt["receipt_id"]),
                    "record_id": record_id,
                    "source_sha256": source_sha256,
                }
            )
        )
        existing = connection.execute(
            "SELECT * FROM memory_retrieval_feedback WHERE feedback_key = ?",
            (feedback_key,),
        ).fetchone()
        if existing is not None:
            return _retrieval_feedback_from_row(existing)
        feedback_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO memory_retrieval_feedback("
            "feedback_id, feedback_key, receipt_id, record_id, kind, label, query_sha256, "
            "surface, ranking_version, reason_sha256, source_json, source_sha256, proposal_id, "
            "state, created_at, updated_at, revoked_at, revocation_source_sha256) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL, '')",
            (
                feedback_id,
                feedback_key,
                str(receipt["receipt_id"]),
                str(record_id),
                kind,
                label,
                str(receipt["query_sha256"]),
                str(receipt["surface"]),
                str(receipt["ranking_version"]),
                _sha256(" ".join(str(reason or "").split())),
                source_json,
                source_sha256,
                proposal_id,
                created_at,
                created_at,
            ),
        )
        row = connection.execute(
            "SELECT * FROM memory_retrieval_feedback WHERE feedback_id = ?",
            (feedback_id,),
        ).fetchone()
        assert row is not None
        return _retrieval_feedback_from_row(row)

    def _feedback_penalties(
        self,
        query_sha256: str,
        surface: str,
    ) -> dict[str, tuple[float, tuple[str, ...]]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT feedback_id, record_id FROM memory_retrieval_feedback "
                "WHERE kind = 'relevance' AND state = 'active' "
                "AND query_sha256 = ? AND surface = ? ORDER BY created_at, feedback_id",
                (query_sha256, str(surface or "private")[:64]),
            ).fetchall()
        grouped: dict[str, list[str]] = {}
        for row in rows:
            grouped.setdefault(str(row["record_id"]), []).append(str(row["feedback_id"]))
        return {
            record_id: (min(0.30, 0.12 * len(feedback_ids)), tuple(feedback_ids))
            for record_id, feedback_ids in grouped.items()
        }

    def _record_retrieval(
        self,
        *,
        query: str,
        surface: str,
        project: str,
        people: Sequence[object],
        record_type: object,
        hits: Sequence[RetrievalHit],
        ranking_details: Mapping[str, Any],
        created_at: float,
    ) -> RetrievalReceipt:
        clean_people = _people(people)
        receipt_filters = dict(ranking_details)
        receipt_filters["people_count"] = len(clean_people)
        receipt_filters["people_sha256"] = [_sha256(person.casefold()) for person in clean_people]
        receipt = RetrievalReceipt(
            receipt_id=str(uuid.uuid4()),
            query_sha256=_sha256(query),
            surface=str(surface or "private")[:64],
            project=str(project or "")[:500],
            record_type=str(record_type) if record_type else None,
            ranking_version=RETRIEVAL_RANKING_VERSION,
            filters=receipt_filters,
            returned=tuple(
                {
                    "record_id": hit.record.record_id,
                    "score": hit.score,
                    "confidence": hit.record.confidence,
                    "source_sha256": _sha256(_json(hit.record.source)),
                    "components": dict(hit.components),
                    "reasons": list(hit.reasons),
                }
                for hit in hits
            ),
            created_at=created_at,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO memory_retrieval_receipts("
                "receipt_id, query_sha256, surface, project, record_type, filters_json, "
                "ranking_version, returned_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt.receipt_id,
                    receipt.query_sha256,
                    receipt.surface,
                    receipt.project,
                    receipt.record_type,
                    _json(receipt.filters),
                    receipt.ranking_version,
                    _json(list(receipt.returned)),
                    receipt.created_at,
                ),
            )
            connection.execute(
                "DELETE FROM memory_retrieval_receipts WHERE receipt_id IN ("
                "SELECT receipt_id FROM memory_retrieval_receipts "
                "ORDER BY created_at DESC, receipt_id DESC LIMIT -1 OFFSET ?)",
                (MAX_RECEIPTS,),
            )
        return receipt

    def evaluate_retrieval(
        self,
        cases: Sequence[Mapping[str, Any]],
        *,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Evaluate positive recall, negative abstention, and context flooding."""

        results = []
        recall_total = 0.0
        reciprocal_total = 0.0
        precision_total = 0.0
        positive_total = 0
        false_positive_total = 0
        negative_total = 0
        no_answer_total = 0
        no_answer_false_positives = 0
        context_budget_passes = 0
        flooding_total = 0
        feedback_case_count = 0
        for case_index, case in enumerate(cases, start=1):
            query = str(case.get("query") or "")
            surface = str(case.get("surface") or "private")
            expected = {str(value) for value in case.get("expected_record_ids") or []}
            expect_no_answer = bool(case.get("expect_no_answer", not expected))
            if expect_no_answer and expected:
                raise ValueError("no-answer evaluation cases cannot declare expected records")
            negative = {str(value) for value in case.get("negative_record_ids") or []}
            feedback = self._feedback_penalties(_sha256(" ".join(query.split())), surface)
            negative.update(feedback)
            feedback_ids = sorted(
                feedback_id
                for _penalty, record_feedback_ids in feedback.values()
                for feedback_id in record_feedback_ids
            )
            if feedback_ids:
                feedback_case_count += 1
            retrieved = self.retrieve(
                query,
                limit=limit,
                surface=surface,
                record_receipt=False,
            )
            returned = [hit.record.record_id for hit in retrieved]
            found = expected.intersection(returned)
            false_positives = negative.intersection(returned)
            recall = len(found) / len(expected) if expected else 0.0
            ranks = [returned.index(record_id) + 1 for record_id in found]
            reciprocal = 1.0 / min(ranks) if ranks else 0.0
            precision = len(found) / len(returned) if returned else (1.0 if expect_no_answer else 0.0)
            if expected:
                positive_total += 1
                recall_total += recall
                reciprocal_total += reciprocal
                precision_total += precision
            if expect_no_answer:
                no_answer_total += 1
                no_answer_false_positives += int(bool(returned))
            false_positive_total += len(false_positives)
            negative_total += len(negative)
            max_context_records = max(1, int(case.get("max_context_records") or 5))
            max_context_characters = max(
                1, int(case.get("max_context_characters") or 7_000)
            )
            max_context_tokens = max(1, int(case.get("max_context_tokens") or 1_800))
            context_characters = sum(len(hit.record.content) for hit in retrieved)
            context_tokens = math.ceil(context_characters / 4)
            context_passed = (
                len(retrieved) <= max_context_records
                and context_characters <= max_context_characters
                and context_tokens <= max_context_tokens
            )
            context_budget_passes += int(context_passed)
            flooding_total += int(not context_passed)
            results.append(
                {
                    "name": str(case.get("name") or f"case-{case_index}")[:200],
                    "query_sha256": _sha256(" ".join(query.split())),
                    "expect_no_answer": expect_no_answer,
                    "expected_record_ids": sorted(expected),
                    "returned_record_ids": returned,
                    "negative_record_ids": sorted(negative),
                    "negative_feedback_ids": feedback_ids,
                    "false_positive_record_ids": sorted(false_positives),
                    "recall": recall,
                    "reciprocal_rank": reciprocal,
                    "precision": precision,
                    "context_record_count": len(retrieved),
                    "context_character_count": context_characters,
                    "context_token_count": context_tokens,
                    "context_budget_passed": context_passed,
                    "flooded": not context_passed,
                }
            )
        count = len(results)
        false_positive_denominator = negative_total + no_answer_total
        false_positive_numerator = false_positive_total + no_answer_false_positives
        report = {
            "evaluation_version": "memory-retrieval-evaluation-v1",
            "corpus_sha256": _sha256(_json([dict(case) for case in cases])),
            "ranking_version": RETRIEVAL_RANKING_VERSION,
            "case_count": count,
            "positive_case_count": positive_total,
            "negative_case_count": no_answer_total,
            "recall_at_k": recall_total / positive_total if positive_total else 0.0,
            "mean_reciprocal_rank": (
                reciprocal_total / positive_total if positive_total else 0.0
            ),
            "precision_at_k": precision_total / positive_total if positive_total else 0.0,
            "false_positive_rate": (
                false_positive_numerator / false_positive_denominator
                if false_positive_denominator
                else 0.0
            ),
            "no_answer_accuracy": (
                1.0 - no_answer_false_positives / no_answer_total
                if no_answer_total
                else 0.0
            ),
            "context_budget_pass_rate": context_budget_passes / count if count else 0.0,
            "flooding_rate": flooding_total / count if count else 0.0,
            "feedback_case_count": feedback_case_count,
            "feedback_version": RETRIEVAL_FEEDBACK_VERSION,
            "limit": limit,
            "cases": results,
        }
        evaluation_id = str(uuid.uuid4())
        created_at = time.time()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO memory_retrieval_evaluations("
                "evaluation_id, ranking_version, report_json, created_at) VALUES (?, ?, ?, ?)",
                (evaluation_id, RETRIEVAL_RANKING_VERSION, _json(report), created_at),
            )
        return {"evaluation_id": evaluation_id, "created_at": created_at, **report}

    def retrieval_evaluations(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_retrieval_evaluations ORDER BY created_at DESC LIMIT ?",
                (min(500, max(1, int(limit))),),
            ).fetchall()
        return [
            {
                "evaluation_id": str(row["evaluation_id"]),
                "ranking_version": str(row["ranking_version"]),
                "created_at": float(row["created_at"]),
                **dict(_load_json(row["report_json"], {})),
            }
            for row in rows
        ]

    def export_legacy_projection(
        self,
        root: Path,
        *,
        actor: str,
    ) -> dict[str, Any]:
        """Write an immutable Markdown generation, then atomically publish it.

        A crash can leave an unreferenced staging/generation directory, but it
        cannot make ``CURRENT`` name a partial export. Existing Markdown is
        never read, modified, or deleted by this operation.
        """

        reviewer = _required_text(actor, "projection actor", 256)
        export_root = Path(root).expanduser()
        marker_name = "PROJECTION.json"
        if export_root.exists():
            entries = {item.name for item in export_root.iterdir()}
            unknown = entries - {"CURRENT", "generations", marker_name}
            if unknown:
                raise RuntimeError(
                    "legacy projection root contains files Serena does not own: "
                    + ", ".join(sorted(unknown))
                )
            marker = export_root / marker_name
            if entries and not marker.is_file():
                raise RuntimeError("legacy projection root has no Serena ownership marker")
            if marker.is_file():
                try:
                    marker_value = json.loads(marker.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise RuntimeError("legacy projection ownership marker is invalid") from exc
                if marker_value != {"authority": "memory-v2", "schema_version": 1}:
                    raise RuntimeError("legacy projection ownership marker is invalid")
        generations = export_root / "generations"
        generation_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:12]
        staging = generations / ("." + generation_id + ".tmp")
        final = generations / generation_id
        export_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = export_root / marker_name
        if not marker.exists():
            _write_synced(
                marker,
                json.dumps(
                    {"authority": "memory-v2", "schema_version": 1},
                    sort_keys=True,
                )
                + "\n",
            )
            _fsync_directory(export_root)
        generations.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging.mkdir(mode=0o700)
        try:
            records = self.records()
            identities = self._projection_identities(records)
            files: list[dict[str, Any]] = []
            by_type: dict[str, list[tuple[int, MemoryRecord, str]]] = {}
            for record in records:
                legacy_id, legacy_type = identities[record.record_id]
                slug = _legacy_slug(record.content)
                relative = Path(legacy_type) / f"{legacy_id:03d}-{slug}.md"
                body = _legacy_markdown(record, legacy_id=legacy_id, legacy_type=legacy_type)
                target = staging / relative
                _write_synced(target, body)
                digest = _sha256(body)
                files.append({"path": relative.as_posix(), "sha256": digest})
                by_type.setdefault(legacy_type, []).append((legacy_id, record, relative.as_posix()))
            index_lines = [
                "# Memory",
                "",
                f"Generated from Memory v2 generation `{generation_id}`.",
                "",
            ]
            for legacy_type in sorted(by_type):
                entries = sorted(by_type[legacy_type], key=lambda item: item[0])
                index_lines.extend([f"## {legacy_type.title()} ({len(entries)})", ""])
                for legacy_id, record, relative in entries:
                    summary = " ".join(record.content.split())[:80]
                    index_lines.append(f"- [#{legacy_id}](./{relative}) - {summary}")
                index_lines.append("")
            index = "\n".join(index_lines).rstrip() + "\n"
            _write_synced(staging / "INDEX.md", index)
            files.append({"path": "INDEX.md", "sha256": _sha256(index)})
            manifest = {
                "schema_version": 1,
                "generation_id": generation_id,
                "source_records_sha256": _sha256(_json([record.to_dict() for record in records])),
                "record_count": len(records),
                "actor": reviewer,
                "created_at": time.time(),
                "files": sorted(files, key=lambda item: item["path"]),
            }
            manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
            _write_synced(staging / "MANIFEST.json", manifest_text)
            _fsync_directory(staging)
            os.replace(staging, final)
            _fsync_directory(generations)
            pointer_tmp = export_root / (".CURRENT." + uuid.uuid4().hex + ".tmp")
            _write_synced(pointer_tmp, generation_id + "\n")
            os.replace(pointer_tmp, export_root / "CURRENT")
            _fsync_directory(export_root)
            return {**manifest, "path": str(final)}
        except BaseException:
            with suppress(OSError):
                shutil.rmtree(staging)
            raise

    def current_legacy_projection(self, root: Path) -> dict[str, Any] | None:
        export_root = Path(root).expanduser()
        try:
            generation_id = (export_root / "CURRENT").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}", generation_id):
            raise RuntimeError("legacy memory projection CURRENT pointer is invalid")
        directory = export_root / "generations" / generation_id
        manifest_path = directory / "MANIFEST.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("legacy memory projection manifest is unavailable") from exc
        for item in manifest.get("files") or []:
            relative = Path(str(item.get("path") or ""))
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("legacy memory projection manifest contains an unsafe path")
            if _file_sha256(directory / relative) != str(item.get("sha256") or ""):
                raise RuntimeError(f"legacy memory projection failed integrity check: {relative}")
        return {**manifest, "path": str(directory)}

    def _projection_identities(self, records: Sequence[MemoryRecord]) -> dict[str, tuple[int, str]]:
        identities: dict[str, tuple[int, str]] = {}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            reserved = {record.legacy_id for record in records if record.legacy_id is not None}
            maximum = max(reserved, default=0)
            row = connection.execute(
                "SELECT COALESCE(MAX(legacy_id), 0) AS maximum FROM memory_projection_ids"
            ).fetchone()
            next_id = max(maximum, int(row["maximum"] or 0)) + 1
            for record in records:
                legacy_type = (
                    record.legacy_type
                    if record.legacy_type in _LEGACY_TYPES
                    else _V2_LEGACY_TYPE_MAP[record.record_type]
                )
                if record.legacy_id is not None:
                    identities[record.record_id] = (record.legacy_id, legacy_type)
                    continue
                existing = connection.execute(
                    "SELECT legacy_id FROM memory_projection_ids WHERE record_id = ?",
                    (record.record_id,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO memory_projection_ids(record_id, legacy_id) VALUES (?, ?)",
                        (record.record_id, next_id),
                    )
                    legacy_id = next_id
                    next_id += 1
                else:
                    legacy_id = int(existing["legacy_id"])
                    if legacy_id in reserved:
                        connection.execute(
                            "UPDATE memory_projection_ids SET legacy_id = ? WHERE record_id = ?",
                            (next_id, record.record_id),
                        )
                        legacy_id = next_id
                        next_id += 1
                identities[record.record_id] = (legacy_id, legacy_type)
        return identities

    def _apply_proposal(
        self,
        connection: sqlite3.Connection,
        proposal: Mapping[str, Any],
        *,
        now: float,
    ) -> tuple[dict[str, Any], str | None]:
        operation = str(proposal["operation"])
        target_id = str(proposal.get("target_record_id") or "") or None
        candidate = dict(proposal.get("candidate") or {})
        source = dict(proposal["source"])
        previous = self._record_row(connection, target_id) if target_id else None
        if target_id:
            if previous is None:
                raise KeyError(f"memory proposal target disappeared before approval: {target_id}")
            expected = (proposal.get("diff") or {}).get("before")
            current = _record_from_row(previous).to_dict()
            current.pop("source", None)
            if not isinstance(expected, Mapping) or _json(current) != _json(dict(expected)):
                raise RuntimeError(
                    f"memory proposal target changed after review diff was created: {target_id}"
                )
        rollback = {"previous": _rollback_record(previous)}
        if operation == "forget":
            assert target_id is not None
            updated = connection.execute(
                "UPDATE memory_records SET status = 'forgotten', forgotten_at = ?, "
                "updated_at = ? WHERE record_id = ?",
                (now, now, target_id),
            ).rowcount
            _require_one_target(updated, target_id)
            return rollback, None
        if operation == "retain":
            assert target_id is not None
            updated = connection.execute(
                "UPDATE memory_records SET retention_until = ?, updated_at = ? WHERE record_id = ?",
                (candidate["retention_until"], now, target_id),
            ).rowcount
            _require_one_target(updated, target_id)
            return rollback, None

        record_id = str(uuid.uuid4())
        status = "contested" if operation == "contradict" else "current"
        valid_from = candidate.get("valid_from")
        valid_until = candidate.get("valid_until")
        if (
            operation in {"update", "supersede"}
            and valid_from is None
            and (valid_until is None or float(valid_until) > now)
        ):
            valid_from = now
        self._insert_record(
            connection,
            record_id=record_id,
            record_type=str(candidate["record_type"]),
            content=str(candidate["content"]),
            source=source,
            confidence=float(candidate["confidence"]),
            sensitivity=str(candidate["sensitivity"]),
            project=str(candidate.get("project") or ""),
            people=tuple(candidate.get("people") or ()),
            valid_from=valid_from,
            valid_until=valid_until,
            retention_until=candidate.get("retention_until"),
            status=status,
            legacy_id=None,
            legacy_type=None,
            created_at=now,
            updated_at=now,
        )
        if target_id and operation in {"update", "supersede"}:
            updated = connection.execute(
                "UPDATE memory_records SET status = 'superseded', valid_until = COALESCE(valid_until, ?), "
                "updated_at = ? WHERE record_id = ?",
                (now, now, target_id),
            ).rowcount
            _require_one_target(updated, target_id)
            self._insert_relation(connection, record_id, target_id, "supersedes", now)
        elif target_id and operation == "contradict":
            updated = connection.execute(
                "UPDATE memory_records SET status = 'contested', updated_at = ? WHERE record_id = ?",
                (now, target_id),
            ).rowcount
            _require_one_target(updated, target_id)
            self._insert_relation(connection, record_id, target_id, "contradicts", now)
        return rollback, record_id

    def _record_row(
        self, connection: sqlite3.Connection, record_id: str | None
    ) -> sqlite3.Row | None:
        if not record_id:
            return None
        return connection.execute(
            "SELECT * FROM memory_records WHERE record_id = ?", (record_id,)
        ).fetchone()

    def _insert_record(self, connection: sqlite3.Connection, **values: Any) -> None:
        connection.execute(
            """
            INSERT INTO memory_records(
                record_id, record_type, content, project, people_json, source_json,
                source_sha256, confidence, sensitivity, valid_from, valid_until,
                retention_until, status, legacy_id, legacy_type, created_at, updated_at,
                forgotten_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                values["record_id"],
                _record_type(values["record_type"]),
                _content(values["content"]),
                str(values.get("project") or "")[:500],
                _json(_people(values.get("people") or ())),
                _json(_source(values["source"])),
                _sha256(_json(values["source"])),
                _confidence(values.get("confidence")),
                _sensitivity(values.get("sensitivity")),
                values.get("valid_from"),
                values.get("valid_until"),
                values.get("retention_until"),
                str(values.get("status") or "current"),
                values.get("legacy_id"),
                values.get("legacy_type"),
                values["created_at"],
                values["updated_at"],
            ),
        )

    @staticmethod
    def _insert_relation(
        connection: sqlite3.Connection,
        source_record_id: str,
        target_record_id: str,
        kind: str,
        created_at: float,
    ) -> None:
        if kind not in RELATION_KINDS:
            raise ValueError("invalid memory relation")
        connection.execute(
            "INSERT OR IGNORE INTO memory_relations("
            "source_record_id, target_record_id, kind, created_at) VALUES (?, ?, ?, ?)",
            (source_record_id, target_record_id, kind, created_at),
        )

    @staticmethod
    def _require_proposal(connection: sqlite3.Connection, proposal_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM memory_proposals WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown memory proposal {proposal_id}")
        return row

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_records (
                    record_id TEXT PRIMARY KEY,
                    record_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    project TEXT NOT NULL DEFAULT '',
                    people_json TEXT NOT NULL DEFAULT '[]',
                    source_json TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    sensitivity TEXT NOT NULL,
                    valid_from REAL,
                    valid_until REAL,
                    retention_until REAL,
                    status TEXT NOT NULL,
                    legacy_id INTEGER,
                    legacy_type TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    forgotten_at REAL,
                    UNIQUE(legacy_type, legacy_id)
                );
                CREATE INDEX IF NOT EXISTS memory_records_current_idx
                    ON memory_records(status, record_type, updated_at);
                CREATE TABLE IF NOT EXISTS memory_relations (
                    source_record_id TEXT NOT NULL
                        REFERENCES memory_records(record_id) ON DELETE CASCADE,
                    target_record_id TEXT NOT NULL
                        REFERENCES memory_records(record_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(source_record_id, target_record_id, kind)
                );
                CREATE TABLE IF NOT EXISTS memory_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    proposal_key TEXT NOT NULL UNIQUE,
                    operation TEXT NOT NULL,
                    target_record_id TEXT,
                    candidate_json TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    diff_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reviewer TEXT,
                    decision_reason TEXT,
                    applied_record_id TEXT,
                    rollback_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    reviewed_at REAL
                );
                CREATE INDEX IF NOT EXISTS memory_proposals_state_idx
                    ON memory_proposals(state, created_at);
                CREATE TABLE IF NOT EXISTS memory_retrieval_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    query_sha256 TEXT NOT NULL,
                    surface TEXT NOT NULL,
                    project TEXT NOT NULL DEFAULT '',
                    record_type TEXT,
                    filters_json TEXT NOT NULL,
                    ranking_version TEXT NOT NULL,
                    returned_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memory_retrieval_receipts_time_idx
                    ON memory_retrieval_receipts(created_at);
                CREATE TABLE IF NOT EXISTS memory_retrieval_feedback (
                    feedback_id TEXT PRIMARY KEY,
                    feedback_key TEXT NOT NULL UNIQUE,
                    receipt_id TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL,
                    query_sha256 TEXT NOT NULL,
                    surface TEXT NOT NULL,
                    ranking_version TEXT NOT NULL,
                    reason_sha256 TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    proposal_id TEXT,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    revoked_at REAL,
                    revocation_source_sha256 TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS memory_retrieval_feedback_rank_idx
                    ON memory_retrieval_feedback(
                        query_sha256, surface, kind, state, record_id
                    );
                CREATE INDEX IF NOT EXISTS memory_retrieval_feedback_time_idx
                    ON memory_retrieval_feedback(created_at);
                CREATE TABLE IF NOT EXISTS memory_retrieval_evaluations (
                    evaluation_id TEXT PRIMARY KEY,
                    ranking_version TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_projection_ids (
                    record_id TEXT PRIMARY KEY REFERENCES memory_records(record_id),
                    legacy_id INTEGER NOT NULL UNIQUE
                );
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO memory_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        if os.name != "nt":
            with suppress(OSError):
                self.path.chmod(0o600)


def _candidate(value: Mapping[str, Any], *, required: bool) -> dict[str, Any]:
    if not value and not required:
        return {}
    content = _content(value.get("content"))
    if required and not content:
        raise ValueError("memory proposal candidate content is required")
    record_type = _record_type(value.get("record_type"))
    valid_from = _optional_number(value.get("valid_from"))
    valid_until = _optional_number(value.get("valid_until"))
    if valid_from is not None and valid_until is not None and valid_until <= valid_from:
        raise ValueError("valid_until must be later than valid_from")
    return {
        "record_type": record_type,
        "content": content,
        "confidence": _confidence(value.get("confidence", 0.75)),
        "sensitivity": _sensitivity(value.get("sensitivity", "personal")),
        "project": str(value.get("project") or "")[:500],
        "people": _people(value.get("people") or ()),
        "valid_from": valid_from,
        "valid_until": valid_until,
        "retention_until": _optional_number(value.get("retention_until")),
    }


def _source(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("memory source must be an object")
    clean = json.loads(_json(dict(value)))
    kind = str(clean.get("kind") or "").strip()
    locator = str(clean.get("locator") or clean.get("session_id") or "").strip()
    digest = str(clean.get("content_sha256") or clean.get("excerpt_sha256") or "").strip()
    if not kind or not locator or not digest:
        raise ValueError("memory source requires kind, locator, and a content digest")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        raise ValueError("memory source digest must be SHA-256")
    encoded = _json(clean)
    if len(encoded) > MAX_SOURCE_CHARS:
        raise ValueError("memory source is too large")
    return clean


def source_receipt(
    *,
    kind: str,
    locator: str,
    source_text: str,
    session_id: str = "",
    surface: str = "",
) -> dict[str, Any]:
    """Build provenance without persisting raw transcript text."""

    return {
        "kind": _required_text(kind, "source kind", 128),
        "locator": _required_text(locator, "source locator", 1_000),
        "content_sha256": _sha256(str(source_text or "")),
        "session_id": str(session_id or "")[:256],
        "surface": str(surface or "")[:64],
    }


def _proposal_diff(
    operation: str,
    current: MemoryRecord | None,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    before = current.to_dict() if current is not None else None
    if before is not None:
        before.pop("source", None)
    return {
        "operation": operation,
        "before": before,
        "after": dict(candidate) if candidate else None,
    }


def _record_from_row(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        record_id=str(row["record_id"]),
        record_type=str(row["record_type"]),
        content=str(row["content"]),
        project=str(row["project"] or ""),
        people=tuple(_load_json(row["people_json"], [])),
        source=dict(_load_json(row["source_json"], {})),
        confidence=float(row["confidence"]),
        sensitivity=str(row["sensitivity"]),
        valid_from=_optional_number(row["valid_from"]),
        valid_until=_optional_number(row["valid_until"]),
        retention_until=_optional_number(row["retention_until"]),
        status=str(row["status"]),
        legacy_id=_integer(row["legacy_id"]),
        legacy_type=str(row["legacy_type"]) if row["legacy_type"] is not None else None,
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        forgotten_at=_optional_number(row["forgotten_at"]),
    )


def _proposal_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "proposal_id": str(row["proposal_id"]),
        "operation": str(row["operation"]),
        "target_record_id": str(row["target_record_id"] or "") or None,
        "candidate": _load_json(row["candidate_json"], {}),
        "source": _load_json(row["source_json"], {}),
        "diff": _load_json(row["diff_json"], {}),
        "state": str(row["state"]),
        "reviewer": str(row["reviewer"] or ""),
        "decision_reason": str(row["decision_reason"] or ""),
        "applied_record_id": str(row["applied_record_id"] or "") or None,
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "reviewed_at": _optional_number(row["reviewed_at"]),
    }


def _retrieval_receipt_from_row(row: sqlite3.Row) -> RetrievalReceipt:
    returned = _load_json(row["returned_json"], [])
    return RetrievalReceipt(
        receipt_id=str(row["receipt_id"]),
        query_sha256=str(row["query_sha256"]),
        surface=str(row["surface"]),
        project=str(row["project"] or ""),
        record_type=str(row["record_type"]) if row["record_type"] else None,
        ranking_version=str(row["ranking_version"]),
        filters=dict(_load_json(row["filters_json"], {})),
        returned=tuple(dict(item) for item in returned if isinstance(item, Mapping)),
        created_at=float(row["created_at"]),
    )


def _retrieval_feedback_from_row(row: sqlite3.Row) -> RetrievalFeedback:
    return RetrievalFeedback(
        feedback_id=str(row["feedback_id"]),
        receipt_id=str(row["receipt_id"]),
        record_id=str(row["record_id"]),
        kind=str(row["kind"]),
        label=str(row["label"]),
        query_sha256=str(row["query_sha256"]),
        surface=str(row["surface"]),
        ranking_version=str(row["ranking_version"]),
        reason_sha256=str(row["reason_sha256"]),
        source_sha256=str(row["source_sha256"]),
        proposal_id=str(row["proposal_id"]) if row["proposal_id"] else None,
        state=str(row["state"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        revoked_at=_optional_number(row["revoked_at"]),
    )


def _require_retrieval_target(
    connection: sqlite3.Connection,
    receipt_id: str,
    record_id: str,
) -> sqlite3.Row:
    clean_receipt_id = _required_text(receipt_id, "retrieval receipt id", 256)
    clean_record_id = _required_text(record_id, "retrieval feedback record id", 256)
    receipt = connection.execute(
        "SELECT * FROM memory_retrieval_receipts WHERE receipt_id = ?",
        (clean_receipt_id,),
    ).fetchone()
    if receipt is None:
        raise KeyError(f"unknown retrieval receipt {clean_receipt_id}")
    returned = _load_json(receipt["returned_json"], [])
    returned_ids = {
        str(item.get("record_id") or "") for item in returned if isinstance(item, Mapping)
    }
    if clean_record_id not in returned_ids:
        raise ValueError("retrieval feedback target was not returned by that receipt")
    return receipt


def _rollback_record(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "record_id": str(row["record_id"]),
        "status": str(row["status"]),
        "forgotten_at": _optional_number(row["forgotten_at"]),
        "retention_until": _optional_number(row["retention_until"]),
        "valid_until": _optional_number(row["valid_until"]),
    }


def _require_one_target(updated: int, record_id: str) -> None:
    if updated != 1:
        raise RuntimeError(f"memory proposal target changed during approval: {record_id}")


def _tokens(value: str) -> set[str]:
    return {token for token in _WORDS.findall(value.lower()) if len(token) > 1}


def _contains_entity_phrase(searchable: str, entity: str) -> bool:
    entity_tokens = _WORDS.findall(str(entity or "").casefold())
    if not entity_tokens:
        return False
    searchable_tokens = _WORDS.findall(str(searchable or "").casefold())
    width = len(entity_tokens)
    return any(
        searchable_tokens[index : index + width] == entity_tokens
        for index in range(len(searchable_tokens) - width + 1)
    )


def _overlap_score(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left.intersection(right)) / math.sqrt(len(left) * len(right))


def _external_score(values: Mapping[str, float], *names: str) -> float:
    score = _optional_external_score(values, *names)
    return score if score is not None else 0.0


def _optional_external_score(values: Mapping[str, float], *names: str) -> float | None:
    for name in names:
        if name not in values:
            continue
        try:
            score = float(values[name])
        except (TypeError, ValueError):
            continue
        if math.isfinite(score):
            return max(0.0, min(1.0, score))
    return None


def _record_type(value: object) -> str:
    clean = str(value or "episode").strip().lower()
    if clean not in RECORD_TYPES:
        raise ValueError("invalid memory record type")
    return clean


def _sensitivity(value: object) -> str:
    clean = str(value or "personal").strip().lower()
    if clean not in SENSITIVITY_LEVELS:
        raise ValueError("invalid memory sensitivity")
    return clean


def _confidence(value: object) -> float:
    try:
        clean = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("memory confidence must be a number") from exc
    if not 0.0 <= clean <= 1.0:
        raise ValueError("memory confidence must be between zero and one")
    return clean


def _content(value: object) -> str:
    clean = str(value or "").strip()
    if len(clean) > MAX_CONTENT_CHARS:
        raise ValueError("memory content is too long")
    return clean


def _people(values: Sequence[object]) -> list[str]:
    if isinstance(values, (str, bytes)):
        values = [values]
    return list(
        dict.fromkeys(
            clean for value in values if (clean := " ".join(str(value or "").split())[:200])
        )
    )[:100]


def _required_text(value: object, name: str, limit: int) -> str:
    clean = " ".join(str(value or "").split())
    if not clean:
        raise ValueError(f"{name} is required")
    return clean[:limit]


def _identifier(value: object) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > 256 or any(char.isspace() for char in clean):
        raise ValueError("invalid memory identifier")
    return clean


def _optional_number(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("memory timestamp must be numeric") from exc


def _integer(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _timestamp(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load_json(value: object, default: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return default


def _legacy_slug(content: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", content.lower()).strip("-")[:50].rstrip("-")
    return slug or "memory"


def _legacy_time(value: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def _legacy_markdown(
    record: MemoryRecord,
    *,
    legacy_id: int,
    legacy_type: str,
) -> str:
    source_locator = str(record.source.get("locator") or "").replace("\n", " ")[:1_000]
    return (
        "---\n"
        f"id: {legacy_id}\n"
        f"type: {legacy_type}\n"
        f"created: {_legacy_time(record.created_at)}\n"
        f"updated: {_legacy_time(record.updated_at)}\n"
        f"source_record_id: {record.record_id}\n"
        f"source_locator: {source_locator}\n"
        f"confidence: {record.confidence:.3f}\n"
        "authority: memory-v2-projection\n"
        "---\n\n" + record.content + "\n"
    )


def _write_synced(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
