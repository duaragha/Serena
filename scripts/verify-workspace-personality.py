"""Verify native personality persistence through host replacement, without inference."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_codex import CodexWorkspace
from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from core.workspace_lease import SessionLease


def main():
    with tempfile.TemporaryDirectory(prefix='workspace-personality-') as directory:
        root = Path(directory)
        (root / '.codex').mkdir()
        env = {'PATH': os.environ['PATH'], 'HOME': directory, 'CODEX_HOME': str(root / '.codex'),
               'OPENAI_BASE_URL': 'http://127.0.0.1:9/v1'}
        binary = shutil.which('codex')
        processes = []
        async def seed():
            async def publish(event):
                pass
            async def checkpoint(target):
                pass
            owner = CodexWorkspace(session_id='new:' + str(uuid4()), cwd=root, publish=publish,
                                   lease_factory=lambda sid: SessionLease(sid, directory=root / 'leases'))
            try:
                await owner.create(binary=binary, env=env, checkpoint=checkpoint)
                processes.append(owner.rpc.process)
                models = (await owner.list_models())['data']
                model = next(item['model'] for item in models if item.get('supportsPersonality') is True)
                (root / '.codex' / 'config.toml').write_text('model = ' + json.dumps(model) + '\n')
                await owner.rpc.request('thread/settings/update', {'threadId':owner.session_id,'model':model})
                await owner.shell_command('echo personality-proof', True)
                async with asyncio.timeout(10):
                    while owner.state != 'ready':
                        await asyncio.sleep(.02)
                return owner.session_id
            finally:
                await owner.close()
        sid = asyncio.run(seed())
        class NativeOwner(CodexWorkspace):
            def __init__(self, **kwargs):
                super().__init__(**kwargs, lease_factory=lambda identity: SessionLease(identity, directory=root / 'leases'))
            async def open(self):
                result = await super().open(binary=binary, env=env)
                processes.append(self.rpc.process)
                return result
        journal = WorkspaceJournal(root / 'events.db')
        for index in range(2):
            host = WorkspaceHost(journal=journal, factories={'codex': NativeOwner},
                                 resolve=lambda identity: {'session_id': identity, 'provider': 'codex', 'cwd': directory})
            try:
                after = host.events(sid)['cursor']
                attached = host.attach(sid)
                assert attached['ok'], attached
                owner = host._sessions[sid][0]
                model = owner.settings['model']
                if index == 0:
                    catalog = host.command(sid, 'inspect', 'personality', {})
                    assert catalog['result']['options'], {
                        'currentModel': model,
                        'models': [{key: item.get(key) for key in ('model', 'supportsPersonality')}
                                   for item in owner.model_catalog]}
                    result = host.command(sid, 'choose', 'set_personality', {'value': 'friendly'})
                    assert result['ok'], result
                    assert host.command(sid, 'choose', 'set_personality', {'value': 'friendly'}) == result
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    events = host.events(sid, after=after)['events']
                    if any(row['event'].get('method') == 'thread/settings/updated'
                           and row['event']['params'].get('threadSettings', {}).get('personality') == 'friendly'
                           for row in events):
                        break
                    time.sleep(.02)
                else:
                    raise AssertionError('No native personality confirmation')
                assert owner.settings['personality'] == 'friendly'
                assert owner.settings['model'] == model and owner.state == 'ready'
                assert len(owner.thread.get('turns', [])) == 1
            finally:
                host.shutdown()
        assert len(processes) == 3 and all(process.returncode is not None for process in processes)
        print(json.dumps({'exactSession': sid, 'nativePersonality': 'friendly', 'restoredAfterReplacement': True,
                          'inference': False, 'processesReaped': 3}))
    print('PASS: disposable authentication-free profile removed')


if __name__ == '__main__':
    main()
