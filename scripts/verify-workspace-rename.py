"""Prove native Codex rename persistence on a disposable session, without inference."""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.billing import strip_metered_auth_env
from core.workspace_codex import CodexWorkspace
from core.workspace_lease import SessionLease


async def main():
    binary = shutil.which('codex')
    assert binary, 'Native Codex is required'
    with tempfile.TemporaryDirectory(prefix='workspace-rename-proof-') as directory:
        root = Path(directory)
        (root / '.codex').mkdir()
        env = strip_metered_auth_env(dict(os.environ))
        env.update(HOME=directory, USERPROFILE=directory, CODEX_HOME=str(root / '.codex'))
        events, processes = [], []
        async def publish(event):
            events.append(event)
        async def checkpoint(identity):
            (root / 'identity.json').write_text(json.dumps(identity))
        def make(sid):
            return CodexWorkspace(session_id=sid, cwd=root, publish=publish,
                                  lease_factory=lambda identity: SessionLease(identity, directory=root / 'leases'))
        owner = make('new:' + str(uuid4()))
        try:
            await owner.create(checkpoint=checkpoint, binary=binary, env=env)
            processes.append(owner.rpc.process)
            sid = owner.session_id
            await owner.shell_command('echo rename-proof', True)
            async with asyncio.timeout(10):
                while owner.state != 'ready':
                    await asyncio.sleep(0.02)
            before = (owner.state, owner.active_turn, owner.rpc.process.pid)
            result = await owner.rename('Native rename proof')
            assert result == {'session_id': sid, 'name': 'Native rename proof'}
            assert before == (owner.state, owner.active_turn, owner.rpc.process.pid)
            from core import indexer, metadata
            from core.workspace_catalog import list_saved_sessions, register_fork

            os.environ['CODEX_HOME'] = str(root / '.codex')
            indexer.DATA_DIR, indexer.DB_PATH = root / 'data', root / 'data' / 'index.db'
            indexer._schema_ready = False
            metadata.METADATA_DIR, metadata.METADATA_PATH = root / 'metadata', root / 'legacy.json'
            metadata.set_custom_title(sid, 'Previous explicit title')
            registered = register_fork({'session_id': sid, 'provider': 'codex', 'cwd': str(root),
                                        'confirmed_native_name': result['name']})
            assert registered == {'display_title': 'Native rename proof'}
            assert list_saved_sessions('codex')['data'][0]['title'] == 'Native rename proof'
            assert metadata.get_meta(sid)['custom_title'] == 'Native rename proof'
            await owner.close()
            assert processes[-1].returncode is not None
            owner = make(sid)
            resumed = await owner.open(binary=binary, env=env)
            processes.append(owner.rpc.process)
            assert resumed['thread']['id'] == sid and resumed['thread']['name'] == 'Native rename proof'
            assert owner.state == 'ready'
            title_events = [event for event in events if event.get('method') == 'thread/name/updated']
            assert any(event['params'].get('threadId') == sid and event['params'].get('threadName') == 'Native rename proof' for event in title_events)
        finally:
            await owner.close()
        assert all(process.returncode is not None for process in processes)
    assert not root.exists()
    print(json.dumps({'ok': True, 'nativeRenameConfirmed': True, 'sameSessionResumed': True,
                      'namePersistedAcrossProcessReplacement': True, 'priorOwnerReapedBeforeResume': True,
                      'nativeNameEventObserved': True, 'inference': False, 'printOnlyShellCommands': 1,
                      'exactSerenaCatalogTitleConfirmed': True, 'customTitleReplacedExplicitly': True,
                      'childrenReaped': True, 'temporaryProfileRemoved': True}))


if __name__ == '__main__':
    asyncio.run(main())
