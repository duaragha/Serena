import shutil
import subprocess

import pytest

from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal


@pytest.mark.parametrize('provider', ['claude', 'codex', 'gemini'])
@pytest.mark.parametrize('cleanup', [True, False])
def test_explicit_close_stops_active_owner_once_and_requires_confirmed_cleanup(tmp_path, provider, cleanup):
    calls = []
    class Owner:
        def __init__(self, **kwargs):
            self.state, self.active_turn = 'ready', None
        async def open(self):
            self.state, self.active_turn = 'running', 'busy-turn'
        async def close(self):
            calls.append('close')
            self.state, self.active_turn = 'closed', None
        def can_retry_attachment(self):
            return cleanup
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'events.db'),
        resolve=lambda sid: dict(session_id=sid, provider=provider, cwd=str(tmp_path)), factories={provider: Owner})
    try:
        host.attach('exact')
        rejected = host.command('exact', 'invalid', 'close_session', {})
        assert not rejected['ok'] and not calls
        result = host.command('exact', 'close-once', 'close_session', {'confirmed': True})
        assert result['ok'] is cleanup
        assert host.command('exact', 'close-once', 'close_session', {'confirmed': True}) == result
        assert calls == ['close']
        events = host.events('exact')['events']
        completed = [e['event']['params']['turn'] for e in events if e['event']['method'] == 'turn/completed']
        if cleanup:
            assert completed == [{'id': 'busy-turn', 'status': 'interrupted'}]
            assert result['result'] == {'closed': True, 'session_id': 'exact'}
        else:
            assert not completed and 'unconfirmed' in result['error']
    finally:
        host.shutdown()


def test_close_shortcut_targets_focused_linked_group_not_selected_sidebar():
    if not shutil.which('node'):
        pytest.skip('node is required')
    from ui.web import HTML
    start = HTML.index('window.__gtkShortcut = function(')
    dispatcher = HTML[start:HTML.index('\n};', start) + 3]
    script = """
const assert=require('node:assert/strict');
const window={};
const currentSessionId='unrelated';let activeTermSid='claude';
const sessionSource=[{session_id:'claude',group:'pair'},{session_id:'codex',group:'pair'},
  {session_id:'unrelated',group:'other'}];
const _findClientSession=sid=>sessionSource.find(row=>row.session_id===sid);
const _activeTerms=new Set(['claude','unrelated']);
const termSessions=new Map([['claude',{}],['codex',{}],['unrelated',{}]]);
const closed=[];const closeActiveTerminal=sid=>closed.push(sid);
""" + dispatcher + """
window.__gtkShortcut('close-terminal');
assert.deepEqual(closed,['claude','codex']);
closed.length=0;activeTermSid='unrelated';
window.__gtkShortcut('close-terminal','codex');
assert.deepEqual(closed,['claude','codex']);
"""
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
