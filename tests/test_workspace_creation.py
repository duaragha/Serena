import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from flask import Flask

from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from ui.workspace_web import workspace_blueprint


@pytest.mark.parametrize("failure", [None, "before_checkpoint", "after_checkpoint"])
def test_creation_request_survives_repeats_and_restart_without_second_owner(tmp_path, failure):
    calls = []
    sid, request = str(uuid4()), str(uuid4())
    journal = WorkspaceJournal(tmp_path / "journal.db")
    class Owner:
        def __init__(self, *, session_id, cwd, publish):
            self.session_id, self.cwd, self.publish = session_id, cwd, publish
            self.state, self.active_turn = "opening", None
            calls.append(self)

        async def create(self, *, checkpoint):
            await asyncio.sleep(0.03)
            if failure == "before_checkpoint":
                raise RuntimeError("native response lost")
            await checkpoint({"session_id": sid, "provider": "codex", "cwd": str(self.cwd)})
            if failure == "after_checkpoint":
                raise RuntimeError("lease handoff failed")
            self.session_id, self.state = sid, "ready"
            await self.publish({"method": "workspace/history", "params": {"thread": {"id": sid, "turns": []}}})

        async def close(self):
            self.state = "closed"

    def host():
        return WorkspaceHost(journal=journal, resolve=lambda sid: None, factories={"codex": Owner})
    original = host()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: original.create(request, "codex", str(tmp_path), confirmed=True), range(4)))
        assert len(calls) == 1
        assert all(result["ok"] is (failure is None) for result in results)
        target = journal.creation_target(request)
        if failure == "before_checkpoint":
            assert target is None
        else:
            assert target["session_id"] == sid and target["committed"] is (failure is None)
        if failure is None:
            assert all(result == results[0] for result in results)
            assert original._sessions.keys() == {sid}
            assert journal.read(sid)["events"][0]["event"]["method"] == "workspace/history"
        other = tmp_path / "other"
        other.mkdir()
        with pytest.raises(ValueError, match="different content"):
            original.create(request, "codex", str(other), confirmed=True)
    finally:
        original.shutdown()
    restored = host()
    try:
        result = restored.create(request, "codex", str(tmp_path), confirmed=True)
        assert result["ok"] is (failure is None)
        assert len(calls) == 1 and restored._sessions == {}
    finally:
        restored.shutdown()


def test_creation_validates_authority_before_claiming_or_launching(tmp_path):
    journal = WorkspaceJournal(tmp_path / "journal.db")
    host = WorkspaceHost(journal=journal, resolve=lambda sid: None, factories={})
    request = str(uuid4())
    try:
        for provider, cwd, confirmed in [("claude", str(tmp_path), True), ("codex", ".", True),
                                         ("codex", str(tmp_path), "true"), ("codex", str(tmp_path / "missing"), True)]:
            with pytest.raises(ValueError):
                host.create(request, provider, cwd, confirmed=confirmed)
        assert host._sessions == {} and host._loop is None
        assert journal.creation_target(request) is None
    finally:
        host.shutdown()


def test_creation_checkpoint_cannot_change_identity_or_claimed_project(tmp_path):
    journal = WorkspaceJournal(tmp_path / "journal.db")
    request = str(uuid4())
    target = {"session_id": str(uuid4()), "provider": "codex", "cwd": str(tmp_path)}
    with pytest.raises(ValueError, match="unfinished"):
        journal.prepare_creation(request, target)
    journal.claim_command("new:" + request, request, {"action": "create_session", "payload": {
        "provider": "codex", "cwd": str(tmp_path), "confirmed": True}})
    with pytest.raises(ValueError, match="unfinished"):
        journal.prepare_creation(request, {**target, "cwd": str(tmp_path / "different")})
    journal.prepare_creation(request, target)
    journal.prepare_creation(request, target)
    with pytest.raises(ValueError, match="different identity"):
        journal.prepare_creation(request, {**target, "session_id": str(uuid4())})
    assert journal.complete_creation(request) == {"ok": True, "result": target}
    with pytest.raises(ValueError, match="finished"):
        journal.complete_creation(request)


def test_creation_api_requires_explicit_authenticated_same_origin_post(tmp_path):
    calls = []
    class Host:
        def create(self, request, provider, cwd, *, confirmed):
            calls.append((request, provider, cwd, confirmed))
            return {"ok": True, "result": {"session_id": "native"}}
    app = Flask(__name__)
    token = "x" * 40
    app.register_blueprint(workspace_blueprint(Host(), token=token))
    client = app.test_client()
    headers = {"X-Serena-Workspace-Token": token}
    body = {"request_id": str(uuid4()), "provider": "codex", "cwd": str(tmp_path), "confirmed": True}
    assert calls == []
    assert client.get("/api/workspace/create", headers=headers).status_code == 405
    assert client.post("/api/workspace/create", json=body).status_code == 403
    assert client.post("/api/workspace/create", headers={**headers, "Origin": "https://foreign.example"}, json=body).status_code == 403
    assert client.post("/api/workspace/create", headers=headers, json=body, environ_overrides={"REMOTE_ADDR": "192.0.2.1"}).status_code == 403
    for invalid in [None, [], {}, {**body, "prompt": "implicit turn"}]:
        assert client.post("/api/workspace/create", headers=headers, json=invalid).status_code == 400
    assert calls == []
    assert client.post("/api/workspace/create", headers=headers, json=body).json["ok"]
    assert calls == [(body["request_id"], "codex", str(tmp_path), True)]
