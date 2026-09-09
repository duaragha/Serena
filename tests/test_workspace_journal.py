from concurrent.futures import ThreadPoolExecutor

import pytest

from core.workspace_journal import WorkspaceJournal


def test_clear_checkpoint_requires_claim_and_preserves_exact_identity(tmp_path):
    journal = WorkspaceJournal(tmp_path / "clear.db")
    target = {"session_id": "11111111-2222-4333-8444-555555555555", "provider": "claude", "cwd": str(tmp_path)}
    payload = {"action": "clear_session", "payload": {"confirmed": True}}
    with pytest.raises(ValueError, match="unfinished"):
        journal.prepare_clear("source", "clear", target)
    journal.claim_command("source", "clear", payload)
    journal.prepare_clear("source", "clear", target)
    journal.prepare_clear("source", "clear", target)
    with pytest.raises(ValueError, match="different identity"):
        journal.prepare_clear("source", "clear", {**target, "session_id": "22222222-2222-4333-8444-555555555555"})
    reopened = WorkspaceJournal(journal.path)
    assert reopened.has_pending_clear("source")
    assert reopened.command_receipt("source", "clear", payload) == (True, None)
    assert reopened.clear_target(target["session_id"]) == {**target, "committed": False}
    receipt = reopened.complete_clear("source", "clear")
    assert receipt == {"ok": True, "result": target}
    assert journal.command_receipt("source", "clear", payload) == (True, receipt)
    assert journal.clear_target(target["session_id"])["committed"]
    assert not journal.has_pending_clear("source")
    with pytest.raises(ValueError, match="already finished"):
        journal.complete_clear("source", "clear")


def test_fork_checkpoint_survives_reopen_and_requires_exact_source_request(tmp_path):
    path = tmp_path / "fork.db"
    journal = WorkspaceJournal(path)
    target = {"session_id": "fork", "provider": "claude", "cwd": str(tmp_path)}
    record = {"method": "workspace/sessionForked", "params": {"threadId": "source", "requestId": "request", "fork": target}}
    journal.append("source", record)
    restored = WorkspaceJournal(path)
    assert restored.fork_checkpoint("source", "request") == target
    assert restored.fork_checkpoint("other", "request") is None
    assert restored.fork_checkpoint("source", "other") is None
    restored.append("source", record)
    with pytest.raises(ValueError, match="ambiguous"):
        restored.fork_checkpoint("source", "request")


def test_reopen_replay_and_session_isolation(tmp_path):
    path = tmp_path / "events.db"
    journal = WorkspaceJournal(path)
    events = [
        {"method": "item/agentMessage/delta", "params": {"threadId": "exact", "delta": text}}
        for text in ["first", "second", "third"]
    ]
    for event in events:
        journal.append("exact", event)
    journal.append("other", {"method": "event", "params": {"threadId": "other"}})
    restored = WorkspaceJournal(path)
    first = restored.read("exact", limit=2)
    assert [e["sequence"] for e in first["events"]] == [1, 2]
    assert first["has_more"]
    last = restored.read("exact", after=first["cursor"])
    assert last["events"] == [{"sequence": 3, "event": events[-1]}]
    assert not last["has_more"]
    assert restored.read("unknown")["events"] == []


def test_concurrent_publishers_produce_no_sequence_gaps(tmp_path):
    journal = WorkspaceJournal(tmp_path / "events.db")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda i: journal.append("s", {"method": "delta", "params": {"i": i}}), range(50)
            )
        )
    events = journal.read("s")["events"]
    assert [e["sequence"] for e in events] == list(range(1, 51))
    assert {e["event"]["params"]["i"] for e in events} == set(range(50))


def test_invalid_event_does_not_consume_sequence(tmp_path):
    journal = WorkspaceJournal(tmp_path / "events.db")
    with pytest.raises(ValueError, match="another session"):
        journal.append("s", {"method": "event", "params": {"threadId": "other"}})
    with pytest.raises(ValueError):
        journal.read("s", limit=1000000)
    assert journal.append("s", {"method": "event"})["sequence"] == 1
