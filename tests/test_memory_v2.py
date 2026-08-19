from __future__ import annotations

import json
import sqlite3

import pytest

from core.control_plane import ControlPlaneStore
from memory.v2 import MemoryV2Store, source_receipt


def _source(text: str = "source words") -> dict:
    return source_receipt(
        kind="chat_turn",
        locator="chat:session-1:turn-2",
        source_text=text,
        session_id="session-1",
        surface="chat",
    )


def _add(
    store: MemoryV2Store,
    content: str,
    *,
    sensitivity: str = "personal",
    retention_until: float | None = None,
) -> str:
    proposal = store.propose_candidate(
        content=content,
        record_type="preference",
        source=_source(content),
        sensitivity=sensitivity,
        retention_until=retention_until,
    )
    approved = store.approve_proposal(
        proposal["proposal_id"], reviewer="Raghav", reason="approved in test"
    )
    return str(approved["applied_record_id"])


def test_legacy_migration_is_idempotent_and_preserves_provenance(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    legacy = [
        {
            "id": 42,
            "type": "project",
            "content": "Serena uses native provider sessions.",
            "filename": "042-serena-native.md",
            "created_at": "2026-08-01 10:00:00",
            "updated_at": "2026-08-02 11:00:00",
            "source_session_id": "session-42",
            "source_agent": "codex",
        }
    ]

    assert store.migrate_legacy(legacy) == {"imported": 1, "existing": 0}
    assert store.migrate_legacy(legacy) == {"imported": 0, "existing": 1}
    record = store.get_record("legacy:project:42")

    assert record is not None
    assert record.record_type == "semantic_fact"
    assert record.legacy_id == 42
    assert record.source["session_id"] == "session-42"
    assert len(record.source["content_sha256"]) == 64


def test_candidate_does_not_rewrite_memory_before_review(tmp_path, monkeypatch) -> None:
    hooks: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "core.plugin_loader.emit_plugin_hook",
        lambda event, payload, **_kwargs: hooks.append((event, payload)) or [],
    )
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    proposal = store.propose_candidate(
        content="Raghav prefers compact status updates.",
        record_type="preference",
        source=_source(),
    )

    assert proposal["state"] == "proposed"
    assert proposal["diff"]["before"] is None
    assert store.records() == []
    assert hooks == [
        (
            "memory.proposal.created",
            {
                "proposal_id": proposal["proposal_id"],
                "operation": "add",
                "target_record_id": "",
                "state": "proposed",
            },
        )
    ]

    rejected = store.reject_proposal(
        proposal["proposal_id"], reviewer="Raghav", reason="not accurate"
    )
    assert rejected["state"] == "rejected"
    assert store.records() == []


def test_supersession_is_versioned_and_reversible(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    old_id = _add(store, "Raghav prefers the blue car.")
    proposal = store.create_proposal(
        operation="supersede",
        target_record_id=old_id,
        candidate={
            "record_type": "preference",
            "content": "Raghav now prefers the red car.",
            "confidence": 0.95,
            "sensitivity": "personal",
        },
        source=_source("Raghav now prefers the red car."),
    )
    approved = store.approve_proposal(proposal["proposal_id"], reviewer="Raghav")

    old = store.get_record(old_id)
    new = store.get_record(str(approved["applied_record_id"]))
    assert old is not None and old.status == "superseded"
    assert new is not None and new.status == "current"
    assert store.relations(new.record_id)[0]["kind"] == "supersedes"

    rolled_back = store.rollback_proposal(
        proposal["proposal_id"], reviewer="Raghav", reason="undo the correction"
    )
    assert rolled_back["state"] == "rolled_back"
    assert store.get_record(old_id).status == "current"  # type: ignore[union-attr]
    assert store.get_record(old_id).valid_until is None  # type: ignore[union-attr]
    assert store.get_record(new.record_id).status == "retracted"  # type: ignore[union-attr]
    assert store.retrieve("blue car")[0].record.record_id == old_id


def test_approval_rejects_a_target_deleted_after_the_review_diff(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    target_id = _add(store, "Raghav prefers the blue car.")
    proposal = store.create_proposal(
        operation="supersede",
        target_record_id=target_id,
        candidate={
            "record_type": "preference",
            "content": "Raghav now prefers the red car.",
            "confidence": 0.9,
            "sensitivity": "personal",
        },
        source=_source("Raghav now prefers the red car."),
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute("DELETE FROM memory_records WHERE record_id = ?", (target_id,))

    with pytest.raises(KeyError, match="target disappeared"):
        store.approve_proposal(proposal["proposal_id"], reviewer="Raghav")

    assert store.proposals(state="proposed")[0]["proposal_id"] == proposal["proposal_id"]
    assert store.records() == []


def test_approval_rejects_a_target_changed_after_the_review_diff(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    target_id = _add(store, "Raghav prefers the blue car.")
    proposal = store.create_proposal(
        operation="forget",
        target_record_id=target_id,
        source=_source("forget the blue car preference"),
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE memory_records SET content = ?, updated_at = updated_at + 1 "
            "WHERE record_id = ?",
            ("changed outside the proposal", target_id),
        )

    with pytest.raises(RuntimeError, match="target changed"):
        store.approve_proposal(proposal["proposal_id"], reviewer="Raghav")

    assert store.get_record(target_id).status == "current"  # type: ignore[union-attr]
    assert store.proposals(state="proposed")[0]["proposal_id"] == proposal["proposal_id"]


def test_unresolved_contradictions_are_not_retrieved_as_truth(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    old_id = _add(store, "Raghav prefers morning appointments.")
    proposal = store.create_proposal(
        operation="contradict",
        target_record_id=old_id,
        candidate={
            "record_type": "preference",
            "content": "Raghav dislikes morning appointments.",
            "confidence": 0.8,
            "sensitivity": "personal",
        },
        source=_source("Raghav dislikes morning appointments."),
    )
    approved = store.approve_proposal(proposal["proposal_id"], reviewer="Raghav")

    assert store.get_record(old_id).status == "contested"  # type: ignore[union-attr]
    assert store.get_record(str(approved["applied_record_id"])).status == "contested"  # type: ignore[union-attr]
    assert store.retrieve("morning appointment") == []


def test_local_semantic_retrieval_handles_paraphrases_and_explains_scores(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    record_id = _add(store, "Raghav prefers compact automobiles.")

    hits = store.retrieve("which small car does Raghav like", limit=3)

    assert hits
    assert hits[0].record.record_id == record_id
    assert hits[0].semantic_score > 0
    assert any(reason.startswith("local_semantic:") for reason in hits[0].reasons)


def test_non_public_records_are_filtered_from_non_private_surfaces(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    sensitive_id = _add(store, "Therapy appointments are confidential.", sensitivity="sensitive")
    personal_id = _add(store, "Quiet reading happens at home.", sensitivity="personal")
    public_id = _add(store, "Serena is a local assistant.", sensitivity="public")

    assert store.retrieve("therapy appointments", surface="public") == []
    assert store.retrieve("quiet reading", surface="public") == []
    assert store.retrieve("therapy", surface="private")[0].record.record_id == sensitive_id
    assert store.retrieve("quiet", surface="private")[0].record.record_id == personal_id
    assert store.retrieve("local assistant", surface="public")[0].record.record_id == public_id


def test_phone_sync_creates_review_proposals_when_v2_is_authoritative(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "memory.sqlite3"
    monkeypatch.setenv("SERENA_MEMORY_V2_DB_PATH", str(database))
    store = MemoryV2Store(database)
    store.migrate_legacy([{"id": 1, "type": "user", "content": "old preference"}])
    store.activate_authority(actor="Raghav")

    from memory.locket_mirror import _stage_v2_pull

    result = _stage_v2_pull(
        {7: {"id": 7, "type": "user", "source": "app", "content": "new preference"}},
        {7: {"id": 1, "type": "user", "content": "old preference"}},
        dry_run=False,
    )

    assert result == {"created": 0, "updated": 1, "deleted": 0, "proposed": 1}
    proposal = store.proposals()[0]
    assert proposal["operation"] == "update"
    assert proposal["target_record_id"] == "legacy:user:1"
    assert store.get_record("legacy:user:1").content == "old preference"  # type: ignore[union-attr]


def test_expired_retention_creates_a_proposal_without_forgetting(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    record_id = _add(store, "Temporary vehicle comparison.", retention_until=10.0)

    proposals = store.retention_proposals(now=11.0)

    assert proposals[0]["operation"] == "forget"
    assert proposals[0]["target_record_id"] == record_id
    assert store.get_record(record_id).status == "current"  # type: ignore[union-attr]
    store.approve_proposal(proposals[0]["proposal_id"], reviewer="Raghav")
    assert store.get_record(record_id).status == "forgotten"  # type: ignore[union-attr]


def test_retention_extension_is_reviewed_and_reversible(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    record_id = _add(store, "Keep this comparison briefly.", retention_until=10.0)
    proposal = store.create_proposal(
        operation="retain",
        target_record_id=record_id,
        candidate={"retention_until": 50.0},
        source=_source("keep that until later"),
    )

    assert store.get_record(record_id).retention_until == 10.0  # type: ignore[union-attr]
    store.approve_proposal(proposal["proposal_id"], reviewer="Raghav")
    assert store.get_record(record_id).retention_until == 50.0  # type: ignore[union-attr]

    store.rollback_proposal(
        proposal["proposal_id"], reviewer="Raghav", reason="restore the earlier limit"
    )
    assert store.get_record(record_id).retention_until == 10.0  # type: ignore[union-attr]


def test_retrieval_evaluation_reports_recall_and_rank(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    record_id = _add(store, "Raghav prefers the compact vehicle.")

    report = store.evaluate_retrieval(
        [{"name": "paraphrase", "query": "favorite small car", "expected_record_ids": [record_id]}],
        limit=3,
    )

    assert report["case_count"] == 1
    assert report["recall_at_k"] == 1.0
    assert report["mean_reciprocal_rank"] == 1.0
    assert store.retrieval_evaluations()[0]["evaluation_id"] == report["evaluation_id"]


def test_retrieval_receipt_is_durable_without_raw_query(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    record_id = _add(store, "Raghav prefers the compact vehicle.")

    result = store.retrieve_with_receipt("favorite small car", surface="private")

    assert result["hits"][0]["record"]["record_id"] == record_id
    assert result["receipt"]["returned"][0]["record_id"] == record_id
    assert len(result["receipt"]["query_sha256"]) == 64
    with sqlite3.connect(store.path) as connection:
        encoded = connection.execute(
            "SELECT query_sha256 || filters_json || returned_json "
            "FROM memory_retrieval_receipts WHERE receipt_id = ?",
            (result["receipt"]["receipt_id"],),
        ).fetchone()[0]
    assert "favorite small car" not in encoded


def test_memory_proposal_events_use_transactional_outbox(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    proposal = store.create_proposal(
        operation="add",
        candidate={"record_type": "preference", "content": "Raghav likes small cars."},
        source=_source("Raghav likes small cars."),
    )
    assert store.pending_control_events() == 1

    store.approve_proposal(proposal["proposal_id"], reviewer="Raghav")
    assert store.pending_control_events() == 2
    control = ControlPlaneStore(tmp_path / "control.sqlite3")
    assert store.flush_control_outbox(control) == 2
    assert store.pending_control_events() == 0
    events = control.events(surface="memory", job_id=proposal["proposal_id"])
    assert {event.event_type for event in events} == {"proposal.created", "proposal.approved"}
    obligations = control.obligations(state="fulfilled", surface="memory")
    assert [item.job_id for item in obligations] == [proposal["proposal_id"]]


def test_legacy_projection_publishes_only_complete_verified_generations(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    record_id = _add(store, "Raghav prefers compact exports.")
    root = tmp_path / "projection"

    exported = store.export_legacy_projection(root, actor="Raghav")
    current = store.current_legacy_projection(root)

    assert current is not None
    assert current["generation_id"] == exported["generation_id"]
    assert current["record_count"] == 1
    generation = root / "generations" / current["generation_id"]
    files = [item["path"] for item in current["files"]]
    assert "INDEX.md" in files
    projected = next(generation.glob("user/*.md"))
    body = projected.read_text(encoding="utf-8")
    assert record_id in body
    assert "authority: memory-v2-projection" in body


def test_failed_projection_cannot_move_current_pointer(tmp_path, monkeypatch) -> None:
    import memory.v2 as memory_v2

    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    _add(store, "Raghav prefers durable exports.")
    root = tmp_path / "projection"
    first = store.export_legacy_projection(root, actor="Raghav")
    real_write = memory_v2._write_synced

    def fail_manifest(path, content):
        if path.name == "MANIFEST.json":
            raise OSError("simulated crash")
        return real_write(path, content)

    monkeypatch.setattr(memory_v2, "_write_synced", fail_manifest)
    with pytest.raises(OSError, match="simulated crash"):
        store.export_legacy_projection(root, actor="Raghav")

    assert (root / "CURRENT").read_text(encoding="utf-8").strip() == first["generation_id"]
    manifest = json.loads(
        (root / "generations" / first["generation_id"] / "MANIFEST.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["generation_id"] == first["generation_id"]


def test_authority_activation_is_explicit_and_durable(tmp_path) -> None:
    path = tmp_path / "memory.sqlite3"
    store = MemoryV2Store(path)

    assert store.is_authoritative() is False
    activated = store.activate_authority(actor="Raghav")

    assert activated["state"] == "active"
    assert activated["already_active"] is False
    assert MemoryV2Store.authority_is_active(path) is True
    assert store.activate_authority(actor="Raghav")["already_active"] is True
    assert store.pending_control_events() == 1


def test_legacy_store_writes_become_visible_proposals_after_activation(
    tmp_path, monkeypatch
) -> None:
    from memory import store as legacy_store

    database = tmp_path / "memory.sqlite3"
    monkeypatch.setenv("SERENA_MEMORY_V2_DB_PATH", str(database))
    monkeypatch.setattr(legacy_store, "MEMORY_DIR", tmp_path / "legacy")
    monkeypatch.setattr(MemoryV2Store, "flush_control_outbox", lambda self, control=None: 0)

    legacy_id = legacy_store.add_memory(
        "Original legacy preference.", "user", _no_mirror=True
    )
    assert isinstance(legacy_id, int)
    legacy_path = legacy_store._find_path(legacy_id)
    assert legacy_path is not None
    original_markdown = legacy_path.read_text(encoding="utf-8")

    store = MemoryV2Store(database)
    store.migrate_legacy(legacy_store.list_memories())
    store.activate_authority(actor="Raghav")

    added = legacy_store.add_memory("A distinct new reference.", "reference")
    edited = legacy_store.update_memory(legacy_id, content="Edited preference.")
    snoozed = legacy_store.snooze_memory(legacy_id, days=2)
    forgotten = legacy_store.delete_memory(legacy_id)

    assert all(isinstance(value, str) for value in (added, edited, snoozed, forgotten))
    assert legacy_path.read_text(encoding="utf-8") == original_markdown
    proposals = store.proposals(state="proposed", limit=20)
    assert {item["proposal_id"] for item in proposals}.issuperset(
        {added, edited, snoozed, forgotten}
    )
    assert {item["operation"] for item in proposals} >= {"add", "update", "forget"}


def test_projection_refuses_a_directory_it_does_not_own(tmp_path) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    root = tmp_path / "existing"
    root.mkdir()
    original = root / "notes.md"
    original.write_text("do not replace\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="does not own"):
        store.export_legacy_projection(root, actor="Raghav")

    assert original.read_text(encoding="utf-8") == "do not replace\n"


def test_projection_reassigns_generated_id_when_late_legacy_import_reserves_it(
    tmp_path,
) -> None:
    store = MemoryV2Store(tmp_path / "memory.sqlite3")
    generated_id = _add(store, "A generated preference.")
    root = tmp_path / "projection"
    store.export_legacy_projection(root, actor="Raghav")
    assert (
        store.migrate_legacy(
            [{"id": 1, "type": "project", "content": "An imported project fact."}]
        )["imported"]
        == 1
    )

    store.export_legacy_projection(root, actor="Raghav")
    identities = store._projection_identities(store.records())

    assert identities["legacy:project:1"][0] == 1
    assert identities[generated_id][0] != 1
    assert len({value[0] for value in identities.values()}) == 2
