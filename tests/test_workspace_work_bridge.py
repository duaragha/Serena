"""Accepted-job HTTP routing through the exact native owner and real journal."""

import json

import pytest

from core import codex_bridge
from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal


@pytest.mark.parametrize('uncertain', [False, True])
def test_native_http_dispatch_never_falls_back_or_repeats(tmp_path, monkeypatch, uncertain):
    from ui import web

    path = tmp_path / 'rollout.jsonl'
    path.write_text('')
    submitted = []
    states = []

    class Owner:
        def __init__(self, *, session_id, cwd, publish):
            self.session_id, self.cwd = session_id, cwd
            self.state, self.active_turn = 'closed', None
        async def open(self):
            self.state = 'ready'
        async def close(self):
            self.state = 'closed'
        async def list_background_tasks(self):
            return {'data': []}
        async def submit(self, inputs):
            submitted.append(inputs)
            if uncertain:
                raise TimeoutError('acknowledgement lost')
            records = [
                {'type': 'user_message', 'message': inputs[0]['text']},
                {'type': 'agent_message', 'message': 'finished native work'},
                {'type': 'task_complete'},
            ]
            with path.open('a') as output:
                for payload in records:
                    output.write(json.dumps({'type': 'event_msg', 'payload': payload}) + '\n')
            return {'turn': {'id': 'native-turn'}}

    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'journal.db'),
        resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
        factories={'codex': Owner})
    monkeypatch.setitem(web.app.extensions, 'workspace_host', host)
    monkeypatch.setattr(codex_bridge, 'find_codex_jsonl', lambda sid: path)
    monkeypatch.setattr(codex_bridge, '_unsafe_work_metadata', lambda sid: None)
    monkeypatch.setattr(codex_bridge, '_mark_route_dispatch', lambda item, state, **fields: states.append((state, fields)))
    monkeypatch.setattr(codex_bridge, 'call_codex_work_via_bridge', lambda *args, **kwargs: pytest.fail('PTY fallback'))
    monkeypatch.setattr(codex_bridge, 'interrupt_codex_work', lambda *args, **kwargs: pytest.fail('PTY interrupt'))
    item = '11111111-1111-4111-8111-111111111111'
    dispatch = '22222222-2222-4222-8222-222222222222'
    body = {'target_sid': 'exact', 'item_id': item, 'dispatch_id': dispatch, 'prompt': 'do accepted work', 'timeout': 1}
    try:
        host.attach('exact')
        host.note_view_context('exact', {'view_id': item, 'sequence': 1,
                                        'focused': True, 'visible': True, 'draft': False})
        client = web.app.test_client()
        assert client.post('/api/codex-work-bridge', json=body,
                           environ_overrides={'REMOTE_ADDR': '100.100.100.100'}).status_code == 403
        invalid = client.post('/api/codex-work-bridge', json={**body, 'dispatch_id': None}).json
        assert not invalid['ok'] and not submitted
        result = client.post('/api/codex-work-bridge', json=body).json
        repeated = client.post('/api/codex-work-bridge', json=body).json
        assert len(submitted) == 1 and len(host._sessions) == 1
        assert result['committed'] and repeated['committed']
        assert result['start_offset'] == repeated['start_offset'] == 0
        if uncertain:
            assert not result['ok'] and result['reserved']
            assert not repeated['ok'] and repeated['reserved']
            assert all(state == 'uncertain' for state, _ in states)
        else:
            assert result['ok'] and repeated['ok']
            assert result['response'] == repeated['response'] == 'finished native work'
            assert result['end_offset'] == repeated['end_offset'] == 3
            assert not result['reserved'] and not host._work_reservations
            assert [state for state, _ in states] == ['committed', 'completed'] * 2
        interrupted = client.post('/api/codex-work-interrupt', json={'target_sid': 'exact', 'item_id': dispatch})
        assert interrupted.status_code == 409
    finally:
        host.shutdown()
