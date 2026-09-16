"""Native Muse envelopes, exact resume, streaming and legacy view recovery."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from core import muse_scanner
from core.workspace_muse import MuseWorkspace, command_id

SID = "11111111-2222-4333-8444-555555555555"


def record(kind, payload, at=1789496668651273):
    return {"schema_version": 1, "payload_type": kind, "recorded_at": at, "payload": payload}


def native_log(tmp_path, monkeypatch):
    root = tmp_path / "muse" / "sessions"
    monkeypatch.setattr(muse_scanner, "SESSIONS_DIR", root)
    path = root / "2026/09/15" / SID / "session.jsonl"
    path.parent.mkdir(parents=True)
    rows = [
        record("runtime.session.metadata", {"kind": "metadata", "record": {"workspace_root": str(tmp_path)}}),
        record("runtime.user_intent.accepted", {"intent_id": "turn-1", "semantic_kind": {"kind": "chat"},
            "model_messages": [{"content": [{"kind": "text", "text": "repair Muse history"},
                                            {"kind": "text", "text": "keep both blocks"}]}]}),
        record("runtime.session", {"kind": "run", "run_id": "turn-1", "event": {
            "kind": "started", "prompt": "repair Muse history"}}),
        record("runtime.session", {"kind": "run", "run_id": "turn-1", "event": {
            "kind": "assistant_message_committed", "text": "repaired"}}),
        record("runtime.session", {"kind": "task", "event": {"kind": "output", "text": "tool noise"}}),
    ]
    path.write_text("\n".join(map(json.dumps, rows)) + "\n{unfinished", encoding="utf-8")
    return path


def test_real_envelopes_restore_titles_projects_and_read_view(tmp_path, monkeypatch):
    from core.parser import parse_full, parse_messages_for_search

    path = native_log(tmp_path, monkeypatch)
    meta = muse_scanner.parse_muse_metadata(path)
    assert meta.first_message == "repair Muse history\nkeep both blocks"
    assert meta.cwd == str(tmp_path)
    assert meta.message_count == 2
    assert meta.first_timestamp.year == 2026
    assert [m.text for m in parse_full(path)] == [meta.first_message, "repaired"]
    assert len(parse_messages_for_search(path)) == 2


def test_internal_logs_are_not_listed_or_resumable(tmp_path, monkeypatch):
    path = native_log(tmp_path, monkeypatch)
    for family in ("subagent", "reminder", "approval-review"):
        child = path.parent / family / "22222222-2222-4222-8222-222222222222" / "session.jsonl"
        child.parent.mkdir(parents=True)
        child.write_text(path.read_text())
    assert list(muse_scanner.scan_muse_sessions()) == [("muse", path)]
    assert muse_scanner.resumable_session_path("22222222-2222-4222-8222-222222222222") is None


def test_unknown_envelopes_cannot_break_catalog_refresh(tmp_path, monkeypatch):
    path = native_log(tmp_path, monkeypatch)
    with path.open("a") as stream:
        for payload in (["future"], "future", None, {"model_messages": None},
                        {"model_messages": [None, {"content": None}], "semantic_kind": {"kind": "chat"}}):
            stream.write("\n" + json.dumps(record("runtime.user_intent.accepted", payload)))
    assert muse_scanner.parse_muse_metadata(path).message_count == 2


def test_upgrade_reparses_unchanged_untitled_rows_and_removes_empty_rows(tmp_path, monkeypatch):
    from core import indexer, metadata, project_mirror

    path = native_log(tmp_path, monkeypatch)
    monkeypatch.setattr(indexer, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(indexer, "DATA_DIR", tmp_path)
    monkeypatch.setattr(indexer, "_schema_ready", False)
    monkeypatch.setattr(metadata, "get_all_meta", lambda: {SID: {"custom_title": "My Muse chat", "starred": True}})
    monkeypatch.setattr(project_mirror, "sync_mirrors", lambda: None)
    for scanner in ("scan_sessions", "scan_codex_sessions", "scan_locket_sessions", "scan_voice_sessions", "scan_gemini_sessions"):
        monkeypatch.setattr(indexer, scanner, lambda: [])
    conn = indexer._get_db()
    for sid, fp in [(SID, path), ("empty", path.parent.parent / "empty/session.jsonl"),
                    ("child", path.parent / "subagent/child/session.jsonl")]:
        if not fp.exists():
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text('{"type":"ping"}\n')
        stat = fp.stat()
        conn.execute("INSERT INTO sessions (session_id,project_dir,agent,title,file_path,file_size,file_mtime,raw_message_count) VALUES (?,?,?,?,?,?,?,?)",
                     (sid, "muse", "muse", "Untitled chat", str(fp), stat.st_size, stat.st_mtime, 0))
    conn.commit()
    conn.close()
    indexer.update_index()
    conn = indexer._get_db()
    rows = conn.execute("SELECT * FROM sessions").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["first_message"].startswith("repair Muse")
    assert rows[0]["custom_title"] == "My Muse chat"
    assert rows[0]["starred"] == 1
    assert rows[0]["cwd"] == str(tmp_path)
    assert path.exists()


class Lease:
    def __init__(self, sid):
        self.sid = sid
        self.released = False

    def launching(self): pass
    def bind(self, pid): pass
    def transfer_after_transition(self, sid):
        self.sid = sid
        return self
    def release(self): self.released = True


class Rpc:
    def __init__(self):
        self.events = asyncio.Queue()
        self.process = None
        self.calls = []
        self.pages = []
        self.history = {"mode": "inline", "items": [], "snapshot": None}

    async def start(self, command, **kwargs):
        assert command[-1] == "serve"
        self.process = SimpleNamespace(pid=42)

    async def request(self, method, params):
        self.calls.append((method, params))
        if "commandId" in params:
            assert UUID(params["commandId"]).version == 7
        if method == "initialize": return {"schema": {"version": 1}}
        if method in {"session/start", "session/resume"}:
            return {"session": {"sessionId": SID, "activeTurnId": None, "modelId": "native-muse"}, "history": self.history}
        if method == "view/page": return self.pages.pop(0) if self.pages else {"events": [], "nextCursor": None}
        if method == "turn/start": return {"turnId": "turn-2"}
        return {}

    async def notify(self, *args): pass
    async def respond(self, *args): pass
    async def close(self): self.process = None


def owner(tmp_path, rpc=None):
    rpc = rpc or Rpc()
    events = []

    async def publish(e): events.append(e)

    obj = MuseWorkspace(session_id=SID, cwd=tmp_path, publish=publish,
                        binary="muse", rpc_factory=lambda: rpc, lease_factory=Lease)
    return obj, rpc, events


def test_resume_keeps_identity_and_stop_does_not_wait_for_completion(tmp_path):
    async def run():
        obj, rpc, events = owner(tmp_path)
        try:
            await obj.open()
            assert any(m == "session/resume" and p["sessionId"] == SID for m, p in rpc.calls)
            assert not any(m == "session/start" for m, _ in rpc.calls)
            await obj.submit([{"type": "text", "text": "next"}])
            assert obj.active_turn == "turn-2"
            await asyncio.wait_for(obj.interrupt(), 0.2)
            assert rpc.calls[-1][0] == "turn/interrupt"
            await obj._event({"method": "turn/completed", "params": {"turnId": "turn-2", "terminal": "cancelled"}})
            assert obj.state == "ready" and obj.active_turn is None
        finally:
            await obj.close()
        assert obj.can_retry_attachment()
    asyncio.run(run())


def test_native_history_refusal_keeps_readable_history_without_rewriting_or_forking(tmp_path, monkeypatch):
    from core.workspace_muse import MuseWorkspaceError
    from core.workspace_rpc import WorkspaceRpcError

    path = native_log(tmp_path, monkeypatch)
    original = path.read_bytes()

    class RefusingRpc(Rpc):
        async def request(self, method, params):
            result = await super().request(method, params)
            if method == 'session/resume':
                raise WorkspaceRpcError('internal error: classify turns: session fork rejected: MalformedJsonl')
            return result

    async def run():
        obj, rpc, events = owner(tmp_path, RefusingRpc())
        with pytest.raises(MuseWorkspaceError, match='Saved messages remain readable') as error:
            await obj.open()
        assert isinstance(error.value.__cause__, WorkspaceRpcError)
        assert obj.state == 'unavailable' and obj.can_retry_attachment()
        assert rpc.process is None and obj._lease is None
        history = next(e for e in events if e['method'] == 'workspace/history')
        assert history['params']['thread']['id'] == SID
        assert history['params']['thread']['turns'][0]['items'][1]['text'] == 'repaired'
        assert sum(m == 'session/resume' for m, _ in rpc.calls) == 1
        assert not any(m in {'session/start', 'session/fork', 'turn/start'} for m, _ in rpc.calls)
        with pytest.raises(MuseWorkspaceError, match='not ready'):
            await obj.submit([{'type': 'text', 'text': 'do not replay'}])
        assert path.read_bytes() == original

    asyncio.run(run())


def test_streaming_items_keep_native_details_and_final_revision(tmp_path):
    async def run():
        obj, rpc, events = owner(tmp_path)
        item = {"itemId": "a", "kind": "agentMessage", "turnId": "t", "revision": 1, "status": "inProgress", "text": ""}
        await obj._event({"method": "item/started", "params": {"item": item}})
        await obj._event({"method": "item/delta", "params": {"itemId": "a", "delta": "hello"}})
        assert events[-1]["params"]["item"]["text"] == "hello"
        final = {**item, "text": "hello world", "status": "completed", "revision": 2}
        await obj._event({"method": "item/completed", "params": {"item": final}})
        await obj._event({"method": "item/started", "params": {"item": item}})
        assert events[-1]["params"]["item"]["text"] == "hello world"
        tool = {**item, "kind": "toolCall", "tool": "bash", "args": '{"command":"pwd"}', "visibleOutput": "/tmp"}
        translated = obj._item(tool)
        assert translated["input"] == {"command": "pwd"}
        assert translated["output"] == "/tmp"
    asyncio.run(run())


def test_unavailable_live_projection_uses_native_pages_without_resending(tmp_path):
    async def run():
        rpc = Rpc()
        rpc.history = {"mode": "none", "noneReason": "projectionUnavailable"}
        item = {"itemId": "old", "kind": "agentMessage", "turnId": "old-turn", "revision": 1,
                "status": "completed", "text": "retained answer"}
        rpc.pages = [{"events": [{"method": "item/completed", "params": {"item": item, "viewCursor": "observed-1"}}],
                      "nextCursor": None}]
        obj, _, events = owner(tmp_path, rpc)
        try:
            await obj.open()
            assert obj._paged
            assert events[-1]["params"]["thread"]["turns"][0]["items"][0]["text"] == "retained answer"
            await obj.submit([{"type": "text", "text": "next"}])
            rpc.pages = [{"events": [{"method": "turn/completed", "params": {
                "turnId": "turn-2", "terminal": "completed", "viewCursor": "observed-2"}}], "nextCursor": None}]
            async with asyncio.timeout(3):
                while obj.active_turn: await asyncio.sleep(0.01)
            assert sum(m == "turn/start" for m, _ in rpc.calls) == 1
            assert any(m == "view/page" and p.get("cursor") == "observed-1" for m, p in rpc.calls)
        finally:
            await obj.close()
    asyncio.run(run())


def test_approval_requires_current_choice_and_is_not_automatically_granted(tmp_path):
    async def run():
        obj, rpc, events = owner(tmp_path)
        p = {"approvalId": "a", "currentRequirementId": "stage-1", "toolName": "bash", "subject": {"command": "pwd"},
             "availableChoices": [{"choiceId": "once", "label": "Allow once", "scope": "once"}]}
        await obj._event({"method": "approval/requested", "params": p})
        assert not rpc.calls
        assert events[-1]["method"] == "session/request_permission"
        with pytest.raises(Exception, match="Invalid Muse approval"):
            await obj.answer("approval:a", {"outcome": {"optionId": "invented"}})
        await obj.answer("approval:a", {"outcome": {"outcome": "selected", "optionId": "once"}})
        assert rpc.calls[-1][1]["requirementId"] == "stage-1"
    asyncio.run(run())
