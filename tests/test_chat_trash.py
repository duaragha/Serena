"""The trash bin: a deleted chat lists by name and comes back where it lived."""

import json
import os
import subprocess
import sys
import time

import psutil
import pytest

from core import chat_trash, indexer
from core import metadata as meta
from core.workspace_lease import SessionLease, SessionOwnedError, terminate_recorded_runtime

SID = "6f1d2c3b-4a59-4e6f-8a7b-9c0d1e2f3a4b"


@pytest.fixture
def index_env(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(indexer, "DB_PATH", tmp_path / "data" / "index.db")
    monkeypatch.setattr(indexer, "_INDEX_LOCK_PATH", tmp_path / "index.lock")
    monkeypatch.setattr(meta, "METADATA_DIR", tmp_path / "meta")
    monkeypatch.setattr(meta, "METADATA_PATH", tmp_path / "metadata.json")
    monkeypatch.setenv("SERENA_RUNTIME_LEASE_DIR", str(tmp_path / "leases"))
    (tmp_path / "data").mkdir()
    return tmp_path


def _transcript(root, sid=SID, text="fix the login redirect loop"):
    path = root / "projects" / "-home-raghav-app" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "user",
        "sessionId": sid,
        "cwd": "/home/raghav/app",
        "timestamp": "2026-09-25T12:00:00Z",
        "message": {"role": "user", "content": text},
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def test_deleted_chat_lists_by_name_and_restores_with_its_metadata(index_env):
    path = _transcript(index_env)
    assert indexer.index_session_file("claude", path) == SID
    meta.set_custom_title(SID, "Login loop")
    meta.set_starred(SID, True)

    indexer.delete_session(SID, source="test")
    assert indexer.get_session(SID) is None
    assert not path.exists()

    listing = chat_trash.list_trash()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert (item["session_id"], item["title"], item["agent"], item["restorable"]) == (
        SID, "Login loop", "claude", True)

    result = chat_trash.restore(item["id"])
    assert result == {"session_id": SID, "path": str(path), "indexed": True}
    assert path.exists()
    row = indexer.get_session(SID)
    assert row["display_title"] == "Login loop"
    assert row["starred"]
    assert meta.get_meta(SID)["starred"] is True
    assert chat_trash.list_trash() == {"items": [], "total": 0}


def test_undo_restores_the_newest_delete_of_a_chat(index_env):
    path = _transcript(index_env)
    indexer.index_session_file("claude", path)
    indexer.delete_session(SID, source="test")
    first = chat_trash.list_trash()["items"][0]["id"]
    chat_trash.restore(first)
    path.write_text(path.read_text(encoding="utf-8") * 2, encoding="utf-8")
    indexer.index_session_file("claude", path)
    indexer.delete_session(SID, source="test")
    # Its first trip to the trash is gone, so only the newest delete can match.
    assert chat_trash.list_trash()["total"] == 1

    chat_trash.restore_latest(SID)
    assert path.read_text(encoding="utf-8").count("\n") == 2
    with pytest.raises(chat_trash.TrashError):
        chat_trash.restore_latest(SID)


def test_entries_trashed_before_summaries_are_named_from_the_transcript(index_env):
    entry = indexer.DATA_DIR / "deleted-sessions" / SID
    entry.mkdir(parents=True)
    original = index_env / "projects" / "-home-raghav-app" / f"{SID}.jsonl"
    _transcript(index_env)
    original.rename(entry / original.name)
    (entry / "recovery.json").write_text(json.dumps({
        "session_id": SID, "original_path": str(original),
        "deleted_at": "2026-09-01T10:00:00-04:00", "metadata": {},
    }), encoding="utf-8")

    item = chat_trash.list_trash()["items"][0]
    assert item["agent"] == "claude"
    assert item["title"] and item["title"] != "Untitled chat"


def test_restore_never_overwrites_a_chat_at_the_original_path(index_env):
    path = _transcript(index_env)
    indexer.index_session_file("claude", path)
    indexer.delete_session(SID, source="test")
    path.write_text("a newer chat\n", encoding="utf-8")
    item = chat_trash.list_trash()["items"][0]
    assert item["restorable"] is False
    with pytest.raises(chat_trash.TrashError):
        chat_trash.restore(item["id"])
    assert path.read_text(encoding="utf-8") == "a newer chat\n"


@pytest.mark.parametrize("entry_id", ["", "..", "../data", "a/b", "missing"])
def test_restore_only_accepts_entries_inside_the_trash(index_env, entry_id):
    (indexer.DATA_DIR / "deleted-sessions").mkdir(parents=True)
    with pytest.raises(chat_trash.TrashError):
        chat_trash.restore(entry_id)


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX")
def test_forced_stop_ends_a_runtime_another_owner_still_holds(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENA_RUNTIME_LEASE_DIR", str(tmp_path / "leases"))
    agent = subprocess.Popen(
        [sys.executable, "-c", "import subprocess,sys,time; "
         "subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)"],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not psutil.Process(agent.pid).children() and time.monotonic() < deadline:
            time.sleep(0.05)
        tool = psutil.Process(agent.pid).children()[0]
        owner = SessionLease(SID)
        owner.launching()
        owner.bind(agent.pid)
        with pytest.raises(SessionOwnedError):
            SessionLease(SID)
        assert terminate_recorded_runtime(SID, grace=2)
        agent.wait(timeout=5)
        assert not tool.is_running() or tool.status() == psutil.STATUS_ZOMBIE
        # The owner lets go once its agent is gone; a new lease is then legal.
        owner.release()
        SessionLease(SID).release()
    finally:
        if agent.poll() is None:
            agent.kill()


def test_forced_stop_never_signals_a_reused_pid(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENA_RUNTIME_LEASE_DIR", str(tmp_path / "leases"))
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        import hashlib

        key = hashlib.sha256(SID.encode()).hexdigest()
        (tmp_path / "leases").mkdir()
        (tmp_path / "leases" / (key + ".json")).write_text(json.dumps({
            "phase": "bound", "child": {"pid": bystander.pid, "born": 1.0},
        }), encoding="utf-8")
        assert terminate_recorded_runtime(SID) is False
        assert bystander.poll() is None
    finally:
        bystander.kill()


def _client():
    from ui import web

    return web, web.app.test_client()


def _delete(client, sid, query=""):
    return client.delete(
        "/api/session/" + sid + query,
        base_url="http://127.0.0.1:46747",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )


@pytest.mark.parametrize("force", [False, True])
def test_only_a_forced_delete_stops_and_waits_out_an_owner(monkeypatch, force):
    web, client = _client()
    stopped, attempts = [], []

    def delete(sid, *, source):
        attempts.append(sid)
        if len(attempts) < 3:
            raise SessionOwnedError("owned")
        return "/chat.jsonl"

    monkeypatch.setattr(web, "get_session", lambda sid: {"session_id": sid, "agent": "claude"})
    monkeypatch.setattr(web, "_fleet_worker_marker", lambda sid: None)
    monkeypatch.setattr(web, "_delete_workspace_session", delete)
    monkeypatch.setattr(web, "_stop_session_runtimes", stopped.append)
    monkeypatch.setattr(web.time, "sleep", lambda _s: None)

    response = _delete(client, SID, "?force=1" if force else "")
    if force:
        assert response.status_code == 200 and response.get_json()["ok"]
        assert stopped == [SID] and len(attempts) == 3
    else:
        assert response.status_code == 409
        assert response.get_json()["code"] == "session_owned"
        assert stopped == [] and len(attempts) == 1


def test_forced_delete_never_touches_serena_or_fleet_history(monkeypatch):
    web, client = _client()
    stopped = []
    monkeypatch.setattr(web, "_stop_session_runtimes", stopped.append)
    monkeypatch.setattr(web, "_delete_workspace_session", lambda *a, **k: pytest.fail("deleted"))
    monkeypatch.setattr(web, "get_session", lambda sid: {"session_id": sid, "agent": "serena-voice"})
    assert _delete(client, SID, "?force=1").status_code == 403
    monkeypatch.setattr(web, "get_session", lambda sid: {"session_id": sid, "agent": "claude"})
    monkeypatch.setattr(web, "_fleet_worker_marker", lambda sid: {"run_id": "r"})
    assert _delete(client, SID, "?force=1").status_code == 409
    assert stopped == []


def test_trash_routes_list_and_restore(index_env):
    web, client = _client()
    path = _transcript(index_env)
    indexer.index_session_file("claude", path)
    indexer.delete_session(SID, source="test")
    env = dict(base_url="http://127.0.0.1:46747", environ_base={"REMOTE_ADDR": "127.0.0.1"})

    listing = client.get("/api/trash", **env).get_json()
    assert listing["total"] == 1 and listing["items"][0]["session_id"] == SID

    missing = client.post("/api/trash/restore", json={"ids": ["missing"]}, **env)
    assert missing.status_code == 409 and missing.get_json()["restored"] == []

    undo = client.post("/api/trash/restore", json={"session_ids": [SID]}, **env).get_json()
    assert undo == {"ok": True, "restored": [SID], "errors": []}
    assert path.exists() and indexer.get_session(SID) is not None
