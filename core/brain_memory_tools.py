"""Serena's own hands for her memory and knowledge base, on Raghav's word.

Kept out of core/brain_tools.py on purpose: that module is read-only by
construction and its docstring promises so. These are brokered writes,
gated by core.memory_authority against the real spoken turn, in the same
pattern as core/brain_work_tools.py.

Raghav's instruction, verbatim: "should be able to add, delete, edit
memories, knowledge everything honestly if i tell her to." The broker holds
the "if i tell her to" part; everything else is hers.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import create_sdk_mcp_server, tool
from mcp.types import ToolAnnotations

from core.brain_laptop_tools import current_turn, previous_user_turn_text, recent_turn_texts
from core.memory_authority import authorize

_BROKERED_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
_BROKERED_DELETE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)
_LOCAL_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

_REFUSAL = (
    "NOT DONE. Reason: {reason}. Tell Raghav plainly that you could not do it "
    "and why. Do not say it is saved, changed, or deleted."
)

_V2_TYPES = (
    "semantic_fact",
    "episode",
    "procedure",
    "commitment",
    "preference",
    "correction",
)
_REVIEW_SIGNAL = re.compile(
    r"\b(?:approve|accept|apply|reject|decline|roll\s*back|rollback|undo)\b",
    re.IGNORECASE,
)
_MIGRATION_SIGNAL = re.compile(r"\b(?:migrate|migration|upgrade|import)\b", re.IGNORECASE)
_EXPORT_SIGNAL = re.compile(r"\b(?:export|projection|project|markdown)\b", re.IGNORECASE)


def _text(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}]}


@tool(
    "save_memory",
    "Deprecated fail-closed legacy write. Memory changes require a visible "
    "Memory v2 proposal and explicit review through propose_memory_change and "
    "review_memory_proposal; this tool never mutates memory.",
    {"content": str, "type": str},
    annotations=_BROKERED_WRITE,
)
async def save_memory(args):
    content = str(args.get("content") or "").strip()
    if not content:
        return _text(_REFUSAL.format(reason="empty content"))
    return _text(
        _REFUSAL.format(
            reason="direct legacy memory writes are disabled; create and review a Memory v2 proposal"
        )
    )


@tool(
    "edit_memory",
    "Deprecated fail-closed legacy write. Memory corrections require a visible "
    "Memory v2 proposal and explicit review; this tool never mutates memory.",
    {"memory_id": int, "content": str},
    annotations=_BROKERED_WRITE,
)
async def edit_memory(args):
    content = str(args.get("content") or "").strip()
    try:
        int(args.get("memory_id"))
    except (TypeError, ValueError):
        return _text(_REFUSAL.format(reason="no valid memory id"))
    if not content:
        return _text(_REFUSAL.format(reason="empty replacement content"))
    return _text(
        _REFUSAL.format(
            reason="direct legacy memory writes are disabled; create and review a Memory v2 proposal"
        )
    )


@tool(
    "delete_memory",
    "Deprecated fail-closed legacy write. Forgetting requires a visible Memory "
    "v2 proposal and explicit review; this tool never mutates memory.",
    {"memory_id": int},
    annotations=_BROKERED_DELETE,
)
async def delete_memory(args):
    try:
        int(args.get("memory_id"))
    except (TypeError, ValueError):
        return _text(_REFUSAL.format(reason="no valid memory id"))
    return _text(
        _REFUSAL.format(
            reason="direct legacy memory writes are disabled; create and review a Memory v2 proposal"
        )
    )


@tool(
    "save_knowledge",
    "Write or replace one markdown note inside a knowledge topic when Raghav "
    "asks you on a spoken turn to save research or notes. topic is a "
    "kebab-case slug (a new slug creates the topic), filename is a .md name "
    "like notes.md. A broker independently re-reads his actual words.",
    {"topic": str, "filename": str, "content": str},
    annotations=_BROKERED_WRITE,
)
async def save_knowledge(args):
    topic = str(args.get("topic") or "").strip().strip("/").lower()
    filename = str(args.get("filename") or "notes.md").strip()
    content = str(args.get("content") or "").strip()
    if not topic or not content:
        return _text(_REFUSAL.format(reason="topic and content are both required"))
    if "/" in filename or not filename.endswith(".md"):
        return _text(_REFUSAL.format(reason="filename must be a plain .md name"))
    if not all(part.isalnum() for part in topic.replace("-", " ").split()):
        return _text(_REFUSAL.format(reason=f"{topic!r} is not a clean topic slug"))
    decision = authorize(
        "save_knowledge",
        origin=current_turn(),
        destructive=False,
        detail=f"{topic}/{filename}",
        recent_texts=recent_turn_texts(),
    )
    if not decision.allowed:
        return _text(_REFUSAL.format(reason=decision.reason))

    def _write() -> str:
        from core.config import KNOWLEDGE_DIR

        directory = KNOWLEDGE_DIR / topic
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(content + "\n", encoding="utf-8")
        return str(directory / filename)

    path = await asyncio.to_thread(_write)
    return _text(f"SAVED. knowledge note at {path}.")


@tool(
    "delete_knowledge_topic",
    "Delete an entire knowledge topic by slug when Raghav asks you on a "
    "spoken turn to remove it. Confirm the slug with search_knowledge first "
    "and tell him what you are deleting. Permanent. A broker independently "
    "re-reads his actual words.",
    {"topic": str},
    annotations=_BROKERED_DELETE,
)
async def delete_knowledge_topic(args):
    topic = str(args.get("topic") or "").strip().strip("/")
    if not topic:
        return _text(_REFUSAL.format(reason="no topic given"))
    decision = authorize(
        "delete_knowledge_topic",
        origin=current_turn(),
        destructive=True,
        detail=topic,
        recent_texts=recent_turn_texts(),
    )
    if not decision.allowed:
        return _text(_REFUSAL.format(reason=decision.reason))

    def _delete() -> bool:
        from knowledge.reader import delete_topic

        return delete_topic(topic)

    if not await asyncio.to_thread(_delete):
        return _text(_REFUSAL.format(reason=f"no knowledge topic called {topic!r}"))
    return _text(f"DELETED. knowledge topic {topic!r} is gone.")


@tool(
    "propose_memory_change",
    "Create a visible Memory v2 proposal from the current spoken turn. This never "
    "changes canonical memory. operation is add, supersede, contradict, forget, or "
    "retain. For add/supersede/contradict provide complete candidate content and a "
    "v2 record type. For every other operation provide target_record_id. retain also "
    "requires retention_until as an ISO 8601 date or Unix timestamp. Return the proposal "
    "and diff to Raghav before asking him to approve or reject it.",
    {
        "operation": str,
        "record_type": str,
        "content": str,
        "target_record_id": str,
        "confidence": str,
        "sensitivity": str,
        "retention_until": str,
    },
    annotations=_BROKERED_WRITE,
)
async def propose_memory_change(args):
    origin = current_turn()
    decision = authorize(
        "propose_memory_change",
        origin=origin,
        destructive=False,
        detail=str(args.get("content") or "")[:200],
        recent_texts=recent_turn_texts(),
    )
    if not decision.allowed:
        return _text(_REFUSAL.format(reason=decision.reason))
    operation = str(args.get("operation") or "add").strip().lower()
    record_type = str(args.get("record_type") or "episode").strip().lower()
    content = str(args.get("content") or "").strip()
    target = str(args.get("target_record_id") or "").strip()
    if record_type not in _V2_TYPES:
        return _text(_REFUSAL.format(reason="invalid Memory v2 record type"))
    try:
        confidence = float(str(args.get("confidence") or "0.75"))
    except ValueError:
        return _text(_REFUSAL.format(reason="confidence must be between zero and one"))
    sensitivity = str(args.get("sensitivity") or "personal").strip().lower()
    retention_until = str(args.get("retention_until") or "").strip()
    retention_timestamp = None
    if retention_until:
        try:
            retention_timestamp = float(retention_until)
        except ValueError:
            try:
                retention_timestamp = datetime.fromisoformat(
                    retention_until.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                return _text(
                    _REFUSAL.format(
                        reason="retention_until must be an ISO 8601 date or Unix timestamp"
                    )
                )

    def _propose() -> dict:
        from memory.v2 import MemoryV2Store, source_receipt

        spoken = str(origin.get("text") or "")
        locator = str(origin.get("turn_id") or origin.get("call_id") or "spoken-turn")
        source = source_receipt(
            kind="spoken_turn",
            locator=locator,
            source_text=spoken,
            session_id=str(origin.get("call_id") or ""),
            surface=str(origin.get("protocol") or "voice"),
        )
        candidate = None
        if operation in {"add", "update", "supersede", "contradict", "retain"}:
            candidate = {
                "record_type": record_type,
                "content": content,
                "confidence": confidence,
                "sensitivity": sensitivity,
                "retention_until": retention_timestamp,
            }
        store = MemoryV2Store()
        proposal = store.create_proposal(
            operation=operation,
            target_record_id=target or None,
            candidate=candidate,
            source=source,
        )
        try:
            store.flush_control_outbox()
        except Exception:
            proposal["control_event_pending"] = True
        return proposal

    try:
        proposal = await asyncio.to_thread(_propose)
    except (KeyError, RuntimeError, ValueError) as exc:
        return _text(_REFUSAL.format(reason=str(exc)))
    return _text("PROPOSED, NOT APPLIED.\n" + json.dumps(proposal, indent=2, sort_keys=True))


@tool(
    "list_memory_proposals",
    "List visible Memory v2 proposals and their before/after diffs. Read-only. "
    "Use state proposed, approved, rejected, or rolled_back.",
    {"state": str},
    annotations=_LOCAL_READ_ONLY,
)
async def list_memory_proposals(args):
    state = str(args.get("state") or "proposed").strip().lower()

    def _list() -> list[dict]:
        from memory.v2 import MemoryV2Store

        return MemoryV2Store().proposals(state=state, limit=50)

    try:
        proposals = await asyncio.to_thread(_list)
    except ValueError as exc:
        return _text(f"NOT READ. Reason: {exc}.")
    return _text(json.dumps(proposals, indent=2, sort_keys=True)[:12_000])


@tool(
    "review_memory_proposal",
    "Approve, reject, or roll back one Memory v2 proposal only when Raghav "
    "explicitly says that review action on the live spoken turn. Approval applies "
    "the displayed diff transactionally. Rejection never changes canonical memory. "
    "Rollback retracts the applied version and restores the prior state.",
    {"proposal_id": str, "action": str, "reason": str},
    annotations=_BROKERED_DELETE,
)
async def review_memory_proposal(args):
    origin = current_turn()
    recent = recent_turn_texts()
    window = [str(origin.get("text") or ""), *recent]
    if not any(_REVIEW_SIGNAL.search(text) for text in window):
        return _text(
            _REFUSAL.format(
                reason="the recent conversation did not explicitly approve, reject, or undo a proposal"
            )
        )
    decision = authorize(
        "review_memory_proposal",
        origin=origin,
        destructive=False,
        detail=str(args.get("proposal_id") or ""),
        recent_texts=recent,
    )
    if not decision.allowed:
        return _text(_REFUSAL.format(reason=decision.reason))
    proposal_id = str(args.get("proposal_id") or "").strip()
    action = str(args.get("action") or "").strip().lower()
    reason = str(args.get("reason") or "reviewed on Raghav's spoken turn").strip()

    def _review() -> dict:
        from memory.v2 import MemoryV2Store

        store = MemoryV2Store()
        if action == "approve":
            result = store.approve_proposal(proposal_id, reviewer="Raghav", reason=reason)
        elif action == "reject":
            result = store.reject_proposal(proposal_id, reviewer="Raghav", reason=reason)
        elif action in {"rollback", "undo"}:
            result = store.rollback_proposal(proposal_id, reviewer="Raghav", reason=reason)
        else:
            raise ValueError("action must be approve, reject, or rollback")
        try:
            store.flush_control_outbox()
        except Exception:
            result["control_event_pending"] = True
        return result

    try:
        result = await asyncio.to_thread(_review)
    except (KeyError, RuntimeError, ValueError) as exc:
        return _text(_REFUSAL.format(reason=str(exc)))
    return _text("REVIEW RECORDED.\n" + json.dumps(result, indent=2, sort_keys=True))


@tool(
    "search_memory_v2",
    "Search the canonical memory authority with local ranking. The compatibility "
    "name is retained while inactive v2 safely falls back to legacy Markdown. "
    "Returns record and source ids, score explanations, and a receipt. Read-only.",
    {"query": str, "surface": str},
    annotations=_LOCAL_READ_ONLY,
)
async def search_memory_v2(args):
    query = str(args.get("query") or "").strip()
    surface = str(args.get("surface") or "private").strip().lower()
    previous = previous_user_turn_text()

    def _search() -> dict:
        from memory.retrieval import retrieve_memory

        return retrieve_memory(
            query,
            surface=surface,
            recent_context=(previous,) if previous else (),
        ).v2_compatibility_dict()

    results = await asyncio.to_thread(_search)
    return _text(json.dumps(results, indent=2, sort_keys=True)[:12_000])


@tool(
    "record_memory_feedback",
    "Record feedback about one record returned by a persisted Memory v2 retrieval "
    "receipt. Explicit irrelevant/not-relevant feedback creates a reversible ranking "
    "example only. Explicit factual correction creates a visible proposal for review "
    "and never changes canonical memory. Intent is verified against the current or "
    "immediately previous genuine user turn; bare 'wrong' defaults to non-mutating "
    "relevance feedback. corrected_content is required for factual correction.",
    {
        "receipt_id": str,
        "record_id": str,
        "corrected_content": str,
        "reason": str,
    },
    annotations=_BROKERED_WRITE,
)
async def record_memory_feedback(args):
    origin = current_turn()
    previous = previous_user_turn_text()
    utterances = tuple(
        value for value in (str(origin.get("text") or ""), previous) if value
    )
    grounded_text = " ".join(utterances)
    corrected_content = str(args.get("corrected_content") or "").strip()
    from memory.feedback import classify_feedback

    intent = next(
        (
            candidate
            for text in utterances
            if (candidate := classify_feedback(text, corrected_content=corrected_content))
            is not None
        ),
        None,
    )
    if intent is None or intent.kind == "revoke":
        return _text(
            _REFUSAL.format(
                reason="the current conversation did not explicitly mark a returned memory irrelevant or factually wrong"
            )
        )
    if intent.kind == "factual_correction" and not corrected_content:
        return _text(
            _REFUSAL.format(
                reason="a factual correction needs the complete corrected memory for review"
            )
        )
    receipt_id = str(args.get("receipt_id") or "").strip()
    record_id = str(args.get("record_id") or "").strip()
    decision = authorize(
        "record_memory_feedback",
        origin=origin,
        destructive=False,
        detail=f"{receipt_id}:{record_id}",
        recent_texts=[previous] if previous else [],
    )
    if not decision.allowed:
        return _text(_REFUSAL.format(reason=decision.reason))

    def _record() -> tuple[str, dict]:
        from memory.v2 import MemoryV2Store, source_receipt

        source = source_receipt(
            kind="spoken_turn",
            locator=str(origin.get("turn_id") or origin.get("call_id") or "spoken-turn"),
            source_text=grounded_text,
            session_id=str(origin.get("call_id") or ""),
            surface=str(origin.get("protocol") or "voice"),
        )
        source["feedback_classifier_version"] = intent.classifier_version
        source["feedback_classifier_rule"] = intent.rule
        store = MemoryV2Store()
        if intent.kind == "relevance":
            result = store.record_relevance_feedback(
                receipt_id,
                record_id,
                reason=str(args.get("reason") or grounded_text),
                source=source,
            )
            return "RELEVANCE FEEDBACK RECORDED. Canonical memory unchanged.\n", result
        result = store.propose_factual_correction(
            receipt_id,
            record_id,
            corrected_content=corrected_content,
            reason=str(args.get("reason") or grounded_text),
            source=source,
        )
        return "CORRECTION PROPOSED, NOT APPLIED.\n", result

    try:
        prefix, result = await asyncio.to_thread(_record)
    except (KeyError, RuntimeError, ValueError) as exc:
        return _text(_REFUSAL.format(reason=str(exc)))
    return _text(prefix + json.dumps(result, indent=2, sort_keys=True))


@tool(
    "list_memory_feedback",
    "List auditable Memory v2 retrieval feedback. Relevance failures and factual "
    "correction proposals are separate. state may be active or revoked; kind may be "
    "relevance or factual_correction. Read-only.",
    {"state": str, "kind": str},
    annotations=_LOCAL_READ_ONLY,
)
async def list_memory_feedback(args):
    state = str(args.get("state") or "").strip().lower() or None
    kind = str(args.get("kind") or "").strip().lower() or None

    def _list() -> list[dict]:
        from memory.v2 import MemoryV2Store

        return MemoryV2Store().retrieval_feedback(state=state, kind=kind, limit=100)

    try:
        feedback = await asyncio.to_thread(_list)
    except ValueError as exc:
        return _text(f"NOT READ. Reason: {exc}.")
    return _text(json.dumps(feedback, indent=2, sort_keys=True)[:12_000])


@tool(
    "revoke_memory_feedback",
    "Revoke one relevance-feedback example when the current or immediately previous "
    "genuine user turn explicitly asks to undo that feedback. The audit row remains. "
    "Factual corrections are instead accepted or rejected through proposal review.",
    {"feedback_id": str},
    annotations=_BROKERED_WRITE,
)
async def revoke_memory_feedback(args):
    origin = current_turn()
    previous = previous_user_turn_text()
    utterances = tuple(
        value for value in (str(origin.get("text") or ""), previous) if value
    )
    grounded_text = " ".join(utterances)
    from memory.feedback import classify_feedback

    intent = next(
        (
            candidate
            for text in utterances
            if (candidate := classify_feedback(text)) is not None
        ),
        None,
    )
    if intent is None or intent.kind != "revoke":
        return _text(
            _REFUSAL.format(
                reason="the current conversation did not explicitly ask to revoke relevance feedback"
            )
        )
    feedback_id = str(args.get("feedback_id") or "").strip()
    decision = authorize(
        "revoke_memory_feedback",
        origin=origin,
        destructive=False,
        detail=feedback_id,
        recent_texts=[previous] if previous else [],
    )
    if not decision.allowed:
        return _text(_REFUSAL.format(reason=decision.reason))

    def _revoke() -> dict:
        from memory.v2 import MemoryV2Store, source_receipt

        source = source_receipt(
            kind="spoken_turn",
            locator=str(origin.get("turn_id") or origin.get("call_id") or "spoken-turn"),
            source_text=grounded_text,
            session_id=str(origin.get("call_id") or ""),
            surface=str(origin.get("protocol") or "voice"),
        )
        return MemoryV2Store().revoke_relevance_feedback(feedback_id, source=source)

    try:
        result = await asyncio.to_thread(_revoke)
    except (KeyError, RuntimeError, ValueError) as exc:
        return _text(_REFUSAL.format(reason=str(exc)))
    return _text("RELEVANCE FEEDBACK REVOKED.\n" + json.dumps(result, indent=2, sort_keys=True))


@tool(
    "evaluate_memory_v2",
    "Run and persist a local Memory v2 retrieval-quality evaluation. cases_json is "
    "a JSON list of objects with name, query, and expected_record_ids. No network "
    "or third-party memory service is used.",
    {"cases_json": str, "limit": int},
    annotations=_LOCAL_READ_ONLY,
)
async def evaluate_memory_v2(args):
    try:
        cases = json.loads(str(args.get("cases_json") or "[]"))
        if not isinstance(cases, list):
            raise ValueError("cases_json must be a JSON list")
        limit = min(50, max(1, int(args.get("limit") or 8)))
        from memory.v2 import MemoryV2Store

        result = await asyncio.to_thread(MemoryV2Store().evaluate_retrieval, cases, limit=limit)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return _text(f"NOT EVALUATED. Reason: {exc}.")
    return _text(json.dumps(result, indent=2, sort_keys=True)[:12_000])


@tool(
    "export_memory_v2",
    "Create a crash-safe immutable legacy Markdown projection only when Raghav "
    "explicitly asks to export or project Memory v2. Existing Markdown is never "
    "overwritten or deleted; CURRENT changes atomically after a verified generation.",
    {"output_dir": str},
    annotations=_BROKERED_WRITE,
)
async def export_memory_v2(args):
    origin = current_turn()
    recent = recent_turn_texts()
    window = [str(origin.get("text") or ""), *recent]
    if not any(_EXPORT_SIGNAL.search(text) for text in window):
        return _text(
            _REFUSAL.format(reason="the recent conversation did not ask for a memory export")
        )
    decision = authorize(
        "export_memory_v2",
        origin=origin,
        destructive=False,
        detail="legacy Markdown projection",
        recent_texts=recent,
    )
    if not decision.allowed:
        return _text(_REFUSAL.format(reason=decision.reason))
    configured = str(args.get("output_dir") or "").strip()
    output = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".local" / "state" / "serena" / "memory-legacy-projection"
    )
    try:
        from memory.v2 import MemoryV2Store

        result = await asyncio.to_thread(
            MemoryV2Store().export_legacy_projection, output, actor="Raghav"
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _text(_REFUSAL.format(reason=str(exc)))
    return _text("EXPORTED.\n" + json.dumps(result, indent=2, sort_keys=True))


@tool(
    "migrate_memory_v2",
    "Import existing Markdown memories into Memory v2 without editing or deleting the "
    "Markdown source. Run only when Raghav explicitly asks to migrate or upgrade memory.",
    {},
    annotations=_BROKERED_WRITE,
)
async def migrate_memory_v2(_args):
    origin = current_turn()
    recent = recent_turn_texts()
    window = [str(origin.get("text") or ""), *recent]
    if not any(_MIGRATION_SIGNAL.search(text) for text in window):
        return _text(
            _REFUSAL.format(reason="the recent conversation did not ask to migrate memory")
        )
    decision = authorize(
        "migrate_memory_v2",
        origin=origin,
        destructive=False,
        detail="legacy Markdown import",
        recent_texts=recent,
    )
    if not decision.allowed:
        return _text(_REFUSAL.format(reason=decision.reason))

    def _migrate() -> dict:
        from memory.v2 import MemoryV2Store

        store = MemoryV2Store()
        result = store.migrate_current_legacy_store()
        result["projection"] = store.export_legacy_projection(
            Path.home() / ".local" / "state" / "serena" / "memory-legacy-projection",
            actor="Raghav",
        )
        result["activation"] = store.activate_authority(actor="Raghav")
        try:
            result["control_events_flushed"] = store.flush_control_outbox()
        except Exception:
            result["control_event_pending"] = True
        return result

    try:
        result = await asyncio.to_thread(_migrate)
    except (OSError, RuntimeError, ValueError) as exc:
        return _text(_REFUSAL.format(reason=f"migration did not activate v2: {exc}"))
    return _text(f"MIGRATED. {result['imported']} imported, {result['existing']} already present.")


MEMORY_TOOLS = (
    save_memory,
    edit_memory,
    delete_memory,
    save_knowledge,
    delete_knowledge_topic,
    propose_memory_change,
    list_memory_proposals,
    review_memory_proposal,
    search_memory_v2,
    record_memory_feedback,
    list_memory_feedback,
    revoke_memory_feedback,
    evaluate_memory_v2,
    export_memory_v2,
    migrate_memory_v2,
)
MEMORY_TOOL_NAMES = [f"mcp__serena-memory__{item.name}" for item in MEMORY_TOOLS]


def memory_tools_server():
    return create_sdk_mcp_server(name="serena-memory", tools=list(MEMORY_TOOLS))
