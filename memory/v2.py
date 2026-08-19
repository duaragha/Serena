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

DEFAULT_DB_PATH = Path.home() / ".local" / "state" / "serena" / "memory-v2.sqlite3"
SCHEMA_VERSION = 2

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
RETRIEVAL_RANKING_VERSION = "local-hybrid-v1"

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
_CONCEPT_GROUPS = (
    ("prefer", "preference", "like", "favour", "favorite", "favourite", "choose"),
    ("car", "cars", "vehicle", "vehicles", "automobile", "automobiles"),
    ("doctor", "physician", "gp", "clinician"),
    ("therapist", "therapy", "counsellor", "counselor", "psychotherapy"),
    ("job", "work", "employment", "career"),
    ("home", "house", "apartment", "condo", "residence"),
    ("phone", "mobile", "iphone", "android", "handset"),
    ("meeting", "appointment", "session", "call"),
    ("remove", "delete", "forget", "erase", "purge"),
    ("deadline", "due", "finish", "complete", "commitment"),
)
_CONCEPT_BY_WORD = {word: f"concept:{group[0]}" for group in _CONCEPT_GROUPS for word in group}
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
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "record": self.record.to_dict(),
            "score": self.score,
            "literal_score": self.literal_score,
            "semantic_score": self.semantic_score,
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
    returned: tuple[dict[str, Any], ...]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["returned"] = list(self.returned)
        return value


class MemoryV2Store:
    """SQLite authority for v2 records and reviewed proposal application."""

    def __init__(self, path: Path | None = None) -> None:
        configured = os.environ.get("SERENA_MEMORY_V2_DB_PATH", "").strip()
        self.path = Path(path or configured or DEFAULT_DB_PATH).expanduser()
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

    def retrieve(
        self,
        query: str,
        *,
        limit: int = 8,
        surface: str = "private",
        project: str = "",
        people: Sequence[str] = (),
        record_type: str | None = None,
        now: float | None = None,
        record_receipt: bool = True,
    ) -> list[RetrievalHit]:
        """Combine literal and local semantic signals with validity and state."""

        clean_query = " ".join(str(query or "").split())
        if not clean_query:
            return []
        current = time.time() if now is None else float(now)
        q_words = _tokens(clean_query)
        q_semantic = _semantic_tokens(clean_query)
        wanted_people = {str(person).strip().lower() for person in people if str(person).strip()}
        hits: list[RetrievalHit] = []
        for record in self.records(record_type=record_type):
            if record.valid_from is not None and record.valid_from > current:
                continue
            if record.valid_until is not None and record.valid_until <= current:
                continue
            if record.retention_until is not None and record.retention_until <= current:
                continue
            if surface != "private" and record.sensitivity != "public":
                continue
            words = _tokens(record.content)
            semantic = _semantic_tokens(record.content)
            literal = _overlap_score(q_words, words)
            semantic_score = _cosine(q_semantic, semantic)
            project_score = 1.0 if project and record.project.lower() == project.lower() else 0.0
            record_people = {person.lower() for person in record.people}
            people_score = _overlap_score(wanted_people, record_people) if wanted_people else 0.0
            age_days = max(0.0, (current - record.updated_at) / 86_400.0)
            recency = math.exp(-age_days / 365.0)
            state_priority = 1.0 if record.record_type == "commitment" else 0.0
            score = (
                0.38 * literal
                + 0.38 * semantic_score
                + 0.08 * recency
                + 0.07 * project_score
                + 0.05 * people_score
                + 0.04 * state_priority
            ) * record.confidence
            if literal <= 0 and semantic_score <= 0 and project_score <= 0 and people_score <= 0:
                continue
            reasons = []
            if literal > 0:
                reasons.append(f"literal:{literal:.3f}")
            if semantic_score > 0:
                reasons.append(f"local_semantic:{semantic_score:.3f}")
            if project_score:
                reasons.append("project")
            if people_score:
                reasons.append("people")
            if state_priority:
                reasons.append("active_state_priority")
            reasons.append(f"confidence:{record.confidence:.3f}")
            hits.append(
                RetrievalHit(
                    record=record,
                    score=round(score, 6),
                    literal_score=round(literal, 6),
                    semantic_score=round(semantic_score, 6),
                    reasons=tuple(reasons),
                )
            )
        hits.sort(key=lambda hit: (-hit.score, -hit.record.updated_at, hit.record.record_id))
        selected = hits[: min(MAX_RETRIEVAL_RESULTS, max(1, int(limit)))]
        if record_receipt:
            self._record_retrieval(
                query=clean_query,
                surface=surface,
                project=project,
                people=people,
                record_type=record_type,
                hits=selected,
                created_at=current,
            )
        return selected

    def retrieve_with_receipt(self, query: str, **filters: Any) -> dict[str, Any]:
        """Retrieve once and return the exact durable receipt for what was exposed."""

        filters.pop("record_receipt", None)
        hits = self.retrieve(query, record_receipt=False, **filters)
        receipt_time = filters.get("now")
        receipt = self._record_retrieval(
            query=" ".join(str(query or "").split()),
            surface=str(filters.get("surface") or "private"),
            project=str(filters.get("project") or ""),
            people=filters.get("people") or (),
            record_type=filters.get("record_type"),
            hits=hits,
            created_at=time.time() if receipt_time is None else float(receipt_time),
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

    def _record_retrieval(
        self,
        *,
        query: str,
        surface: str,
        project: str,
        people: Sequence[object],
        record_type: object,
        hits: Sequence[RetrievalHit],
        created_at: float,
    ) -> RetrievalReceipt:
        receipt = RetrievalReceipt(
            receipt_id=str(uuid.uuid4()),
            query_sha256=_sha256(query),
            surface=str(surface or "private")[:64],
            project=str(project or "")[:500],
            record_type=str(record_type) if record_type else None,
            ranking_version=RETRIEVAL_RANKING_VERSION,
            returned=tuple(
                {
                    "record_id": hit.record.record_id,
                    "score": hit.score,
                    "confidence": hit.record.confidence,
                    "source_sha256": _sha256(_json(hit.record.source)),
                    "reasons": list(hit.reasons),
                }
                for hit in hits
            ),
            created_at=created_at,
        )
        filters = {"people": _people(people)}
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
                    _json(filters),
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
        """Evaluate recall and reciprocal rank on an inspectable local corpus."""

        results = []
        recall_total = 0.0
        reciprocal_total = 0.0
        for case in cases:
            query = str(case.get("query") or "")
            expected = {str(value) for value in case.get("expected_record_ids") or []}
            returned = [
                hit.record.record_id
                for hit in self.retrieve(query, limit=limit, record_receipt=False)
            ]
            found = expected.intersection(returned)
            recall = len(found) / len(expected) if expected else 1.0
            ranks = [returned.index(record_id) + 1 for record_id in found]
            reciprocal = 1.0 / min(ranks) if ranks else 0.0
            recall_total += recall
            reciprocal_total += reciprocal
            results.append(
                {
                    "name": str(case.get("name") or query)[:200],
                    "expected_record_ids": sorted(expected),
                    "returned_record_ids": returned,
                    "recall": recall,
                    "reciprocal_rank": reciprocal,
                }
            )
        count = len(results)
        report = {
            "case_count": count,
            "recall_at_k": recall_total / count if count else 0.0,
            "mean_reciprocal_rank": reciprocal_total / count if count else 0.0,
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
            valid_from=candidate.get("valid_from"),
            valid_until=candidate.get("valid_until"),
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
        returned=tuple(dict(item) for item in returned if isinstance(item, Mapping)),
        created_at=float(row["created_at"]),
    )


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


def _semantic_tokens(value: str) -> dict[str, float]:
    tokens = _tokens(value)
    features: dict[str, float] = {}
    for token in tokens:
        stem = _stem(token)
        features[f"stem:{stem}"] = 0.5
        concept = _CONCEPT_BY_WORD.get(token) or _CONCEPT_BY_WORD.get(stem)
        if concept:
            features[concept] = 1.0
    return features


def _stem(token: str) -> str:
    for suffix in ("ing", "ments", "ment", "ed", "ies", "es", "s"):
        if token.endswith(suffix) and len(token) > len(suffix) + 3:
            return token[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return token


def _overlap_score(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left.intersection(right)) / math.sqrt(len(left) * len(right))


def _cosine(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    if not left or not right:
        return 0.0
    numerator = sum(value * right.get(key, 0.0) for key, value in left.items())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


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
