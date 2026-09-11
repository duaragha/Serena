"""Lost job receipts require exact durable native acceptance and completion."""

import hashlib

import pytest

from core.workspace_journal import WorkspaceJournal


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
def test_completed_work_receipt_recovers_after_reopening_database(tmp_path, status):
    journal, key, receipt = prepare(tmp_path, status=status)
    reopened = WorkspaceJournal(journal.path)
    assert reopened.recover_completed_work("exact") == 1
    assert reopened.command_record("exact", key)["result"] == receipt
    assert not reopened.has_pending_work("exact")
    assert reopened.recover_completed_work("exact") == 0


def prepare(tmp_path, *, status="completed", defect=None):
    journal = WorkspaceJournal(tmp_path / "work.db")
    key = "work:item:dispatch"
    payload = {"action": "work_submit", "item_id": "item", "prompt_sha256": "digest", "start_offset": 12}
    receipt = {"ok": True, "committed": True, "session_id": "exact", "turn_id": "turn", "start_offset": 12}
    journal.claim_command("exact", key, payload)
    checkpoint = {"method": "workspace/workSubmitted", "params": {
        "threadId": "exact", "requestId": key, "payload": dict(payload), "receipt": dict(receipt)}}
    if defect == "prompt":
        checkpoint["params"]["payload"]["prompt_sha256"] = "different"
    if defect == "offset":
        checkpoint["params"]["receipt"]["start_offset"] = 13
    if defect == "request":
        checkpoint["params"]["requestId"] = "work:another:dispatch"
    if defect == "session":
        checkpoint["params"]["receipt"]["session_id"] = "other"
    if defect != "no_checkpoint":
        journal.append("exact", checkpoint)
    if defect == "duplicate":
        journal.append("exact", checkpoint)
    if defect != "no_completion":
        journal.append("exact", {"method": "turn/completed", "params": {
            "threadId": "exact", "turn": {
                "id": "other" if defect == "turn" else "turn", "status": status}}})
    return journal, key, receipt


@pytest.mark.parametrize("defect", ["prompt", "offset", "request", "session", "no_checkpoint",
                                    "duplicate", "no_completion", "turn"])
def test_ambiguous_or_missing_evidence_keeps_reservation_pending(tmp_path, defect):
    journal, key, _ = prepare(tmp_path, defect=defect)
    assert journal.recover_completed_work("exact") == 0
    assert journal.command_record("exact", key)["result"] is None
    assert journal.has_pending_work("exact")


def test_nonterminal_status_cannot_unlock_work(tmp_path):
    journal, _, _ = prepare(tmp_path, status="inProgress")
    assert journal.recover_completed_work("exact") == 0
    assert journal.has_pending_work("exact")


@pytest.mark.parametrize("defect", [None, "old", "different_prompt", "two_turns", "two_inputs",
                                    "wrong_turn", "missing_start", "no_completion", "attachment"])
def test_native_events_recover_lost_ack_only_with_unique_exact_input(tmp_path, defect):
    journal = WorkspaceJournal(tmp_path / "native.db")
    prompt = "exact accepted job"
    boundary = journal.latest_sequence("exact")
    if defect == "old":
        boundary = 50
    payload = {"action": "work_submit", "item_id": "item", "start_offset": 12,
               "event_start": boundary, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}
    key = "work:item:dispatch"
    journal.claim_command("exact", key, payload)
    if defect != "missing_start":
        journal.append("exact", {"method": "turn/started", "params": {
            "threadId": "exact", "turn": {"id": "turn", "status": "inProgress"}}})
    if defect == "two_turns":
        journal.append("exact", {"method": "turn/started", "params": {
            "threadId": "exact", "turn": {"id": "other", "status": "inProgress"}}})
    content = [{"type": "text", "text": "different" if defect == "different_prompt" else prompt}]
    if defect == "attachment":
        content.append({"type": "image", "url": "irrelevant"})
    for method in ("item/started", "item/completed"):
        journal.append("exact", {"method": method, "params": {
            "threadId": "exact", "turnId": "other" if defect == "wrong_turn" else "turn",
            "item": {"id": method if defect == "two_inputs" else "input", "type": "userMessage", "content": content}}})
    if defect != "no_completion":
        journal.append("exact", {"method": "turn/completed", "params": {
            "threadId": "exact", "turn": {"id": "turn", "status": "completed"}}})
    assert WorkspaceJournal(journal.path).recover_completed_work("exact") == (1 if defect is None else 0)
    assert journal.has_pending_work("exact") is (defect is not None)
    if defect is None:
        receipt = journal.command_record("exact", key)["result"]
        assert receipt["turn_id"] == "turn" and receipt["start_offset"] == 12
