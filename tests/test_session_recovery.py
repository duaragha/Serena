import json
import subprocess
import sys
from pathlib import Path

import pytest

from core import indexer
from core.parser import parse_full


@pytest.fixture(autouse=True)
def isolated_index_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(indexer, "_INDEX_LOCK_PATH", tmp_path / "index.lock")
    monkeypatch.setenv("SERENA_RUNTIME_LEASE_DIR", str(tmp_path / "leases"))


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
    rollout.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

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

    def rollback(self):
        pass


def test_delete_session_retains_recovery_copy(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SERENA_RUNTIME_LEASE_DIR", str(tmp_path / "leases"))
    sid = "019f5bbd-2597-7800-8840-e5f2aa7619b8"
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
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
    manifest = json.loads((recovery_dir / "recovery.json").read_text(encoding="utf-8"))
    assert manifest["original_path"] == str(rollout)
    assert manifest["deleted_via"] == "test-ui"
    assert manifest["metadata"]["custom_title"] == "Mobile"
    assert deleted_meta == [sid]


def test_owned_session_delete_cannot_modify_index_transcript_or_metadata(tmp_path, monkeypatch):
    from core.workspace_lease import SessionLease, SessionOwnedError

    monkeypatch.setenv("SERENA_RUNTIME_LEASE_DIR", str(tmp_path / "leases"))
    path = tmp_path / "chat.jsonl"
    path.write_text("original\n", encoding="utf-8")
    monkeypatch.setattr(indexer, "get_session", lambda sid: {"session_id": "exact", "file_path": str(path)})
    monkeypatch.setattr(indexer, "_get_db", lambda: pytest.fail("Deletion reached database while owned"))
    owner = SessionLease("exact")
    try:
        with pytest.raises(SessionOwnedError):
            indexer.delete_session("exact")
        assert path.read_text(encoding="utf-8") == "original\n"
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


def test_stopped_exact_child_can_be_trashed_while_sibling_keeps_running(tmp_path, monkeypatch):
    from core import metadata
    from core.workspace_lease import SessionLease, SessionOwnedError

    monkeypatch.setattr(metadata, "METADATA_DIR", tmp_path / "meta")
    monkeypatch.setattr(metadata, "_ensure_migrated", lambda: None)
    files = {sid: tmp_path / f"{sid}.jsonl" for sid in ("selected", "sibling", "third")}
    for sid, file in files.items():
        file.write_text(f"{sid} history\n")
    group = metadata.link_sessions(list(files))
    metadata.set_custom_title("selected", "Unified Changes")
    metadata.unlink_session("selected")
    monkeypatch.setattr(indexer, "get_session", lambda sid: {"session_id": sid, "file_path": str(files[sid])})
    monkeypatch.setattr(indexer, "_get_db", lambda: _FakeConnection())
    children, leases = [], []
    try:
        for sid in ("selected", "sibling"):
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            children.append(child)
            lease = SessionLease(sid)
            leases.append(lease)
            lease.bind(child.pid)
            lease.release()
        with pytest.raises(SessionOwnedError):
            indexer.delete_session("selected")
        assert files["selected"].exists()
        children[0].terminate()
        children[0].wait(timeout=10)
        indexer.delete_session("selected", source="test-stop-trash")
        recovered = indexer.DATA_DIR / "deleted-sessions" / "selected"
        assert (recovered / "selected.jsonl").read_text() == "selected history\n"
        assert json.loads((recovered / "recovery.json").read_text())["metadata"]["group_unlinked"]
        assert not files["selected"].exists()
        assert files["sibling"].read_text() == "sibling history\n"
        assert metadata.get_group("sibling") == metadata.get_group("third") == group
        assert children[1].poll() is None
        with pytest.raises(SessionOwnedError):
            indexer.delete_session("sibling")
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)
        for lease in leases:
            lease.release()


@pytest.mark.parametrize("failure", ["manifest", "move", "database"])
def test_failed_delete_keeps_original_transcript_and_metadata(tmp_path, monkeypatch, failure):
    sid = "failure-proof"
    path = tmp_path / "chat.jsonl"
    path.write_text("original history\n", encoding="utf-8")
    monkeypatch.setattr(indexer, "get_session", lambda value: {"session_id": sid, "file_path": str(path)})
    monkeypatch.setattr(indexer.meta_sync, "get_meta", lambda value: {"custom_title": "Keep me"})
    monkeypatch.setattr(indexer.meta_sync, "delete_meta", lambda value: pytest.fail("Metadata must not be removed"))
    calls = []

    class FailedConnection(_FakeConnection):
        def execute(self, *args):
            calls.append("execute")
            raise OSError("database failure")

        def rollback(self):
            calls.append("rollback")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(indexer, "_get_db", lambda: FailedConnection())
    if failure == "manifest":
        original = Path.write_text

        def write(target, *args, **kwargs):
            if target.name == "recovery.json":
                raise OSError("manifest failure")
            return original(target, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", write)
    if failure == "move":
        def move(*args):
            raise OSError("move failure")
        monkeypatch.setattr(indexer.shutil, "move", move)
    with pytest.raises(OSError, match=failure):
        indexer.delete_session(sid)
    assert path.read_text(encoding="utf-8") == "original history\n"
    assert calls == (["execute", "rollback", "close"] if failure == "database" else [])
