import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.workspace_archive import restore_codex_archive


@pytest.mark.parametrize('case', ['ok', 'unconfirmed', 'owned', 'factory', 'foreign', 'wrong-project', 'not-archived', 'lost', 'wrong-result', 'loaded', 'cleanup'])
def test_restore_is_exact_exclusive_and_never_resumes(tmp_path, case):
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
                    rpc_factory=Rpc, lease_factory=lease_factory)
        if case == 'ok':
            result = await restore_codex_archive(sid, tmp_path, **args)
            assert result == {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path), 'archived': False}
        else:
            with pytest.raises((ValueError, RuntimeError, TimeoutError)):
                await restore_codex_archive(sid, tmp_path, **args)
        methods = [method for method, _ in calls]
        assert not {'thread/resume', 'thread/start', 'turn/start'} & set(methods)
        assert methods.count('thread/unarchive') == (0 if case in {'unconfirmed', 'owned', 'factory', 'foreign', 'wrong-project', 'not-archived'} else 1)
        if case == 'unconfirmed':
            assert calls == []
        elif case == 'owned':
            assert methods == ['lease']
        elif case == 'factory':
            assert methods == ['lease', 'release']
        else:
            assert methods[-2:] == ['close', 'release']
    asyncio.run(run())
