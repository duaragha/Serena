import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from core.workspace_journal import WorkspaceJournal


def _delete_checkpoint(tmp_path, root, child=None):
    identities = [root, *([child] if child else [])]
    recovery = tmp_path / "delete-recovery"
    return {
        "session_id": root,
        "provider": "codex",
        "cwd": str(tmp_path),
        "recovery_dir": str(recovery),
        "targets": [{
            "session_id": identity,
            "provider": "codex",
            "cwd": str(tmp_path),
            "path": str(tmp_path / "codex" / "sessions" / f"{identity}.jsonl"),
            "archived": False,
            "recovery_path": str(recovery / "rollouts" / f"{identity}.jsonl"),
            "size": 1,
            "sha256": "0" * 64,
        } for identity in identities],
    }


def test_delete_checkpoint_blocks_family_and_completes_exactly_once(tmp_path):
    journal = WorkspaceJournal(tmp_path / "delete.db")
    root, child, request = str(uuid4()), str(uuid4()), str(uuid4())
    checkpoint = _delete_checkpoint(tmp_path, root, child)
    assert journal.claim_delete(root, request) == (True, None)
    assert journal.has_pending_delete(root) and not journal.has_pending_delete(child)
    journal.prepare_delete(root, request, checkpoint)
    journal.prepare_delete(root, request, checkpoint)
    reopened = WorkspaceJournal(journal.path)
    assert reopened.delete_checkpoint(root, request) == checkpoint
    assert reopened.pending_deletes([root, child, str(uuid4())]) == {
        root: {"session_id": root, "request_id": request},
        child: {"session_id": root, "request_id": request},
    }
    with pytest.raises(ValueError, match="unconfirmed"):
        reopened.claim_delete(child, str(uuid4()))
    with pytest.raises(ValueError, match="archiving is unavailable"):
        reopened.claim_archive(child, str(uuid4()))
    with pytest.raises(ValueError, match="restoration is unavailable"):
        reopened.claim_archive_restore(child, str(uuid4()))
    with pytest.raises(ValueError, match="different native family"):
        reopened.prepare_delete(root, request, _delete_checkpoint(tmp_path, root))
    receipt = {"ok": True, "result": {
        "session_id": root,
        "provider": "codex",
        "cwd": str(tmp_path),
        "deleted": True,
        "thread_ids": [root, child],
        "recovery_dir": checkpoint["recovery_dir"],
        "catalog_removed": True,
        "thread_count": 2,
    }}
    assert reopened.complete_delete(root, request, receipt) == receipt
    assert not journal.has_pending_delete(root) and not journal.has_pending_delete(child)
    assert journal.claim_delete(root, request) == (False, receipt)
    assert [row["event"]["method"] for row in journal.read(root)["events"]][-2:] == [
        "workspace/deletePrepared", "workspace/deleted",
    ]


def test_delete_and_archive_family_claims_conflict_before_mutation(tmp_path):
    root, child = str(uuid4()), str(uuid4())
    checkpoint = _delete_checkpoint(tmp_path, root, child)

    journal = WorkspaceJournal(tmp_path / "pending-command.db")
    journal.claim_command(child, str(uuid4()), {"action": "submit", "payload": {}})
    delete_request = str(uuid4())
    journal.claim_delete(root, delete_request)
    with pytest.raises(ValueError, match="another unconfirmed operation"):
        journal.prepare_delete(root, delete_request, checkpoint)

    archive = WorkspaceJournal(tmp_path / "pending-delete.db")
    delete_request, archive_request = str(uuid4()), str(uuid4())
    archive.claim_delete(child, delete_request)
    archive.claim_archive(root, archive_request)
    archive_target = {"session_id": root, "provider": "codex", "cwd": str(tmp_path)}
    with pytest.raises(ValueError, match="unconfirmed delete"):
        archive.prepare_archive(root, archive_request, {
            **archive_target,
            "targets": [archive_target, {**archive_target, "session_id": child}],
        })


def test_delete_completion_receipt_and_event_are_atomic(tmp_path):
    journal = WorkspaceJournal(tmp_path / "delete-atomic.db")
    root, request = str(uuid4()), str(uuid4())
    checkpoint = _delete_checkpoint(tmp_path, root)
    journal.claim_delete(root, request)
    journal.prepare_delete(root, request, checkpoint)
    receipt = {"ok": True, "result": {
        "session_id": root,
        "provider": "codex",
        "cwd": str(tmp_path),
        "deleted": True,
        "thread_ids": [root],
        "recovery_dir": checkpoint["recovery_dir"],
        "catalog_removed": True,
        "thread_count": 1,
    }}
    with sqlite3.connect(journal.path) as conn:
        conn.execute("""CREATE TRIGGER reject_delete_event BEFORE INSERT ON workspace_events
                     WHEN json_extract(NEW.event, '$.method')='workspace/deleted'
                     BEGIN SELECT RAISE(ABORT, 'simulated delete receipt failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="simulated delete receipt failure"):
        journal.complete_delete(root, request, receipt)
    assert journal.command_receipt(
        root, request, {"action": "delete_session", "payload": {"confirmed": True}}
    ) == (True, None)
    with sqlite3.connect(journal.path) as conn:
        conn.execute("DROP TRIGGER reject_delete_event")
    assert journal.complete_delete(root, request, receipt) == receipt


def test_archive_checkpoint_blocks_root_and_descendant_until_exact_receipt(tmp_path):
    journal = WorkspaceJournal(tmp_path / "archive.db")
    root, child, request = str(uuid4()), str(uuid4()), str(uuid4())
    checkpoint = {"session_id": root, "provider": "codex", "cwd": str(tmp_path), "targets": [
        {"session_id": root, "provider": "codex", "cwd": str(tmp_path)},
        {"session_id": child, "provider": "codex", "cwd": str(tmp_path / "child")},
    ]}
    assert journal.claim_archive(root, request) == (True, None)
    assert journal.has_pending_archive(root) and not journal.has_pending_archive(child)
    journal.prepare_archive(root, request, checkpoint)
    journal.prepare_archive(root, request, checkpoint)
    reopened = WorkspaceJournal(journal.path)
    assert reopened.archive_checkpoint(root, request) == checkpoint
    assert reopened.has_pending_archive(root) and reopened.has_pending_archive(child)
    with pytest.raises(ValueError, match="unconfirmed"):
        reopened.claim_archive(child, str(uuid4()))
    with pytest.raises(ValueError, match="restoration is unavailable"):
        reopened.claim_archive_restore(child, str(uuid4()))
    with pytest.raises(ValueError, match="different native family"):
        reopened.prepare_archive(root, request, {**checkpoint, "targets": checkpoint["targets"][:1]})
    receipt = {"ok": True, "result": {"session_id": root, "archived": True}}
    reopened.finish_command(root, request, receipt)
    assert not journal.has_pending_archive(root) and not journal.has_pending_archive(child)
    assert journal.claim_archive(root, request) == (False, receipt)


def test_archive_claim_rejects_pending_restore_and_requires_exact_checkpoint(tmp_path):
    journal = WorkspaceJournal(tmp_path / "archive-guard.db")
    root, child, request = str(uuid4()), str(uuid4()), str(uuid4())
    journal.claim_archive_restore(root, str(uuid4()))
    with pytest.raises(ValueError, match="restoration"):
        journal.claim_archive(root, request)
    other = str(uuid4())
    journal.claim_archive(other, request)
    with pytest.raises(ValueError, match="checkpoint"):
        journal.prepare_archive(other, request, {
            "session_id": other, "provider": "codex", "cwd": "relative", "targets": [],
        })
    second_root, second_request = str(uuid4()), str(uuid4())
    journal.claim_archive(second_root, second_request)
    journal.claim_archive_restore(child, str(uuid4()))
    with pytest.raises(ValueError, match="unconfirmed restoration"):
        journal.prepare_archive(second_root, second_request, {
            "session_id": second_root, "provider": "codex", "cwd": str(tmp_path), "targets": [
                {"session_id": second_root, "provider": "codex", "cwd": str(tmp_path)},
                {"session_id": child, "provider": "codex", "cwd": str(tmp_path)},
            ],
        })


def test_archive_completion_commits_receipt_and_event_atomically(tmp_path):
    journal = WorkspaceJournal(tmp_path / "archive-completion.db")
    root, request = str(uuid4()), str(uuid4())
    target = {"session_id": root, "provider": "codex", "cwd": str(tmp_path)}
    journal.claim_archive(root, request)
    journal.prepare_archive(root, request, {**target, "targets": [target]})
    receipt = {"ok": True, "result": {
        **target, "archived": True, "thread_ids": [root], "cataloged": True, "thread_count": 1,
    }}
    with sqlite3.connect(journal.path) as conn:
        conn.execute("""CREATE TRIGGER reject_archive_event BEFORE INSERT ON workspace_events
                     WHEN json_extract(NEW.event, '$.method')='workspace/archived'
                     BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="simulated disk failure"):
        journal.complete_archive(root, request, receipt)
    assert journal.command_receipt(
        root, request, {"action": "archive_session", "payload": {"confirmed": True}}
    ) == (True, None)
    with sqlite3.connect(journal.path) as conn:
        conn.execute("DROP TRIGGER reject_archive_event")
    assert journal.complete_archive(root, request, receipt) == receipt
    assert [row["event"]["method"] for row in journal.read(root)["events"]][-2:] == [
        "workspace/archivePrepared", "workspace/archived",
    ]


def test_saved_mode_is_exact_session_and_survives_unrelated_events(tmp_path):
    journal = WorkspaceJournal(tmp_path / 'modes.db')
    assert journal.saved_codex_mode('exact') is None
    journal.append('other', {'method': 'workspace/settings', 'params': {'collaborationMode': 'default'}})
    journal.append('exact', {'method': 'workspace/settings', 'params': {'collaborationMode': 'plan'}})
    journal.append('exact', {'method': 'workspace/history', 'params': {}})
    journal.append('exact', {'method': 'workspace/settings', 'params': {'model': 'chosen'}})
    assert journal.saved_codex_mode('exact') == 'plan'
    journal.append('exact', {'method': 'workspace/settings', 'params': {'collaborationMode': 'default'}})
    assert journal.saved_codex_mode('exact') == 'default'
    journal.append('exact', {'method': 'workspace/settings', 'params': {'collaborationMode': 'invalid'}})
    with pytest.raises(ValueError, match='Saved Codex mode'):
        journal.saved_codex_mode('exact')


def test_existing_clear_journal_migrates_without_losing_target(tmp_path):
    path = tmp_path / "legacy.db"
    target = {"session_id": "11111111-2222-4333-8444-555555555555", "provider": "claude", "cwd": str(tmp_path)}
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE workspace_clears (source_id TEXT, request_id TEXT, target_id TEXT UNIQUE, target TEXT, committed INTEGER, PRIMARY KEY(source_id,request_id))")
    conn.execute("INSERT INTO workspace_clears VALUES (?, ?, ?, ?, 1)", ("source", "clear", target["session_id"], json.dumps(target)))
    conn.commit()
    conn.close()
    journal = WorkspaceJournal(path)
    assert journal.uncataloged_clears() == [{**target, "created_at": ""}]
    assert journal.clear_target(target["session_id"]) == {**target, "committed": True}
    journal.mark_clear_cataloged(target["session_id"])
    assert WorkspaceJournal(path).uncataloged_clears() == []


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


@pytest.mark.parametrize("confirmed", [True, False])
def test_named_clear_checkpoint_matches_exact_command_and_keeps_title_outcome(tmp_path, confirmed):
    journal = WorkspaceJournal(tmp_path / "named-clear.db")
    target = {
        "session_id": "11111111-2222-4333-8444-555555555555",
        "provider": "codex",
        "cwd": str(tmp_path),
        "requestedName": "Next work",
        "nameConfirmed": confirmed,
        **({} if confirmed else {"nameError": "Native rename unavailable"}),
    }
    payload = {"action": "clear_session", "payload": {"confirmed": True, "name": "Next work"}}
    journal.claim_command("source", "clear", payload)
    journal.prepare_clear("source", "clear", target)
    assert journal.complete_clear("source", "clear") == {"ok": True, "result": target}
    assert journal.clear_target(target["session_id"])["requestedName"] == "Next work"


def test_named_clear_checkpoint_rejects_unconfirmed_or_mismatched_title_metadata(tmp_path):
    journal = WorkspaceJournal(tmp_path / "bad-named-clear.db")
    base = {"session_id": "11111111-2222-4333-8444-555555555555", "provider": "codex", "cwd": str(tmp_path)}
    journal.claim_command("source", "clear", {
        "action": "clear_session", "payload": {"confirmed": True, "name": "Expected"},
    })
    for target in [
        {**base, "requestedName": "Different", "nameConfirmed": True},
        {**base, "requestedName": "Expected", "nameConfirmed": False},
        {**base, "requestedName": "Expected", "nameConfirmed": True, "nameError": "contradiction"},
        {**base, "requestedName": "Expected", "nameConfirmed": "yes"},
    ]:
        with pytest.raises(ValueError):
            journal.prepare_clear("source", "clear", target)


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


@pytest.mark.parametrize('setting', ['personality', 'speed'])
def test_saved_setting_reset_preserves_history_and_newer_preferences(tmp_path, setting):
    journal = WorkspaceJournal(tmp_path / 'reset.db')
    value = 'friendly' if setting == 'personality' else {'model': 'old', 'value': 'priority'}
    event = ({'method': 'workspace/settings', 'params': {'personality': value}} if setting == 'personality'
             else {'method': 'workspace/speed', 'params': value})
    reader = journal.saved_codex_personality if setting == 'personality' else journal.saved_codex_speed
    journal.append('exact', event)
    revision = journal.saved_codex_setting_revision('exact', setting)
    with pytest.raises(ValueError, match='changed'):
        journal.reset_saved_codex_setting('exact', setting, 'stale', 'failure', revision)
    assert reader('exact') == value
    journal.append('exact', event)
    with pytest.raises(ValueError, match='changed'):
        journal.reset_saved_codex_setting('exact', setting, value, 'failure', revision)
    revision = journal.saved_codex_setting_revision('exact', setting)
    journal.reset_saved_codex_setting('exact', setting, value, 'failure', revision)
    assert reader('exact') is None
    assert journal.read('exact')['events'][0]['event'] == event
    journal.append('exact', event)
    assert reader('exact') == value
