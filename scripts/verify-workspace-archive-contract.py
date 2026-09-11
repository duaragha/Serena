"""Probe native archive/restore semantics in an isolated, unsigned profile."""
import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import indexer, metadata
from core.workspace_archive import restore_codex_archive
from core.workspace_catalog import list_saved_sessions, register_fork
from core.workspace_codex import CodexWorkspace
from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from core.workspace_lease import SessionLease
from core.workspace_rpc import WorkspaceRpc
from ui.workspace_web import workspace_blueprint


def browser_restore(root, sid, target, width):
    from playwright.sync_api import expect, sync_playwright
    from werkzeug.serving import WSGIRequestHandler, make_server

    from ui.workspace_app import install_workspace

    repo = Path(__file__).resolve().parents[1]
    app = Flask(__name__, static_folder=str(repo / 'ui/static'))
    host = install_workspace(app, root / 'workspace.db', factories={},
                             resolve=lambda identity: target if identity == sid else None)

    class QuietHandler(WSGIRequestHandler):
        def log_request(self, *args, **kwargs): pass

    server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors, requests = [], []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=os.environ.get('SERENA_PROOF_BROWSER_EXECUTABLE') or shutil.which('microsoft-edge'), headless=True)
            try:
                page = browser.new_page(viewport={'width': width, 'height': 1000})
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: requests.append(request.url))
                page.on('response', lambda response: errors.append(f'{response.status} {response.url}') if response.status >= 400 else None)
                page.goto(f'http://127.0.0.1:{server.server_port}/workspace/{sid}')
                page.locator('.aw-composer textarea').fill('Keep this archive draft')
                button = page.get_by_role('button', name='Open saved conversation', exact=True)
                if not button.is_visible():
                    page.get_by_role('button', name='Session actions', exact=True).click()
                button.click()
                saved = page.get_by_role('dialog', name='Saved conversations', exact=True)
                saved.get_by_role('radio', name='Archived', exact=True).check()
                saved.locator('.aw-command').filter(has_text=sid).click()
                dialog = page.get_by_role('dialog', name='Restore archived conversation', exact=True)
                assert not host._sessions
                assert not any('/restore-archive' in url or url.endswith('/attach') for url in requests)
                shots = repo / 'apps/desktop/build/workspace-proof'
                shots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shots / f'archive-native-confirm-{width}.png'))
                with page.expect_response(lambda response: response.url.endswith('/restore-archive')) as received:
                    dialog.get_by_role('button', name='Confirm restore conversation', exact=True).click()
                response = received.value
                result = response.json()
                request_id = response.request.post_data_json['request_id']
                assert result['ok'], result
                expect(dialog.get_by_role('status')).to_have_text('Conversation restored')
                expect(saved.locator('.aw-command')).to_have_count(0)
                assert not host._sessions
                assert page.locator('.aw-composer textarea').input_value() == 'Keep this archive draft'
                assert page.locator('body').evaluate('el=>el.scrollWidth<=innerWidth')
                page.screenshot(path=str(shots / f'archive-native-restored-{width}.png'))
                with page.expect_navigation():
                    dialog.get_by_role('button', name='Open restored conversation', exact=True).click()
                assert page.url.endswith('/workspace/' + sid)
                expect(page.locator('.aw-composer textarea')).to_have_value('Keep this archive draft')
                assert not host._sessions
                assert not any(url.endswith('/attach') or url.endswith('/commands') for url in requests)
                assert not errors, errors
                print(f'PASS: {width}px browser confirmed exact native restoration over HTTP; separate navigation, retained draft, zero attachment/turn calls or browser errors')
                return result, request_id
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        host.shutdown()
        assert not thread.is_alive()


async def main(browser_width=None):
    with tempfile.TemporaryDirectory(prefix="workspace-archive-contract-") as directory, ExitStack() as patches:
        root = Path(directory)
        home = root / "codex"
        home.mkdir()
        for module, key, value in ((indexer, 'DATA_DIR', root), (indexer, 'DB_PATH', root / 'index.db'),
                                   (indexer, '_schema_ready', False), (indexer, '_INDEX_LOCK_PATH', root / 'index.lock'),
                                   (metadata, 'METADATA_DIR', root / 'metadata'), (metadata, '_migrated', True)):
            patches.enter_context(patch.object(module, key, value))
        patches.enter_context(patch.dict(os.environ, {'CODEX_HOME': str(home)}))
        project = root / "project"
        project.mkdir()
        env = {"PATH": os.environ["PATH"], "HOME": directory, "CODEX_HOME": str(home),
               "OPENAI_BASE_URL": "http://127.0.0.1:9/v1"}
        binary = shutil.which("codex")
        events = []
        async def publish(event):
            events.append(event)
        async def checkpoint(target): pass
        owner = CodexWorkspace(session_id="new:" + str(uuid4()), cwd=project, publish=publish,
                               lease_factory=lambda sid: SessionLease(sid, directory=root / "leases"))
        processes = []
        app = Flask(__name__)
        app.register_blueprint(workspace_blueprint(object(), token='a' * 40))
        client = app.test_client()

        def catalog(archived):
            response = client.get('/api/workspace/proof/sessions?provider=codex&archived=' + str(archived).lower(),
                                  headers={'X-Serena-Workspace-Token': 'a' * 40})
            assert response.status_code == 200, response.json
            return response.json['data']

        try:
            await owner.create(binary=binary, env=env, checkpoint=checkpoint)
            processes.append(owner.rpc.process)
            sid = owner.session_id
            await owner.shell_command("echo archive-contract-original", True)
            async with asyncio.timeout(10):
                while owner.state != "ready":
                    await asyncio.sleep(.02)
            original = list((home / "sessions").rglob(f"*{sid}.jsonl"))
            assert len(original) == 1
            target = {'session_id': sid, 'provider': 'codex', 'cwd': str(project)}
            metadata._save_one(sid, {'custom_title': 'Retained archive title', 'group': 'proof-group', 'done': False})
            register_fork(target)
            saved_meta = metadata.get_meta(sid)
            response = await owner.rpc.request("thread/archive", {"threadId": sid})
            assert response == {}, response
            async with asyncio.timeout(10):
                while not any(event.get("method") == "thread/archived" and event.get("params", {}).get("threadId") == sid
                              for event in events):
                    await asyncio.sleep(.02)
            archived = list((home / "archived_sessions").rglob(f"*{sid}.jsonl"))
            assert len(archived) == 1 and not original[0].exists()
            assert "archive-contract-original" in archived[0].read_text()
            register_fork(target)
            assert not list_saved_sessions('codex')['data']
            assert list_saved_sessions('codex', archived=True)['data'][0]['session_id'] == sid
            assert indexer.get_session(sid)['is_done'] == 0
            assert metadata.get_meta(sid) == saved_meta
            assert not catalog(False)
            assert [row['session_id'] for row in catalog(True)] == [sid]
            assert catalog(True)[0]['title'] == 'Retained archive title'
            print("PASS: native archive confirmed exact ID and moved, rather than deleted, its real transcript")
        finally:
            await owner.close()

        rpc = WorkspaceRpc()
        try:
            await rpc.start([binary, "app-server", "--stdio"], cwd=project, env=env)
            processes.append(rpc.process)
            await rpc.request("initialize", {"clientInfo": {"name": "serena-archive-proof", "version": "1"},
                                              "capabilities": {"experimentalApi": True}})
            await rpc.notify("initialized", {})
            read = await rpc.request("thread/read", {"threadId": sid, "includeTurns": True})
            assert read["thread"]["id"] == sid and "archive-contract-original" in json.dumps(read)
            loaded = await rpc.request("thread/loaded/list", {})
            assert loaded["data"] == [], loaded
            await rpc.close()

            class TrackedRpc(WorkspaceRpc):
                async def start(self, *args, **kwargs):
                    await super().start(*args, **kwargs)
                    processes.append(self.process)

            async def isolated_restore(identity, cwd, *, confirmed):
                return await restore_codex_archive(identity, cwd, confirmed=confirmed, binary=binary, env=env,
                    rpc_factory=TrackedRpc, lease_factory=lambda identity: SessionLease(identity, directory=root / 'leases'))

            journal = WorkspaceJournal(root / 'workspace.db')
            host = WorkspaceHost(journal=journal, resolve=lambda identity: target if identity == sid else None,
                                 factories={}, register_fork=register_fork)
            request_id = str(uuid4())
            with patch('core.workspace_archive.restore_codex_archive', isolated_restore):
                try:
                    if browser_width:
                        result, request_id = await asyncio.to_thread(browser_restore, root, sid, target, browser_width)
                    else:
                        result = await asyncio.to_thread(host.restore_archive, sid, request_id, confirmed=True)
                    assert result['ok'], result
                    assert await asyncio.to_thread(host.restore_archive, sid, request_id, confirmed=True) == result
                    assert not host._sessions
                    assert not journal.has_pending_archive_restore(sid)
                finally:
                    await asyncio.to_thread(host.shutdown)
                recovered = WorkspaceHost(journal=WorkspaceJournal(journal.path), resolve=lambda _: None, factories={})
                try:
                    assert await asyncio.to_thread(recovered.restore_archive, sid, request_id, confirmed=True) == result
                    assert not recovered._sessions
                finally:
                    await asyncio.to_thread(recovered.shutdown)
            restored = result['result']
            assert restored['session_id'] == sid and restored['archived'] is False
            assert not archived[0].exists()
            assert len(list((home / "sessions").rglob(f"*{sid}.jsonl"))) == 1
            register_fork(target)
            assert list_saved_sessions('codex')['data'][0]['session_id'] == sid
            assert not list_saved_sessions('codex', archived=True)['data']
            assert metadata.get_meta(sid) == saved_meta
            assert [row['session_id'] for row in catalog(False)] == [sid]
            assert not catalog(True)
            print("PASS: archived history read and exact restore require no resumed writer or model turn")
        finally:
            await rpc.close()
        assert all(process.returncode is not None for process in processes)
        assert not list(project.iterdir())
        print(json.dumps({"ok": True, "processesReaped": len(processes), "credentialsUsed": False,
                          "inference": False, "archiveNotificationConfirmed": True, "restoreDoesNotResume": True}))
        print('PASS: real archive/restore reindexed exact session; custom title, done and group metadata unchanged')
        print('PASS: authenticated catalog route separates real active/archive rows without a runtime host')
        print('PASS: durable host restore replays its receipt across restart without another native process')
    print("PASS: disposable profile removed; no user sessions or project files changed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-width', type=int, choices=(390, 1600))
    asyncio.run(main(parser.parse_args().browser_width))
