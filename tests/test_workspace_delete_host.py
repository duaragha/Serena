from types import SimpleNamespace
from uuid import uuid4

import pytest
from flask import Flask

from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from ui.workspace_web import workspace_blueprint


class DeleteOwner:
    def __init__(self, cwd):
        self.cwd = cwd
        self.state = "ready"
        self.active_turn = None
        self.questions = {}
        self.elicitations = {}
        self.active_agent_threads = set()
        self.events = SimpleNamespace(tasks={})
        self.closes = 0

    async def list_background_tasks(self):
        return {"data": []}

    async def close(self):
        self.closes += 1
        self.state = "closed"

    def can_retry_attachment(self):
        return self.state == "closed"


def _checkpoint(tmp_path, root, child, request):
    recovery = tmp_path / "workspace-deleted-sessions" / f"{root}-{request}"
    targets = []
    for identity, archived in ((root, False), (child, True)):
        path = tmp_path / "codex" / ("archived_sessions" if archived else "sessions") / f"{identity}.jsonl"
        targets.append({
            "session_id": identity,
            "provider": "codex",
            "cwd": str(tmp_path),
            "path": str(path),
            "archived": archived,
            "recovery_path": str(recovery / "rollouts" / f"{identity}.jsonl"),
            "size": 1,
            "sha256": "0" * 64,
        })
    return {
        "session_id": root,
        "provider": "codex",
        "cwd": str(tmp_path),
        "targets": targets,
        "recovery_dir": str(recovery),
    }


def test_current_codex_delete_closes_once_checkpoints_and_removes_full_catalog(tmp_path, monkeypatch):
    root, child, request = str(uuid4()), str(uuid4()), str(uuid4())
    journal = WorkspaceJournal(tmp_path / "delete-current.db")
    owner = DeleteOwner(tmp_path)
    prepared = _checkpoint(tmp_path, root, child, request)
    calls = []

    async def delete(identity, cwd, recovery, *, confirmed, family_guard, checkpoint):
        calls.append(("native-delete", identity, cwd, str(recovery), confirmed))
        assert owner.state == "closed" and journal.has_pending_delete(root)
        family_guard([{"id": root}, {"id": child}])
        checkpoint(prepared)
        assert journal.has_pending_delete(child)
        return {**prepared, "deleted": True, "thread_ids": [root, child]}

    monkeypatch.setattr("core.workspace_archive.delete_codex_tree", delete)

    def remove(target):
        calls.append(("catalog", target["session_id"]))
        return {"session_id": target["session_id"], "removed": True}

    host = WorkspaceHost(
        journal=journal,
        resolve=lambda _: pytest.fail("attached owner is authoritative"),
        factories={},
        delete_catalog=remove,
    )
    host._sessions[root] = (owner, "codex")
    try:
        result = host.delete_session(root, request, confirmed=True)
        assert result == {"ok": True, "result": {
            "session_id": root,
            "provider": "codex",
            "cwd": str(tmp_path),
            "deleted": True,
            "thread_ids": [root, child],
            "recovery_dir": prepared["recovery_dir"],
            "catalog_removed": True,
            "thread_count": 2,
        }}
        assert host.delete_session(root, request, confirmed=True) == result
        assert calls == [
            ("native-delete", root, str(tmp_path), prepared["recovery_dir"], True),
            ("catalog", root),
            ("catalog", child),
        ]
        assert owner.closes == 1 and root not in host._sessions
        assert not journal.has_pending_delete(root) and not journal.has_pending_delete(child)
        assert [item["event"]["method"] for item in journal.read(root)["events"]][-2:] == [
            "workspace/deletePrepared", "workspace/deleted",
        ]
    finally:
        host.shutdown()


@pytest.mark.parametrize("boundary", ["before", "after"])
def test_delete_failure_is_retryable_only_before_durable_checkpoint(tmp_path, monkeypatch, boundary):
    root, child, request = str(uuid4()), str(uuid4()), str(uuid4())
    journal = WorkspaceJournal(tmp_path / f"delete-{boundary}.db")
    owner = DeleteOwner(tmp_path)
    prepared = _checkpoint(tmp_path, root, child, request)
    calls = []

    async def delete(identity, cwd, recovery, *, confirmed, family_guard, checkpoint):
        calls.append("native")
        if boundary == "after":
            checkpoint(prepared)
        raise RuntimeError("native acknowledgement lost")

    monkeypatch.setattr("core.workspace_archive.delete_codex_tree", delete)
    host = WorkspaceHost(
        journal=journal,
        resolve=lambda _: None,
        factories={},
        delete_catalog=lambda _: pytest.fail("catalog must not change"),
    )
    host._sessions[root] = (owner, "codex")
    try:
        result = host.delete_session(root, request, confirmed=True)
        assert not result["ok"]
        assert bool(result.get("retryable")) is (boundary == "before")
        assert bool(result.get("uncertain")) is (boundary == "after")
        assert journal.has_pending_delete(root) is (boundary == "after")
        if boundary == "after":
            assert journal.has_pending_delete(child)
            with pytest.raises(ValueError, match="Delete outcome"):
                host.attach(child)
            with pytest.raises(ValueError, match="unconfirmed"):
                host.delete_session(root, str(uuid4()), confirmed=True)
        repeated = host.delete_session(root, request, confirmed=True)
        if boundary == "before":
            assert repeated == result
        else:
            assert repeated["uncertain"] and "will not be repeated" in repeated["error"]
        assert calls == ["native"]
    finally:
        host._sessions.clear()
        host.shutdown()


@pytest.mark.parametrize("deleted", [False, True])
def test_delete_reconciliation_never_replays_mutation(tmp_path, monkeypatch, deleted):
    root, child, request = str(uuid4()), str(uuid4()), str(uuid4())
    journal = WorkspaceJournal(tmp_path / "delete-reconcile.db")
    checkpoint = _checkpoint(tmp_path, root, child, request)
    journal.claim_delete(root, request)
    journal.prepare_delete(root, request, checkpoint)
    calls = []

    async def inspect(identity, cwd, expected, *, family_guard):
        calls.append(("inspect", identity, cwd))
        assert expected == checkpoint
        family_guard([] if deleted else [{"id": root}, {"id": child}])
        return {**checkpoint, "deleted": deleted, "thread_ids": [root, child]}

    monkeypatch.setattr("core.workspace_archive.inspect_codex_delete_tree", inspect)
    monkeypatch.setattr(
        "core.workspace_archive.delete_codex_tree",
        lambda *args, **kwargs: pytest.fail("reconciliation must not replay native delete"),
    )

    def remove(target):
        calls.append(("catalog", target["session_id"]))
        return {"session_id": target["session_id"], "removed": True}

    host = WorkspaceHost(
        journal=journal,
        resolve=lambda _: pytest.fail("checkpoint is authoritative"),
        factories={},
        delete_catalog=remove,
    )
    try:
        result = host.delete_session(root, request, confirmed=True, reconcile=True)
        assert result["ok"] is deleted
        assert result.get("retryable", False) is (not deleted)
        assert result["result"]["deleted"] is deleted
        assert not journal.has_pending_delete(root)
        assert host.delete_session(root, request, confirmed=True, reconcile=True) == result
        assert [call[0] for call in calls].count("inspect") == 1
        assert [call[0] for call in calls].count("catalog") == (2 if deleted else 0)
    finally:
        host.shutdown()


def test_catalog_failure_recovers_by_inspection_without_repeating_native_delete(tmp_path, monkeypatch):
    root, child, request = str(uuid4()), str(uuid4()), str(uuid4())
    journal = WorkspaceJournal(tmp_path / "delete-catalog.db")
    owner = DeleteOwner(tmp_path)
    prepared = _checkpoint(tmp_path, root, child, request)
    calls = []

    async def delete(*args, checkpoint, **kwargs):
        calls.append("native")
        checkpoint(prepared)
        return {**prepared, "deleted": True, "thread_ids": [root, child]}

    async def inspect(*args, family_guard, **kwargs):
        calls.append("inspect")
        family_guard([])
        return {**prepared, "deleted": True, "thread_ids": [root, child]}

    monkeypatch.setattr("core.workspace_archive.delete_codex_tree", delete)
    monkeypatch.setattr("core.workspace_archive.inspect_codex_delete_tree", inspect)
    failures = {root: 1}

    def remove(target):
        identity = target["session_id"]
        calls.append("catalog:" + identity)
        if failures.pop(identity, 0):
            raise RuntimeError("catalog unavailable")
        return {"session_id": identity, "removed": True}

    host = WorkspaceHost(journal=journal, resolve=lambda _: None, factories={}, delete_catalog=remove)
    host._sessions[root] = (owner, "codex")
    try:
        failed = host.delete_session(root, request, confirmed=True)
        assert failed["uncertain"] and journal.has_pending_delete(root)
    finally:
        host._sessions.clear()
        host.shutdown()
    restored = WorkspaceHost(
        journal=WorkspaceJournal(journal.path),
        resolve=lambda _: pytest.fail("recovery uses checkpoint"),
        factories={},
        delete_catalog=remove,
    )
    try:
        result = restored.delete_session(root, request, confirmed=True, reconcile=True)
        assert result["ok"] and result["result"]["thread_count"] == 2
        assert calls.count("native") == 1 and calls.count("inspect") == 1
    finally:
        restored.shutdown()


@pytest.mark.parametrize("reconcile", [False, True])
def test_delete_route_requires_auth_and_exact_body(reconcile):
    calls = []
    app = Flask(__name__)
    host = SimpleNamespace(
        delete_session=lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True}
    )
    app.register_blueprint(workspace_blueprint(host, token="s" * 40))
    client = app.test_client()
    suffix = "reconcile-delete-session" if reconcile else "delete-session"
    path = f"/api/workspace/exact/{suffix}"
    payload = {"request_id": "request", "confirmed": True}
    assert client.post(path, json=payload).status_code == 403
    headers = {"X-Serena-Workspace-Token": "s" * 40}
    assert client.post(path, json={**payload, "cwd": "/other"}, headers=headers).status_code == 400
    assert client.post(path, json=payload, headers={**headers, "Origin": "https://other.test"}).status_code == 403
    assert not calls
    assert client.post(path, json=payload, headers=headers).json == {"ok": True}
    assert calls == [(("exact", "request"), {
        "confirmed": True,
        **({"reconcile": True} if reconcile else {}),
    })]


def test_pending_delete_catalog_annotation_covers_root_and_child(tmp_path):
    root, child, request = str(uuid4()), str(uuid4()), str(uuid4())
    journal = WorkspaceJournal(tmp_path / "delete-catalog-annotation.db")
    journal.claim_delete(root, request)
    journal.prepare_delete(root, request, _checkpoint(tmp_path, root, child, request))
    host = WorkspaceHost(journal=journal, resolve=lambda _: None, factories={}, delete_catalog=lambda _: None)
    page = {"data": [{"session_id": root}, {"session_id": child}, {"session_id": str(uuid4())}], "nextOffset": None}
    try:
        result = host.decorate_archive_restores(page)
        pending = {"delete_request_id": request, "delete_source_id": root}
        assert result["data"][0] == {**page["data"][0], **pending}
        assert result["data"][1] == {**page["data"][1], **pending}
        assert result["data"][2] == page["data"][2]
        assert host._loop is None
    finally:
        host.shutdown()
