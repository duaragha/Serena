import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from flask import Flask

from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from ui.workspace_web import workspace_blueprint


class ArchiveOwner:
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


def test_current_codex_archive_closes_once_checkpoints_tree_and_catalogs_every_thread(tmp_path, monkeypatch):
    root, child, request = str(uuid4()), str(uuid4()), str(uuid4())
    journal = WorkspaceJournal(tmp_path / "archive-current.db")
    owner = ArchiveOwner(tmp_path)
    calls = []
    targets = [{"session_id": identity, "provider": "codex", "cwd": str(tmp_path)}
               for identity in [root, child]]

    async def archive(identity, cwd, *, confirmed, family_guard, checkpoint):
        calls.append(("archive", identity, cwd, confirmed))
        assert owner.state == "closed" and journal.has_pending_archive(root)
        family_guard([{"id": identity, "cwd": str(tmp_path)} for identity in [root, child]])
        checkpoint({"session_id": root, "provider": "codex", "cwd": str(tmp_path), "targets": targets})
        assert journal.has_pending_archive(child)
        return {"session_id": root, "provider": "codex", "cwd": str(tmp_path), "archived": True,
                "thread_ids": [root, child], "targets": targets}

    monkeypatch.setattr("core.workspace_archive.archive_codex_tree", archive)
    host = WorkspaceHost(
        journal=journal, resolve=lambda _: pytest.fail("attached identity must remain authoritative"), factories={},
        register_fork=lambda target: calls.append(("catalog", target["session_id"])) or {"session_id": target["session_id"]},
    )
    host._sessions[root] = (owner, "codex")
    try:
        result = host.archive_session(root, request, confirmed=True)
        assert result == {"ok": True, "result": {"session_id": root, "provider": "codex", "cwd": str(tmp_path),
                                                       "archived": True, "thread_ids": [root, child],
                                                       "cataloged": True, "thread_count": 2}}
        assert host.archive_session(root, request, confirmed=True) == result
        assert calls == [("archive", root, str(tmp_path), True), ("catalog", root), ("catalog", child)]
        assert owner.closes == 1 and root not in host._sessions
        assert journal.archive_checkpoint(root, request)["targets"] == targets
        assert not journal.has_pending_archive(root) and not journal.has_pending_archive(child)
        events = [item["event"]["method"] for item in journal.read(root)["events"]]
        assert events[-2:] == ["workspace/archivePrepared", "workspace/archived"]
    finally:
        host.shutdown()


@pytest.mark.parametrize("boundary", ["before", "after"])
def test_archive_failure_is_retryable_only_before_durable_mutation_checkpoint(tmp_path, monkeypatch, boundary):
    root, child, request = str(uuid4()), str(uuid4()), str(uuid4())
    journal = WorkspaceJournal(tmp_path / f"archive-{boundary}.db")
    owner = ArchiveOwner(tmp_path)
    targets = [{"session_id": identity, "provider": "codex", "cwd": str(tmp_path)}
               for identity in [root, child]]

    async def archive(identity, cwd, *, confirmed, family_guard, checkpoint):
        if boundary == "after":
            checkpoint({"session_id": root, "provider": "codex", "cwd": str(tmp_path), "targets": targets})
        raise RuntimeError("native acknowledgement lost")

    monkeypatch.setattr("core.workspace_archive.archive_codex_tree", archive)
    host = WorkspaceHost(journal=journal, resolve=lambda _: None, factories={}, register_fork=lambda _: {})
    host._sessions[root] = (owner, "codex")
    try:
        result = host.archive_session(root, request, confirmed=True)
        assert not result["ok"]
        assert bool(result.get("retryable")) is (boundary == "before")
        assert bool(result.get("uncertain")) is (boundary == "after")
        assert journal.has_pending_archive(root) is (boundary == "after")
        if boundary == "after":
            assert journal.has_pending_archive(child)
            with pytest.raises(ValueError, match="unconfirmed"):
                host.attach(child)
            with pytest.raises(ValueError, match="unconfirmed"):
                host.archive_session(root, str(uuid4()), confirmed=True)
        repeated = host.archive_session(root, request, confirmed=True)
        if boundary == "before":
            assert repeated == result
        else:
            assert repeated["uncertain"] and not repeated["ok"]
    finally:
        host._sessions.clear()
        host.shutdown()


@pytest.mark.parametrize("archived", [False, True])
def test_archive_reconciliation_never_replays_mutation_and_finishes_exact_receipt(tmp_path, monkeypatch, archived):
    root, child, request = str(uuid4()), str(uuid4()), str(uuid4())
    journal = WorkspaceJournal(tmp_path / "archive-reconcile.db")
    targets = [{"session_id": identity, "provider": "codex", "cwd": str(tmp_path)}
               for identity in [root, child]]
    checkpoint = {"session_id": root, "provider": "codex", "cwd": str(tmp_path), "targets": targets}
    journal.claim_archive(root, request)
    journal.prepare_archive(root, request, checkpoint)
    calls = []

    async def inspect(identity, cwd, expected, *, family_guard):
        calls.append((identity, cwd, expected))
        family_guard([{"id": target["session_id"], "cwd": target["cwd"]} for target in expected])
        return {**checkpoint, "archived": archived, "thread_ids": [root, child]}

    monkeypatch.setattr("core.workspace_archive.inspect_codex_archive_tree", inspect)
    monkeypatch.setattr("core.workspace_archive.archive_codex_tree", lambda *args, **kwargs: pytest.fail("must not replay"))
    host = WorkspaceHost(
        journal=journal, resolve=lambda _: pytest.fail("checkpoint is authoritative"), factories={},
        register_fork=lambda target: {"session_id": target["session_id"]},
    )
    try:
        result = host.archive_session(root, request, confirmed=True, reconcile=True)
        assert len(calls) == 1
        assert result["ok"] is archived
        assert result.get("retryable", False) is (not archived)
        assert result["result"]["archived"] is archived
        assert not journal.has_pending_archive(root)
        assert host.archive_session(root, request, confirmed=True, reconcile=True) == result
        assert len(calls) == 1
    finally:
        host.shutdown()


def test_archive_reconcile_without_checkpoint_proves_no_mutation_and_releases_claim(tmp_path, monkeypatch):
    root, request = str(uuid4()), str(uuid4())
    journal = WorkspaceJournal(tmp_path / "archive-no-checkpoint.db")
    journal.claim_archive(root, request)
    monkeypatch.setattr("core.workspace_archive.inspect_codex_archive_tree", lambda *args, **kwargs: pytest.fail("no inspection"))
    host = WorkspaceHost(journal=journal, resolve=lambda _: pytest.fail("no resolution"), factories={})
    try:
        result = host.archive_session(root, request, confirmed=True, reconcile=True)
        assert not result["ok"] and result["retryable"] and result["result"]["archived"] is False
        assert not journal.has_pending_archive(root)
    finally:
        host.shutdown()


def test_archived_catalog_session_cannot_attach_before_explicit_restore(tmp_path):
    sid = str(uuid4())
    host = WorkspaceHost(
        journal=WorkspaceJournal(tmp_path / "archived-attach.db"),
        resolve=lambda identity: {"session_id": identity, "provider": "codex", "cwd": str(tmp_path),
                                  "archived": True},
        factories={"codex": lambda **kwargs: pytest.fail("archived session must not launch")},
    )
    try:
        with pytest.raises(ValueError, match="Restore this archived"):
            host.attach(sid)
        assert host._sessions == {}
    finally:
        host.shutdown()


@pytest.mark.parametrize("reconcile", [False, True])
def test_archive_route_requires_auth_and_exact_body(reconcile):
    calls = []
    app = Flask(__name__)
    host = SimpleNamespace(archive_session=lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True})
    app.register_blueprint(workspace_blueprint(host, token="s" * 40))
    client = app.test_client()
    suffix = "reconcile-archive-session" if reconcile else "archive-session"
    path = f"/api/workspace/exact/{suffix}"
    payload = {"request_id": "request", "confirmed": True}
    assert client.post(path, json=payload).status_code == 403
    headers = {"X-Serena-Workspace-Token": "s" * 40}
    assert client.post(path, json={**payload, "cwd": "/other"}, headers=headers).status_code == 400
    assert client.post(path, json=payload, headers={**headers, "Origin": "https://other.test"}).status_code == 403
    assert not calls
    assert client.post(path, json=payload, headers=headers).json == {"ok": True}
    assert calls == [(("exact", "request"), {"confirmed": True, **({"reconcile": True} if reconcile else {})})]


def test_pending_restore_catalog_annotation_is_read_only_and_exact(tmp_path):
    sid, other, request = str(uuid4()), str(uuid4()), str(uuid4())
    journal = WorkspaceJournal(tmp_path / 'journal.db')
    journal.claim_archive_restore(sid, request)
    host = WorkspaceHost(journal=journal, resolve=lambda _: pytest.fail('No resolution'), factories={})
    try:
        for archived in (0, 1):
            page = {'data': [{'session_id': sid, 'is_archived': archived, 'title': 'Custom'}, {'session_id': other}], 'nextOffset': 50}
            result = host.decorate_archive_restores(page)
            assert result['data'][0] == {**page['data'][0], 'archive_restore_request_id': request}
            assert result['data'][1] == page['data'][1]
            assert 'archive_restore_request_id' not in page['data'][0]
            assert result['nextOffset'] == 50 and host._loop is None
        journal.finish_command(sid, request, {'ok': True})
        assert host.decorate_archive_restores(page) == page
    finally:
        host.shutdown()


def test_pending_archive_catalog_annotation_survives_browser_receipt_loss(tmp_path):
    sid, child, other, request = str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())
    journal = WorkspaceJournal(tmp_path / "archive-catalog.db")
    journal.claim_archive(sid, request)
    journal.prepare_archive(sid, request, {
        "session_id": sid, "provider": "codex", "cwd": str(tmp_path), "targets": [
            {"session_id": sid, "provider": "codex", "cwd": str(tmp_path)},
            {"session_id": child, "provider": "codex", "cwd": str(tmp_path)},
        ],
    })
    host = WorkspaceHost(journal=journal, resolve=lambda _: pytest.fail("No resolution"), factories={})
    page = {"data": [
        {"session_id": sid, "is_archived": 1},
        {"session_id": child, "is_archived": 1},
        {"session_id": other},
    ], "nextOffset": None}
    try:
        result = host.decorate_archive_restores(page)
        pending = {"archive_request_id": request, "archive_source_id": sid}
        assert result["data"][0] == {**page["data"][0], **pending}
        assert result["data"][1] == {**page["data"][1], **pending}
        assert result["data"][2] == page["data"][2]
        assert host._loop is None
        journal.finish_command(sid, request, {"ok": True})
        assert host.decorate_archive_restores(page) == page
    finally:
        host.shutdown()


@pytest.mark.parametrize('failure', [None, 'native', 'catalog', 'receipt'])
def test_restore_receipt_survives_concurrent_clicks_and_restart(tmp_path, monkeypatch, failure):
    sid, request = str(uuid4()), str(uuid4())
    journal = WorkspaceJournal(tmp_path / 'journal.db')
    calls = []

    async def restore(identity, cwd, *, confirmed):
        assert identity == sid and cwd == str(tmp_path) and confirmed
        assert journal.has_pending_archive_restore(sid)
        calls.append('native')
        await asyncio.sleep(.03)
        if failure == 'native':
            raise RuntimeError('acknowledgement lost')
        return {'session_id': sid, 'provider': 'codex', 'cwd': cwd, 'archived': False}

    def register(target):
        calls.append('catalog')
        assert target['session_id'] == sid
        if failure == 'catalog':
            raise RuntimeError('index unavailable')
        return {'session_id': sid, 'indexed': True}

    monkeypatch.setattr('core.workspace_archive.restore_codex_archive', restore)
    if failure == 'receipt':
        monkeypatch.setattr(journal, 'finish_command', lambda *args: (_ for _ in ()).throw(RuntimeError('disk unavailable')))
    def make_host():
        return WorkspaceHost(journal=journal, resolve=lambda identity: {'session_id': identity, 'provider': 'codex', 'cwd': str(tmp_path)},
                             factories={}, register_fork=register)
    host = make_host()
    try:
        assert host._loop is None and not calls
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: host.restore_archive(sid, request, confirmed=True), range(3)))
        assert all(result['ok'] == (failure is None) for result in results)
        assert calls.count('native') == 1
        assert host._sessions == {}
    finally:
        host.shutdown()
    restored = make_host()
    try:
        result = restored.restore_archive(sid, request, confirmed=True)
        assert result['ok'] == (failure is None)
        if failure:
            assert result['uncertain']
            with pytest.raises(ValueError, match='unconfirmed'):
                restored.restore_archive(sid, str(uuid4()), confirmed=True)
            with pytest.raises(ValueError, match='unconfirmed'):
                restored.attach(sid)
        else:
            assert result == results[0]
        assert calls.count('native') == 1
    finally:
        restored.shutdown()


@pytest.mark.parametrize('guard', ['confirmation', 'owned', 'reserved', 'queued', 'provider', 'identity', 'catalog'])
def test_restore_rejects_before_native_mutation(tmp_path, monkeypatch, guard):
    sid, request = str(uuid4()), str(uuid4())
    journal = WorkspaceJournal(tmp_path / 'journal.db')
    monkeypatch.setattr('core.workspace_archive.restore_codex_archive', lambda *args, **kwargs: pytest.fail('No native mutation'))
    target = {'session_id': str(uuid4()) if guard == 'identity' else sid,
              'provider': 'claude' if guard == 'provider' else 'codex', 'cwd': str(tmp_path)}
    host = WorkspaceHost(journal=journal, resolve=lambda identity: target, factories={},
                         register_fork=None if guard == 'catalog' else lambda value: value)
    try:
        if guard == 'owned':
            host._sessions[sid] = (SimpleNamespace(state='running'), 'codex')
        if guard == 'reserved':
            host._work_reservations[sid] = 'job'
        if guard == 'queued':
            host._bridge_queues[sid] = ['message']
        with pytest.raises(ValueError):
            host.restore_archive(sid, request, confirmed=guard != 'confirmation')
        assert journal.command_record(sid, request) is None
    finally:
        host._sessions.clear()
        host._bridge_queues.clear()
        host.shutdown()


@pytest.mark.parametrize('reconcile', [False, True])
def test_restore_route_requires_auth_and_exact_body(reconcile):
    calls = []
    app = Flask(__name__)
    host = SimpleNamespace(restore_archive=lambda *args, **kwargs: calls.append((args, kwargs)) or {'ok': True})
    app.register_blueprint(workspace_blueprint(host, token='s' * 40))
    client = app.test_client()
    path = '/api/workspace/exact/' + ('reconcile-archive' if reconcile else 'restore-archive')
    payload = {'request_id': 'request', 'confirmed': True}
    assert client.post(path, json=payload).status_code == 403
    headers = {'X-Serena-Workspace-Token': 's' * 40}
    assert client.post(path, json={**payload, 'cwd': '/other'}, headers=headers).status_code == 400
    assert client.post(path, json=payload, headers={**headers, 'Origin': 'https://other.test'}).status_code == 403
    assert not calls
    assert client.post(path, json=payload, headers=headers).json == {'ok': True}
    assert calls == [(('exact', 'request'), {'confirmed': True, **({'reconcile': True} if reconcile else {})})]


def test_distinct_restore_requests_cannot_bypass_pending_claim(tmp_path):
    journal = WorkspaceJournal(tmp_path / 'journal.db')
    sid = str(uuid4())

    def claim(_):
        try:
            return journal.claim_archive_restore(sid, str(uuid4()))[0]
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(claim, range(4))) == 1
    assert journal.has_pending_archive_restore(sid)
    assert not journal.has_pending_archive_restore(str(uuid4()))


@pytest.mark.parametrize('pending', ['work', 'clear'])
def test_unconfirmed_prior_work_prevents_restore(tmp_path, monkeypatch, pending):
    sid, request = str(uuid4()), str(uuid4())
    journal = WorkspaceJournal(tmp_path / 'journal.db')
    monkeypatch.setattr(journal, 'has_pending_work' if pending == 'work' else 'has_pending_clear', lambda _: True)
    host = WorkspaceHost(journal=journal, resolve=lambda _: pytest.fail('No resolution'), factories={}, register_fork=lambda _: None)
    try:
        with pytest.raises(ValueError, match='unconfirmed'):
            host.restore_archive(sid, request, confirmed=True)
        assert journal.command_record(sid, request) is None
    finally:
        host.shutdown()


@pytest.mark.parametrize('archived', [False, True])
@pytest.mark.parametrize('fail_inspection', [False, True])
def test_reconcile_only_checks_native_state_and_finishes_exact_pending_receipt(tmp_path, monkeypatch, archived, fail_inspection):
    sid, request = str(uuid4()), str(uuid4())
    journal = WorkspaceJournal(tmp_path / 'journal.db')
    journal.claim_archive_restore(sid, request)
    calls = []

    async def inspect(identity, cwd, *, confirmed, inspect_only):
        assert identity == sid and confirmed and inspect_only
        calls.append(identity)
        if fail_inspection:
            raise RuntimeError('ownership unconfirmed')
        return {'session_id': sid, 'provider': 'codex', 'cwd': cwd, 'archived': archived}

    monkeypatch.setattr('core.workspace_archive.restore_codex_archive', inspect)
    host = WorkspaceHost(journal=journal, resolve=lambda identity: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
                         factories={}, register_fork=lambda _: None)
    try:
        with pytest.raises(ValueError, match='No matching'):
            host.restore_archive(sid, str(uuid4()), confirmed=True, reconcile=True)
        assert not calls
        result = host.restore_archive(sid, request, confirmed=True, reconcile=True)
        assert len(calls) == 1 and not host._sessions
        assert journal.has_pending_archive_restore(sid) == fail_inspection
        if fail_inspection:
            assert result['uncertain'] and not result['ok']
        else:
            assert result['ok'] == (not archived)
            assert result.get('retryable', False) == archived
            assert host.restore_archive(sid, request, confirmed=True, reconcile=True) == result
            assert len(calls) == 1
    finally:
        host.shutdown()
