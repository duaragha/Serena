"""Exercise a paused native goal in disposable storage, without inference."""
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
    with tempfile.TemporaryDirectory(prefix='workspace-goal-proof-') as directory:
        root = Path(directory)
        (root / '.codex').mkdir()
        env = {'PATH':os.environ['PATH'],'HOME':directory,'CODEX_HOME':str(root / '.codex'),
               'OPENAI_BASE_URL':'http://127.0.0.1:9/v1'}
        async def publish(event):
            pass
        async def checkpoint(target):
            pass
        def make(sid):
            return CodexWorkspace(session_id=sid,cwd=root,publish=publish,
                                  lease_factory=lambda identity:SessionLease(identity,directory=root / 'leases'))
        owner = make('new:' + str(uuid4()))
        processes = []
        try:
            await owner.create(binary=shutil.which('codex'),env=env,checkpoint=checkpoint)
            processes.append(owner.rpc.process)
            await owner.shell_command('echo goal-proof',True)
            async with asyncio.timeout(10):
                while owner.state != 'ready':
                    await asyncio.sleep(.02)
            assert await owner.get_goal() == {'goal':None}
            result = await owner.update_goal({'objective':'Paused disposable proof only','status':'paused','tokenBudget':1000},None,True)
            goal = result['goal']
            sid = owner.session_id
            await owner.close()
            owner = make(sid)
            await owner.open(binary=shutil.which('codex'),env=env)
            processes.append(owner.rpc.process)
            assert (await owner.get_goal())['goal'] == goal
            updated = await owner.update_goal({'tokenBudget':None},goal,True)
            assert updated['goal']['tokenBudget'] is None
            try:
                await owner.clear_goal(goal,True)
                raise AssertionError('Stale goal accepted')
            except Exception as error:
                assert 'Goal changed' in str(error)
            assert await owner.clear_goal(updated['goal'],True) == {'goal':None}
            assert owner.state == 'ready' and len(owner.thread['turns']) == 1
            print(json.dumps({'session':sid,'nativePersistence':True,'staleClearRejected':True,
                              'budgetRemoved':True,'goalCleared':True,'inference':False}))
        finally:
            await owner.close()
        assert all(process.returncode is not None for process in processes)
    print('PASS: native owners reaped and disposable profile removed')


if __name__ == '__main__':
    asyncio.run(main())
