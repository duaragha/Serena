import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from flask import Flask

from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from ui.workspace_web import workspace_blueprint


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
