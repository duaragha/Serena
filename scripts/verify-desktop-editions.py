"""Exercise two packaged editions together, without using real chats or credentials."""

import argparse
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_json(url, process, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert process.poll() is None, f"process exited: {process.returncode}"
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return json.load(response)
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(url)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stable", type=Path)
    parser.add_argument("dev", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    processes = []
    with tempfile.TemporaryDirectory(prefix="serena-editions-") as temporary:
        home = Path(temporary)
        binary_dir = home / "bin"
        binary_dir.mkdir()
        fake = binary_dir / "muse"
        fake.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
sid = '11111111-2222-4333-8444-555555555555'
Path(os.environ['HOME'], 'muse-proof.pid').write_text(str(os.getpid()))
def emit(value): print(json.dumps(value), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if 'id' not in request: continue
    result = {}
    if method == 'initialize': result = {'schema': {'version': 1}}
    if method in ('session/start', 'session/resume'):
        result = {'session': {'sessionId': sid, 'modelId': 'muse-proof', 'activeTurnId': None},
                  'history': {'mode': 'inline', 'items': [{'itemId': 'welcome', 'kind': 'agentMessage', 'revision': 1,
                  'turnId': 'welcome', 'status': 'completed', 'text': 'Development runtime ready'}]}}
    if method == 'turn/start': result = {'turnId': 'proof-turn'}
    emit({'jsonrpc': '2.0', 'id': request['id'], 'result': result})
    if method == 'turn/start':
        emit({'method': 'turn/started', 'params': {'sessionId': sid, 'turnId': 'proof-turn'}})
        emit({'method': 'item/completed', 'params': {'sessionId': sid, 'item': {
            'itemId': 'answer', 'kind': 'agentMessage', 'turnId': 'proof-turn',
            'status': 'completed', 'revision': 1, 'text': 'Development request completed'}}})
        emit({'method': 'turn/completed', 'params': {'sessionId': sid, 'turnId': 'proof-turn', 'terminal': 'completed'}})
''')
        fake.chmod(0o755)
        cli = binary_dir / "claude"
        cli.write_text(f"#!{sys.executable}\n" + '''
import os, sys
from pathlib import Path
Path(os.environ['HOME'], 'cli-proof.pid').write_text(str(os.getpid()))
print('Native CLI terminal ready', flush=True)
for line in sys.stdin:
    print('CLI received: ' + line.strip(), flush=True)
''')
        cli.chmod(0o755)
        env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
               "PATH": str(binary_dir) + os.pathsep + os.environ["PATH"],
               "SERENA_CALL_RUNTIME": "lazy", "KNOWLEDGE_DIR": str(home / "knowledge"),
               "MEMORY_DIR": str(home / "memory"), "SERENA_PERSONA_FILE": str(home / "Persona.md")}
        for key in ("APPIMAGE", "APPDIR", "LD_LIBRARY_PATH", "LD_LIBRARY_PATH_ORIG", "ELECTRON_RUN_AS_NODE", "CLAUDE_DIR", "CODEX_HOME"):
            env.pop(key, None)
        proof = {}
        try:
            with sync_playwright() as playwright:
                pages = {}
                for edition, image in (("stable", args.stable), ("dev", args.dev)):
                    debug_port = free_port()
                    log = (args.output / f"{edition}.log").open("w")
                    process = subprocess.Popen([str(image.resolve()), "--no-sandbox", "--disable-gpu",
                        f"--remote-debugging-port={debug_port}"], env=env, stdout=log,
                        stderr=subprocess.STDOUT, start_new_session=True)
                    processes.append((process, log))
                    wait_json(f"http://127.0.0.1:{debug_port}/json/version", process)
                    browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}", timeout=15000)
                    context = browser.contexts[0]
                    page = context.pages[0] if context.pages else context.wait_for_event('page', timeout=90000)
                    page.on('crash', lambda *unused, edition=edition: print(f'{edition}: renderer crashed', flush=True))
                    page.on('pageerror', lambda error, edition=edition: print(f'{edition}: {error}', flush=True))
                    page.wait_for_function("window.SERENA && typeof newChatInline === 'function'")
                    page.wait_for_load_state('domcontentloaded')
                    assert page.evaluate("window.SERENA.structuredWorkspace") is (edition == "dev")
                    health = page.request.get(page.url + "api/health").json()
                    assert health["desktop"]["channel"] == edition
                    proof[edition] = {"health": health, "url": page.url}
                    pages[edition] = page
                    page.evaluate("""([cwd, agent]) => {
                      const original = showPrompt;
                      showPrompt = async () => ({value: 'Edition verification', agent, agents: [agent]});
                      return newChatInline(cwd, {agent}).finally(() => {showPrompt = original;});
                    }""", [str(home), "claude" if edition == "stable" else "muse"])
                    if edition == "stable":
                        page.wait_for_function("[...termSessions.values()].some(s => s.term?.buffer.active.getLine(0)?.translateToString().includes('Native CLI'))")
                        assert page.locator('#termMounts iframe').count() == 0
                        page.evaluate('termSessions.get(activeTermSid).term.focus()')
                        page.keyboard.type('edition-proof')
                        page.keyboard.press('Enter')
                        page.wait_for_function("""() => {
                          const b = termSessions.get(activeTermSid).term.buffer.active;
                          return Array.from({length: b.length}, (_, i) => b.getLine(i).translateToString())
                            .some(line => line.includes('CLI received: edition-proof'));
                        }""")
                    else:
                        frame = page.frame_locator('#termMounts iframe')
                        frame.get_by_text('Development runtime ready', exact=True).wait_for()
                        print('Dev composers:', frame.locator('textarea').evaluate_all(
                            "nodes => nodes.map(n => ({label:n.getAttribute('aria-label'), disabled:n.disabled, hidden:n.hidden}))"), flush=True)
                        page.screenshot(path=str(args.output / 'dev-ready.png'))
                        frame.get_by_role('textbox', name='Message Muse', exact=True).fill('edition proof')
                        frame.get_by_role('textbox', name='Message Muse', exact=True).press('Enter')
                        frame.get_by_text('Development request completed', exact=True).wait_for()
                    page.screenshot(path=str(args.output / f'{edition}-desktop.png'))
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.screenshot(path=str(args.output / f'{edition}-mobile.png'))
                    page.set_viewport_size({"width": 1400, "height": 900})
                assert proof['stable']['health']['pid'] != proof['dev']['health']['pid']
                sid = pages['stable'].evaluate('activeTermSid')
                response = pages['dev'].request.post(proof['dev']['url'] + 'api/spawn-terminal', data={
                    'client_session_id': sid, 'cwd': str(home), 'agent': 'claude'})
                assert response.status >= 400
                assert 'owner' in response.json()['error'].lower()
                cli_pid = int((home / 'cli-proof.pid').read_text())
                muse_pid = int((home / 'muse-proof.pid').read_text())
                # Quit Dev normally. Stable's backend and its live PTY must survive.
                processes[1][0].send_signal(signal.SIGTERM)
                processes[1][0].wait(timeout=35)
                assert processes[0][0].poll() is None
                os.kill(cli_pid, 0)
                assert pages['stable'].request.get(proof['stable']['url'] + 'api/health').json()['pid'] == proof['stable']['health']['pid']
                try:
                    os.kill(muse_pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise AssertionError('Dev left its provider alive after quit')
                proof['coexistence'] = 'Dev quit left stable backend and CLI alive; Dev provider exited'
                (args.output / 'proof.json').write_text(json.dumps(proof, indent=2) + '\n')
                print(json.dumps(proof, indent=2))
        finally:
            for logfile in home.glob('.config/serena-desktop-*/logs/backend.log'):
                shutil.copy2(logfile, args.output / (logfile.parents[1].name + '-backend.log'))
            for process, log in reversed(processes):
                print(f'cleanup: process {process.pid}, status {process.poll()}', flush=True)
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    try:
                        process.wait(timeout=35)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                log.close()


if __name__ == '__main__':
    main()
