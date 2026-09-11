import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.workspace_archive import (
    archive_codex_tree,
    inspect_codex_archive_tree,
    restore_codex_archive,
)


@pytest.mark.parametrize('case', ['ok', 'unconfirmed', 'owned', 'factory', 'foreign', 'wrong-project', 'not-archived', 'lost', 'wrong-result', 'loaded', 'cleanup'])
@pytest.mark.parametrize('inspect_only', [False, True])
def test_restore_is_exact_exclusive_and_never_resumes(tmp_path, case, inspect_only):
    sid = str(uuid4())
    calls = []
    home = tmp_path / 'codex'

    class Rpc:
        process = None
        restored = False

        def __init__(self):
            if case == 'factory':
                raise RuntimeError('transport initialization failed')

        async def start(self, argv, **kwargs):
            calls.append(('start', argv))
            assert 'OPENAI_API_KEY' not in kwargs['env']
            self.process = SimpleNamespace(pid=123)

        async def notify(self, *args): pass

        async def request(self, method, params):
            calls.append((method, params))
            if method == 'thread/read':
                directory = 'sessions' if self.restored or case == 'not-archived' else 'archived_sessions'
                return {'thread': {'id': str(uuid4()) if case == 'foreign' else sid,
                                   'cwd': str(tmp_path / 'other' if case == 'wrong-project' else tmp_path),
                                   'path': str(home / directory / 'rollout.jsonl')}}
            if method == 'thread/unarchive':
                if case == 'lost':
                    raise TimeoutError('acknowledgment lost')
                self.restored = True
                return {'thread': {'id': str(uuid4()) if case == 'wrong-result' else sid}}
            if method == 'thread/loaded/list':
                return {'data': [sid] if case == 'loaded' else []}
            assert method == 'initialize'
            return {}

        async def close(self):
            calls.append(('close', None))
            if case != 'cleanup':
                self.process = None

    def lease_factory(identity):
        assert identity == sid
        calls.append(('lease', identity))
        if case == 'owned':
            raise RuntimeError('already owned')
        return SimpleNamespace(launching=lambda: None, bind=lambda pid: None,
                               release=lambda: calls.append(('release', identity)))

    async def run():
        args = dict(confirmed=case != 'unconfirmed', binary='codex', env={'CODEX_HOME': str(home), 'OPENAI_API_KEY': 'never-forward'},
                    rpc_factory=Rpc, lease_factory=lease_factory, inspect_only=inspect_only)
        if case == 'ok' or (inspect_only and case in {'not-archived', 'lost', 'wrong-result'}):
            result = await restore_codex_archive(sid, tmp_path, **args)
            assert result == {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path), 'archived': inspect_only and case != 'not-archived'}
        else:
            with pytest.raises((ValueError, RuntimeError, TimeoutError)):
                await restore_codex_archive(sid, tmp_path, **args)
        methods = [method for method, _ in calls]
        assert not {'thread/resume', 'thread/start', 'turn/start'} & set(methods)
        assert methods.count('thread/unarchive') == (0 if inspect_only or case in {'unconfirmed', 'owned', 'factory', 'foreign', 'wrong-project', 'not-archived'} else 1)
        if case == 'unconfirmed':
            assert calls == []
        elif case == 'owned':
            assert methods == ['lease']
        elif case == 'factory':
            assert methods == ['lease', 'release']
        else:
            assert methods[-2:] == ['close', 'release']
    asyncio.run(run())


@pytest.mark.parametrize('case', ['ok', 'owned-child', 'owner-guard', 'family-guard', 'loaded',
                                  'ancestry', 'changed', 'bad-ack', 'wrong-notice', 'post-active', 'cleanup'])
def test_archive_tree_leases_and_verifies_every_active_descendant(tmp_path, case):
    root, child, grandchild, old = (str(uuid4()) for _ in range(4))
    home = tmp_path / 'codex'
    calls, leases = [], []

    class Rpc:
        process = None
        archived = False

        def __init__(self):
            self.events = asyncio.Queue()

        async def start(self, argv, **kwargs):
            calls.append(('start', argv))
            assert 'OPENAI_API_KEY' not in kwargs['env']
            self.process = SimpleNamespace(pid=123)

        async def notify(self, *args): pass

        async def request(self, method, params):
            calls.append((method, params))
            if method == 'initialize':
                return {}
            if method == 'thread/read':
                identity = params['threadId']
                parents = {root: None, child: root, grandchild: child, old: root}
                directory = 'archived_sessions' if self.archived or identity == old else 'sessions'
                return {'thread': {'id': identity, 'parentThreadId': parents[identity], 'cwd': str(tmp_path),
                                   'path': str(home / directory / f'{identity}.jsonl')}}
            if method == 'thread/list':
                if params['archived']:
                    return {'data': [{'id': old, 'parentThreadId': root}], 'nextCursor': None}
                if self.archived and case != 'post-active':
                    return {'data': [], 'nextCursor': None}
                if case == 'changed' and sum(call[0] == 'thread/list' and call[1].get('archived') is False for call in calls) > 2:
                    return {'data': [{'id': child, 'parentThreadId': root}], 'nextCursor': None}
                if params['cursor'] is None:
                    parent = 'foreign' if case == 'ancestry' else root
                    return {'data': [{'id': child, 'parentThreadId': parent}], 'nextCursor': 'next'}
                return {'data': [{'id': grandchild, 'parentThreadId': child}], 'nextCursor': None}
            if method == 'thread/loaded/list':
                return {'data': [child] if case == 'loaded' else []}
            assert method == 'thread/archive'
            if case == 'bad-ack':
                return {'ok': True}
            self.archived = True
            for identity in [root, child, grandchild]:
                await self.events.put({'method': 'thread/archived',
                                       'params': {'threadId': old if case == 'wrong-notice' and identity == root else identity}})
            return {}

        async def close(self):
            calls.append(('close', None))
            if case != 'cleanup':
                self.process = None

    def lease_factory(identity):
        calls.append(('lease', identity))
        if case == 'owned-child' and identity == child:
            raise RuntimeError('child already owned')
        lease = SimpleNamespace(launching=lambda: calls.append(('launching', identity)),
                                bind=lambda pid: calls.append(('bind', identity, pid)),
                                release=lambda: calls.append(('release', identity)))
        leases.append(lease)
        return lease

    def owner_guard(thread, pid):
        calls.append(('guard', thread['id'], pid))
        if case == 'owner-guard' and thread['id'] == child:
            raise RuntimeError('external owner')

    def family_guard(threads):
        calls.append(('family', [thread['id'] for thread in threads]))
        if case == 'family-guard':
            raise RuntimeError('reserved descendant')

    checkpoints = []

    async def run():
        args = dict(confirmed=True, binary='codex', env={'CODEX_HOME': str(home), 'OPENAI_API_KEY': 'never'},
                    rpc_factory=Rpc, lease_factory=lease_factory, ownership_guard=owner_guard,
                    family_guard=family_guard, checkpoint=checkpoints.append)
        if case == 'ok':
            result = await archive_codex_tree(root, tmp_path, **args)
            targets = [{'session_id': identity, 'provider': 'codex', 'cwd': str(tmp_path)}
                       for identity in [root, child, grandchild]]
            assert result == {'session_id': root, 'provider': 'codex', 'cwd': str(tmp_path),
                              'archived': True, 'thread_ids': [root, child, grandchild], 'targets': targets}
            assert checkpoints == [{'session_id': root, 'provider': 'codex', 'cwd': str(tmp_path),
                                    'targets': targets}]
        else:
            with pytest.raises((RuntimeError, TimeoutError)):
                await archive_codex_tree(root, tmp_path, **args)
        methods = [call[0] for call in calls]
        assert not {'thread/resume', 'thread/start', 'turn/start'} & set(methods)
        assert methods.count('thread/archive') == (1 if case in {'ok', 'bad-ack', 'wrong-notice', 'post-active', 'cleanup'} else 0)
        assert methods[-1] == 'release'

    asyncio.run(run())


@pytest.mark.parametrize('case', ['active', 'archived', 'mixed', 'changed', 'owner-guard',
                                  'family-guard', 'loaded', 'cleanup'])
def test_archive_reconciliation_is_read_only_and_leases_the_checkpointed_family(tmp_path, case):
    root, child = str(uuid4()), str(uuid4())
    home = tmp_path / 'codex'
    targets = [{'session_id': identity, 'provider': 'codex', 'cwd': str(tmp_path)}
               for identity in [root, child]]
    calls = []

    class Rpc:
        process = None

        async def start(self, argv, **kwargs):
            calls.append(('start', argv))
            assert 'OPENAI_API_KEY' not in kwargs['env']
            self.process = SimpleNamespace(pid=321)

        async def notify(self, *args): pass

        async def request(self, method, params):
            calls.append((method, params))
            if method == 'initialize':
                return {}
            if method == 'thread/read':
                identity = params['threadId']
                archived = case == 'archived' or (case == 'mixed' and identity == root)
                directory = 'archived_sessions' if archived else 'sessions'
                return {'thread': {'id': identity, 'cwd': str(tmp_path),
                                   'path': str(home / directory / f'{identity}.jsonl')}}
            if method == 'thread/list':
                if params['archived']:
                    return {'data': [], 'nextCursor': None}
                data = [] if case == 'archived' else [{'id': child, 'parentThreadId': root}]
                if case == 'changed':
                    data = []
                return {'data': data, 'nextCursor': None}
            if method == 'thread/loaded/list':
                return {'data': [root] if case == 'loaded' else []}
            raise AssertionError(f'Unexpected mutation: {method}')

        async def close(self):
            calls.append(('close', None))
            if case != 'cleanup':
                self.process = None

    class Lease:
        def __init__(self, identity):
            self.identity = identity
            calls.append(('lease', identity))

        def launching(self): calls.append(('launching', self.identity))
        def bind(self, pid): calls.append(('bind', self.identity, pid))
        def release(self): calls.append(('release', self.identity))

    def guard(thread, pid):
        calls.append(('guard', thread['id'], pid))
        if case == 'owner-guard':
            raise RuntimeError('owned')

    def family(threads):
        calls.append(('family', [thread['id'] for thread in threads]))
        if case == 'family-guard':
            raise RuntimeError('reserved')

    async def run():
        kwargs = dict(binary='codex', env={'CODEX_HOME': str(home), 'OPENAI_API_KEY': 'never'},
                      rpc_factory=Rpc, lease_factory=Lease, ownership_guard=guard, family_guard=family)
        if case in {'active', 'archived'}:
            result = await inspect_codex_archive_tree(root, tmp_path, targets, **kwargs)
            assert result == {'session_id': root, 'provider': 'codex', 'cwd': str(tmp_path),
                              'archived': case == 'archived', 'thread_ids': [root, child], 'targets': targets}
        else:
            with pytest.raises((RuntimeError, ValueError)):
                await inspect_codex_archive_tree(root, tmp_path, targets, **kwargs)
        methods = [call[0] for call in calls]
        assert not {'thread/archive', 'thread/unarchive', 'thread/resume', 'thread/start', 'turn/start'} & set(methods)
        assert methods[-2:] == ['release', 'release']

    asyncio.run(run())
