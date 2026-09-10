"""Opt-in real subscription turn through the accepted-job HTTP bridge."""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import suppress
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def child(root, expect_auth_failure=False):
    from werkzeug.serving import make_server

    from core.coding_job_contract import CodingJobBrief, capture_git_snapshot
    from core.voice_inbox import get_default_voice_inbox
    from core.workspace_host import WorkspaceHost
    from core.workspace_journal import WorkspaceJournal
    from core.workspace_rpc import WorkspaceRpc
    from ui.web import app

    project = root / 'project'
    project.mkdir()
    subprocess.run(['git', 'init', '-q', str(project)], check=True)
    subprocess.run(['git', '-C', str(project), '-c', 'user.name=Serena Proof',
                    '-c', 'user.email=proof@example.test', 'commit', '--allow-empty', '-qm', 'proof baseline'], check=True)

    async def bootstrap():
        rpc = WorkspaceRpc()
        try:
            await rpc.start([shutil.which('codex'), 'app-server', '--stdio'], cwd=project, env=dict(os.environ))
            await rpc.request('initialize', {'clientInfo': {'name': 'serena-native-work-proof', 'version': '1'},
                                             'capabilities': {'experimentalApi': True}})
            await rpc.notify('initialized', {})
            account = await rpc.request('account/read', {'refreshToken': False})
            assert account['account']['type'] == 'chatgpt'
            result = await rpc.request('thread/start', {
                'cwd': str(project), 'sandbox': 'read-only', 'approvalPolicy': 'never',
                'developerInstructions': 'Transport verification only. Do not use tools. Reply only with the exact requested text.',
                'config': {'features.shell_tool': False, 'web_search': 'disabled'},
            })
            sid = result['thread']['id']
            await rpc.request('thread/shellCommand', {'threadId': sid, 'command': 'printf SERENA_WORK_SEED', 'timeoutMs': 5000})
            async with asyncio.timeout(20):
                while True:
                    event = await rpc.events.get()
                    if event.get('method') == 'turn/completed':
                        assert event['params']['turn']['status'] == 'completed'
                        break
            return sid
        finally:
            await rpc.close()

    sid = asyncio.run(bootstrap())
    host = WorkspaceHost(journal=WorkspaceJournal(root / 'workspace.db'),
        resolve=lambda requested: {'session_id': sid, 'provider': 'codex', 'cwd': str(project)}
        if requested == sid else None)
    app.extensions['workspace_host'] = host
    server = make_server('127.0.0.1', 0, app, threaded=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        assert host.attach(sid)['ok']
        owner = host._sessions[sid][0]
        pid = owner.rpc.process.pid
        item, dispatch, view = str(uuid4()), str(uuid4()), str(uuid4())
        prompt = 'Reply exactly SERENA_NATIVE_WORK_PROOF. Do not use tools.'
        store = get_default_voice_inbox()
        brief = CodingJobBrief.create(item_id=item, exact_request=prompt, triggering_request=prompt,
            project_root=project, initial_git=capture_git_snapshot(project, item_id=item, label='baseline'),
            work_route={'mode': 'reuse', 'session_id': sid, 'project_root': str(project),
                        'bridge_port': server.server_port})
        store.enqueue_accepted(brief.to_dict(), call_id='isolated-native-work', turn_id=item)
        body = {'target_sid': sid, 'item_id': item, 'dispatch_id': dispatch, 'prompt': prompt, 'timeout': 120}
        outcomes = []
        for sequence in (1, 2):
            host.note_view_context(sid, {'view_id': view, 'sequence': sequence,
                                        'visible': True, 'focused': True, 'draft': False})
            request = Request(f'http://127.0.0.1:{server.server_port}/api/codex-work-bridge',
                method='POST', headers={'Content-Type': 'application/json'}, data=json.dumps(body).encode())
            with urlopen(request, timeout=150) as response:
                result = json.load(response)
            if expect_auth_failure:
                assert not result['ok'] and 'refresh token' in result['message'].lower(), result
            else:
                assert result['ok'] and 'SERENA_NATIVE_WORK_PROOF' in result['response'], result
            assert not result['reserved'], result
            outcomes.append(result)
        assert outcomes[0]['start_offset'] == outcomes[1]['start_offset']
        assert outcomes[0]['end_offset'] == outcomes[1]['end_offset']
        assert owner.rpc.process.pid == pid and len(host._sessions) == 1
        assert store.route_record(item)['state'] == ('uncertain' if expect_auth_failure else 'completed')
        commands = [host.journal.command_record(sid, 'work:' + item + ':' + dispatch)]
        assert commands[0]['result']['ok'] and commands[0]['result']['turn_id']
        assert sum(event['event'].get('method') == 'turn/started' for event in host.journal.read(sid)['events']) == 1
        assert not host.journal.has_pending_work(sid) and not host._work_reservations
        assert subprocess.check_output(['git', '-C', str(project), 'status', '--porcelain'], text=True) == ''
        print('PASS: real native authentication failure reported honestly, not as job success; one submitted turn, retry reused original bounds; reservation released; project unchanged'
              if expect_auth_failure else 'PASS: one real ChatGPT-subscription job turn through HTTP/native owner; repeated dispatch reused its original reply and bounds; reservation released; project unchanged')
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)
        host.shutdown()
    assert owner.rpc.process is None or owner.rpc.process.returncode is not None
    print('PASS: isolated HTTP host and native provider reaped; no user session or installed service changed')


def main():
    import psutil
    parser = argparse.ArgumentParser()
    parser.add_argument('--allow-inference', action='store_true', required=True)
    parser.add_argument('--child', type=Path)
    parser.add_argument('--expect-auth-failure', action='store_true')
    args = parser.parse_args()
    if args.child:
        child(args.child, args.expect_auth_failure)
        return
    from core.billing import strip_metered_auth_env

    source = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'auth.json'
    auth = json.loads(source.read_text())
    if auth.get('auth_mode') != 'chatgpt' or not auth.get('tokens'):
        raise RuntimeError('Existing ChatGPT subscription authentication is required')
    with tempfile.TemporaryDirectory(prefix='serena-native-work-proof-') as temporary:
        root = Path(temporary)
        home = root / 'home'
        codex = home / '.codex'
        codex.mkdir(parents=True, mode=0o700)
        with open(codex / 'auth.json', 'x', opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
            # Preserve refresh age so isolation does not force an unnecessary refresh.
            json.dump({'auth_mode': 'chatgpt', 'tokens': auth['tokens'],
                       'last_refresh': auth.get('last_refresh')}, output)
        env = strip_metered_auth_env(dict(os.environ))
        env.update(HOME=str(home), CODEX_HOME=str(codex), CHATS_DATA_DIR=str(root / 'data'),
                   SERENA_VOICE_INBOX_PATH=str(root / 'voice.db'), SERENA_RUNTIME_LEASE_DIR=str(root / 'leases'),
                   SERENA_STRUCTURED_WORKSPACE='0', SERENA_CALL_RUNTIME='lazy')
        for key in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CLAUDE_CODE_SESSION_ID'):
            env.pop(key, None)
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--allow-inference', '--child', str(root),
                                    *(['--expect-auth-failure'] if args.expect_auth_failure else [])],
                                   cwd=ROOT, env=env)
        try:
            raise SystemExit(process.wait(timeout=240))
        finally:
            if process.poll() is None:
                children = psutil.Process(process.pid).children(recursive=True)
                for child_process in reversed(children):
                    with suppress(psutil.NoSuchProcess):
                        child_process.terminate()
                process.terminate()
                _, remaining = psutil.wait_procs(children, timeout=5)
                for child_process in remaining:
                    with suppress(psutil.NoSuchProcess):
                        child_process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == '__main__':
    main()
