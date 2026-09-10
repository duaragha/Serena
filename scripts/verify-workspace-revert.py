"""Observe a real native revert in a disposable Codex session, without inference."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_codex import CodexWorkspace
from core.workspace_lease import SessionLease


async def main():
    with tempfile.TemporaryDirectory(prefix='workspace-revert-proof-') as directory:
        root = Path(directory)
        (root / '.codex').mkdir()
        env = {'PATH': os.environ['PATH'], 'HOME': directory, 'CODEX_HOME': str(root / '.codex'),
               'OPENAI_BASE_URL': 'http://127.0.0.1:9/v1'}
        events = []
        async def publish(event):
            events.append(event)
        async def checkpoint(identity):
            pass
        owner = CodexWorkspace(session_id='new:' + str(uuid4()), cwd=root, publish=publish,
                               lease_factory=lambda sid: SessionLease(sid, directory=root / 'leases'))
        try:
            await owner.create(checkpoint=checkpoint, binary=shutil.which('codex'), env=env)
            process = owner.rpc.process
            for text in ('retained', 'removed'):
                await owner.shell_command(f'echo {text}', True)
                async with asyncio.timeout(15):
                    while owner.state != 'ready':
                        await asyncio.sleep(.02)
            completed = [e['params']['turn']['id'] for e in events if e['method'] == 'turn/completed']
            assert len(completed) == 2
            await owner.rpc.request('thread/revert', {'threadId': owner.session_id, 'beforeTurnId': completed[-1]})
            async with asyncio.timeout(15):
                while not any(e['method'] == 'workspace/history' and e['params'].get('copyUnavailableAfterRevert') for e in events):
                    await asyncio.sleep(.02)
            assert owner.rpc.process is process and owner.state == 'ready'
            assert [t['id'] for t in owner.thread['turns']] == completed[:1]
            assert owner.history_cursor is None
            print(json.dumps({'exactSession': owner.session_id, 'sameOwner': True, 'retainedTurns': 1,
                              'removedTurns': 1, 'copySuppressed': True, 'inference': False}))
        finally:
            await owner.close()
        assert process.returncode is not None
    print('PASS: native owner reaped; disposable profile removed')


if __name__ == '__main__':
    asyncio.run(main())
