import threading
from pathlib import Path

import pytest
from flask import Flask
from werkzeug.serving import make_server

from ui.workspace_app import install_workspace


@pytest.mark.parametrize('provider', ['claude', 'codex', 'gemini'])
def test_successful_retry_clears_only_the_failed_connection(tmp_path, provider):
    playwright = pytest.importorskip('playwright.sync_api')
    attempts = []

    class Owner:
        state = 'closed'
        active_turn = None

        def __init__(self, session_id, cwd, publish):
            self.publish = publish

        async def open(self):
            self.state = 'ready'
            await self.publish({'method': 'workspace/history', 'params': {'thread': {'id': 'exact', 'turns': []}}})

        async def close(self):
            self.state = 'closed'

        async def list_models(self):
            return {'data': []}

    def resolve(sid):
        attempts.append(sid)
        if len(attempts) == 1:
            raise RuntimeError('An unregistered process prevents attachment')
        return {'session_id': sid, 'provider': provider, 'cwd': str(tmp_path)}

    app = Flask(__name__, static_folder=str(Path(__file__).resolve().parents[1] / 'ui/static'))
    host = install_workspace(app, tmp_path / 'events.db', resolve=resolve,
                             factories={provider: Owner}, describe=lambda sid: {'session_id': sid, 'agent': provider})
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(f'http://127.0.0.1:{server.server_port}/workspace/exact?resume=1')
                retry = page.get_by_role('button', name='Retry connection', exact=True)
                retry.wait_for()
                playwright.expect(page.locator('.aw-error')).to_contain_text('unregistered')
                page.locator('textarea').fill('Preserve this unsent draft')
                retry.click()
                playwright.expect(page.locator('#workspace-connect')).to_be_hidden()
                playwright.expect(page.locator('.aw-error')).to_be_hidden()
                playwright.expect(page.locator('.aw-state')).to_have_text('ready')
                assert page.locator('textarea').input_value() == 'Preserve this unsent draft'
                assert attempts == ['exact', 'exact']
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        host.shutdown()
