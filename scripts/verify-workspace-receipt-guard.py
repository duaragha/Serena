"""Prove corrupt browser receipts block native work admission without inference."""

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from core.billing import strip_metered_auth_env
from core.workspace_codex import CodexWorkspace
from core.workspace_lease import SessionLease
from ui.workspace_app import install_workspace


def main():
    binary = shutil.which('codex')
    assert binary, 'Native Codex is required'
    with tempfile.TemporaryDirectory(prefix='workspace-receipt-proof-') as directory:
        root = Path(directory)
        (root / '.codex').mkdir()
        env = strip_metered_auth_env(dict(os.environ))
        env.update(HOME=directory, USERPROFILE=directory, CODEX_HOME=str(root / '.codex'))
        processes = []

        class LocalOwner(CodexWorkspace):
            def __init__(self, **kwargs):
                super().__init__(**kwargs, lease_factory=lambda sid: SessionLease(sid, directory=root / 'leases'))

            async def open(self):
                result = await super().open(binary=binary, env=env)
                processes.append(self.rpc.process)
                return result

        async def seed():
            async def publish(event):
                pass
            async def checkpoint(identity):
                (root / 'identity.json').write_text(json.dumps(identity))
            owner = LocalOwner(session_id='new:' + str(uuid4()), cwd=root, publish=publish)
            try:
                await owner.create(binary=binary, env=env, checkpoint=checkpoint)
                processes.append(owner.rpc.process)
                await owner.shell_command('echo receipt-proof', True)
                async with asyncio.timeout(10):
                    while owner.state != 'ready':
                        await asyncio.sleep(.02)
                return owner.session_id
            finally:
                await owner.close()

        sid = asyncio.run(seed())
        assert processes[0].returncode is not None
        app = Flask(__name__, static_folder=str(Path(__file__).resolve().parents[1] / 'ui/static'))
        host = install_workspace(app, root / 'journal.db', factories={'codex': LocalOwner},
                                 resolve=lambda identity: {'session_id': identity, 'provider': 'codex', 'cwd': str(root)},
                                 describe=lambda identity: {'session_id': identity, 'agent': 'codex'})
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert host.attach(sid)['ok']
            owner = host._sessions[sid][0]
            pid = owner.rpc.process.pid
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel='msedge')
                try:
                    context = browser.new_context()
                    page = context.new_page()
                    errors, commands = [], []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('request', lambda request: commands.append(request.url) if request.url.endswith(('/commands', '/uploads')) else None)
                    key = f'serena-workspace-pending:{sid}'
                    page.add_init_script(f'sessionStorage.setItem({json.dumps(key)}, "{{")')
                    page.goto(f'http://127.0.0.1:{server.server_port}/workspace/{sid}')
                    page.get_by_role('textbox', name='Message Codex').wait_for()
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        snapshot = host.runtime_context_snapshot()['runtimes']
                        if snapshot and snapshot[0]['draft_known'] and snapshot[0]['draft']:
                            break
                        page.wait_for_timeout(50)
                    else:
                        raise AssertionError('Corrupt receipt was not reported as unresolved work')
                    result = host.reserve_work(sid, str(uuid4()))
                    assert result == {'ok': False, 'message': 'Native session has an unsent draft'}, result
                    assert page.evaluate('(key)=>sessionStorage.getItem(key)', key) == '{'
                    assert owner.state == 'ready' and owner.rpc.process.pid == pid
                    assert not commands and not errors
                    page.close()
                    context.close()
                    healthy = browser.new_context()
                    page = healthy.new_page()
                    page.goto(f'http://127.0.0.1:{server.server_port}/workspace/{sid}')
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        snapshot = host.runtime_context_snapshot()['runtimes']
                        if snapshot and snapshot[0]['draft_known'] and not snapshot[0]['draft']:
                            break
                        page.wait_for_timeout(50)
                    else:
                        raise AssertionError('Clean replacement view did not retire the closed corrupt view')
                    item = str(uuid4())
                    assert host.reserve_work(sid, item)['ok']
                    assert host.release_work(sid, item)
                    assert owner.state == 'ready' and owner.rpc.process.pid == pid and len(processes) == 2
                    healthy.close()
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
            host.shutdown()
        assert not thread.is_alive() and all(process.returncode is not None for process in processes)
    assert not root.exists()
    print(json.dumps({'ok': True, 'corruptReceiptBlockedNativeWorkAdmission': True,
                      'savedCorruptDataPreserved': True, 'noBrowserCodingCommands': True,
                      'cleanReplacementViewAdmittedReservation': True, 'sameOwnerPreserved': True,
                      'inference': False, 'printOnlyShellCommands': 1,
                      'childrenReaped': True, 'temporaryProfileRemoved': True}))


if __name__ == '__main__':
    main()
