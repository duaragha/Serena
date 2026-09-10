import json
from pathlib import Path

import pytest

from core import indexer
from core.parser import parse_full


def test_parse_full_reads_codex_event_messages(tmp_path: Path):
    session_dir = tmp_path / ".codex" / "sessions" / "2026" / "07" / "13"
    session_dir.mkdir(parents=True)
    rollout = session_dir / "rollout-test.jsonl"
    records = [
        {
            "timestamp": "2026-07-13T13:51:51Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "build mobile"},
        },
        {
            "timestamp": "2026-07-13T13:52:00Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "starting now"},
        },
    ]
    rollout.write_text("".join(json.dumps(record) + "\n" for record in records))

    messages = parse_full(rollout)

    assert [(message.role, message.text) for message in messages] == [
        ("user", "build mobile"),
        ("assistant", "starting now"),
    ]


class _FakeConnection:
    def execute(self, *_args, **_kwargs):
        return self

    def commit(self):
        pass

    def close(self):
        pass


def test_delete_session_retains_recovery_copy(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SERENA_RUNTIME_LEASE_DIR", str(tmp_path / "leases"))
    sid = "019f5bbd-2597-7800-8840-e5f2aa7619b8"
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text('{"type":"session_meta"}\n')
    deleted_meta = []
    monkeypatch.setattr(indexer, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(
        indexer,
        "get_session",
        lambda _prefix: {"session_id": sid, "file_path": str(rollout)},
    )
    monkeypatch.setattr(indexer, "_get_db", lambda: _FakeConnection())
    monkeypatch.setattr(indexer.meta_sync, "get_meta", lambda _sid: {"custom_title": "Mobile"})
    monkeypatch.setattr(indexer.meta_sync, "delete_meta", deleted_meta.append)

    original = indexer.delete_session(sid[:8], source="test-ui")

    recovery_dir = tmp_path / "data" / "deleted-sessions" / sid
    assert original == str(rollout)
    assert not rollout.exists()
    assert (recovery_dir / "rollout.jsonl").exists()
    manifest = json.loads((recovery_dir / "recovery.json").read_text())
    assert manifest["original_path"] == str(rollout)
    assert manifest["deleted_via"] == "test-ui"
    assert manifest["metadata"]["custom_title"] == "Mobile"
    assert deleted_meta == [sid]


def test_owned_session_delete_cannot_modify_index_transcript_or_metadata(tmp_path, monkeypatch):
    from core.workspace_lease import SessionLease, SessionOwnedError

    monkeypatch.setenv("SERENA_RUNTIME_LEASE_DIR", str(tmp_path / "leases"))
    path = tmp_path / "chat.jsonl"
    path.write_text("original\n")
    monkeypatch.setattr(indexer, "get_session", lambda sid: {"session_id": "exact", "file_path": str(path)})
    monkeypatch.setattr(indexer, "_get_db", lambda: pytest.fail("Deletion reached database while owned"))
    owner = SessionLease("exact")
    try:
        with pytest.raises(SessionOwnedError):
            indexer.delete_session("exact")
        assert path.read_text() == "original\n"
    finally:
        owner.release()


def test_delete_retains_lock_through_archive_and_releases_on_failure(tmp_path, monkeypatch):
    from core.workspace_lease import SessionLease, SessionOwnedError

    monkeypatch.setenv("SERENA_RUNTIME_LEASE_DIR", str(tmp_path / "leases"))
    monkeypatch.setattr(indexer, "get_session", lambda sid: {"session_id": "exact"})

    def deletion(session, *, source):
        with pytest.raises(SessionOwnedError):
            SessionLease("exact")
        raise OSError("archive failure")

    monkeypatch.setattr(indexer, "_delete_unowned_session", deletion)
    with pytest.raises(OSError, match="archive failure"):
        indexer.delete_session("exact")
    lease = SessionLease("exact")
    lease.release()
