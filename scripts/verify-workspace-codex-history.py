"""Prove native history pagination with print-only shell turns, no inference."""
import asyncio
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_codex import CodexWorkspace
from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from core.workspace_lease import SessionLease
from core.workspace_rpc import WorkspaceRpc


def browser_proof(sid, root, project, env, binary):
    from flask import Flask
    from werkzeug.serving import WSGIRequestHandler, make_server

    from ui.workspace_app import install_workspace

    repo = Path(__file__).resolve().parents[1]
    class NativeOwner(CodexWorkspace):
        async def open(self):
            return await super().open(binary=binary, env=env)
    app = Flask(__name__, static_folder=str(repo / "ui" / "static"))
    host = install_workspace(app, root / "browser.db",
        resolve=lambda requested: {"session_id": sid, "provider": "codex", "cwd": str(project)} if requested == sid else None,
        describe=lambda requested: {"session_id": sid, "agent": "codex"} if requested == sid else None,
        factories={"codex": lambda **kwargs: NativeOwner(**kwargs,
            lease_factory=lambda session: SessionLease(session, directory=root / "leases"))})
    class QuietRequests(WSGIRequestHandler):
        def log_request(self, code="-", size="-"):
            pass
    server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietRequests)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def owners():
            return [owner.rpc.process.pid for owner, _ in host._sessions.values()
                    if owner.rpc.process and owner.rpc.process.returncode is None]
        browser_roundtrip(f"http://127.0.0.1:{server.server_port}", sid, owners, "codex-native")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        host.shutdown()


def browser_roundtrip(base, sid, owners, prefix, verify_forks=False):
    from playwright.sync_api import expect, sync_playwright

    repo = Path(__file__).resolve().parents[1]
    artifacts = repo / "apps" / "desktop" / "build" / "workspace-proof"
    artifacts.mkdir(parents=True, exist_ok=True)
    forks = []
    with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                pid = None
                for label, width in (("desktop", 1440), ("mobile", 390)):
                    page = browser.new_page(viewport={"width": width, "height": 900})
                    errors = []
                    page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                    page.on("console", lambda message, errors=errors: errors.append(message.text) if message.type == "error" else None)
                    page.on("response", lambda response, errors=errors: errors.append(f"HTTP {response.status}: {response.url}") if response.status >= 400 and not response.url.endswith("favicon.ico") else None)
                    page.goto(f"{base}/workspace/{sid}")
                    if pid is None:
                        assert not owners(), "Page load launched an owner"
                    page.get_by_role("button", name="Resume session", exact=True).click()
                    page.get_by_role("button", name="Mention project file", exact=True).click()
                    picker = page.get_by_role("dialog", name="Mention project file")
                    search = picker.get_by_role("searchbox", name="Find project file")
                    search.fill("workspace-mention-proof")
                    search.press("Enter")
                    picker.get_by_role("button", name="workspace-mention-proof.py", exact=True).click()
                    assert "@workspace-mention-proof.py" in page.get_by_role("textbox", name="Message Codex").input_value()
                    composer = page.get_by_role("textbox", name="Message Codex")
                    composer.fill("inspect @workspace-mention")
                    page.get_by_role("option", name="workspace-mention-proof.py", exact=True).wait_for()
                    page.screenshot(path=str(artifacts / f"{prefix}-mentions-{label}.png"))
                    composer.press("Enter")
                    assert composer.input_value() == "inspect @workspace-mention-proof.py "
                    print(f"PASS: {prefix} {label} native project file search inserted a draft mention without submitting a turn")
                    draft = composer.input_value()
                    page.get_by_role("button", name="Commands and skills", exact=True).click()
                    skill_dialog = page.get_by_role("dialog", name="Commands and skills")
                    skill_dialog.get_by_role("searchbox", name="Search commands").fill("workspace-setting-proof")
                    toggle = skill_dialog.get_by_role("checkbox", name="Enable skill workspace-setting-proof", exact=True)
                    expect(toggle).to_be_checked()
                    toggle.click()
                    expect(toggle).not_to_be_checked()
                    expect(skill_dialog.get_by_role("button").filter(has_text="$workspace-setting-proof")).to_be_disabled()
                    page.screenshot(path=str(artifacts / f"{prefix}-skills-{label}.png"))
                    toggle.click()
                    expect(toggle).to_be_checked()
                    expect(skill_dialog.get_by_role("button").filter(has_text="$workspace-setting-proof")).to_be_enabled()
                    skill_dialog.get_by_role("button", name="Close commands").click()
                    assert composer.input_value() == draft
                    print(f"PASS: {prefix} {label} native skill disabled and re-enabled through persistent configuration; draft unchanged")
                    page.get_by_role("button", name="Run shell command", exact=True).wait_for(state="visible")
                    page.get_by_role("button", name="Run shell command", exact=True).click()
                    dialog = page.get_by_role("dialog", name="Run shell command")
                    token = f"SERENA_BROWSER_{label.upper()}"
                    dialog.get_by_role("textbox", name="Shell command").fill(f"printf {token}")
                    dialog.get_by_role("checkbox").check()
                    dialog.get_by_role("button", name="Run command", exact=True).click()
                    page.locator("summary").filter(has_text=token).first.click(timeout=15000)
                    page.get_by_text(token, exact=True).wait_for(timeout=15000)
                    expect(page.locator('.aw-state')).to_have_text(re.compile(r'^(ready|completed)$'))
                    actual = owners()
                    assert len(actual) == 1, actual
                    if pid is None:
                        pid = actual[0]
                    assert actual == [pid]
                    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                    page.screenshot(path=str(artifacts / f"{prefix}-{label}.png"))
                    if label == "desktop":
                        page.get_by_role("button", name="Load earlier messages", exact=True).click()
                        page.locator("summary").filter(has_text="SERENA_HISTORY_000").first.wait_for(state="attached", timeout=10000)
                        if verify_forks:
                            page.get_by_role("button", name="Fork conversation", exact=True).click()
                            dialog = page.get_by_role("dialog", name="Fork conversation")
                            dialog.get_by_role("button", name="Create fork", exact=True).click()
                            dialog.get_by_role("button", name="Open fork", exact=True).wait_for(timeout=10000)
                            fork_id = dialog.locator("code").inner_text()
                            assert fork_id != sid
                            dialog.get_by_role("button", name="Open fork", exact=True).click()
                            page.wait_for_url(f"{base}/workspace/{fork_id}")
                            page.get_by_role("button", name="Resume session", exact=True).wait_for()
                            assert owners() == [pid], "Opening fork view launched another owner"
                            forks.append(fork_id)
                    assert not errors, errors
                    page.close()
                    assert owners() == [pid], "Closing page cancelled owner"
                    print(f"PASS: {prefix} {label} explicitly attached, ran native command and rendered output; same owner survived page close")
            finally:
                browser.close()
    return forks


def frozen_browser_proof(sid, root, project, env, frozen):
    import psutil

    repo = Path(__file__).resolve().parents[1]
    env = {**env, "CHATS_DATA_DIR": str(root / "frozen-data"), "SERENA_STRUCTURED_WORKSPACE": "1",
           "SERENA_CALL_RUNTIME": "lazy", "SERENA_RUNTIME_LEASE_DIR": str(root / "frozen-leases"),
           "DBUS_SESSION_BUS_ADDRESS": f"unix:path={root}/unavailable-bus", "XDG_RUNTIME_DIR": str(root / "xdg")}
    Path(env["XDG_RUNTIME_DIR"]).mkdir()
    seed = subprocess.run([sys.executable, "-c", """
import sys
from pathlib import Path
from core.workspace_catalog import register_fork
from core.codex_scanner import parse_codex_metadata
import os
paths=list((Path(os.environ['CODEX_HOME'])/'sessions').rglob('*'+sys.argv[1]+'.jsonl'))
assert len(paths)==1
metadata=parse_codex_metadata(paths[0])
assert metadata.session_id==sys.argv[1]
register_fork({'session_id':metadata.session_id, 'provider':'codex', 'cwd':metadata.cwd})
""", sid], env={**env, "PYTHONPATH": str(repo)}, cwd=project, text=True, capture_output=True)
    assert seed.returncode == 0, seed.stderr
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with (root / "frozen.log").open("w+") as log:
        process = subprocess.Popen([str(Path(frozen).resolve()), "--host", "127.0.0.1", "--port", str(port)],
                                   cwd=project, env=env, stdout=log, stderr=log, start_new_session=True)
        children = []
        try:
            deadline = time.monotonic() + 30
            while True:
                assert process.poll() is None, "Frozen sidecar exited before readiness"
                try:
                    with urlopen(base + f"/workspace/{sid}", timeout=1) as response:
                        assert response.status == 200
                    break
                except (URLError, TimeoutError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Frozen workspace did not become ready") from None
                    time.sleep(0.1)
            def owners():
                return [child.pid for child in psutil.Process(process.pid).children(recursive=True)
                        if child.name() == "codex" and "app-server" in child.cmdline()]
            forks = browser_roundtrip(base, sid, owners, "codex-frozen", verify_forks=True)
            for fork_id in forks:
                metadata_path = Path(env["HOME"]) / ".claude" / "projects" / ".chats-meta" / f"{fork_id}.json"
                assert json.loads(metadata_path.read_text())["resident_work"] is True
            assert forks
            print("PASS: frozen native fork created/indexed through UI, persisted scanner ownership and opened without a second owner")
            if os.environ.get("SERENA_PROOF_ELECTRON"):
                before = owners()
                proof = subprocess.Popen(["node", str(repo / "scripts" / "verify-workspace-electron.cjs"),
                    os.environ["SERENA_PROOF_ELECTRON"], os.environ["SERENA_PROOF_PLAYWRIGHT"],
                    str(repo / "apps" / "desktop"), base, sid, str(repo / "apps" / "desktop" / "build" / "workspace-proof"),
                    os.environ["SERENA_PROOF_XVFB"]], env=env, cwd=repo, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                try:
                    out, err = proof.communicate(timeout=90)
                finally:
                    with suppress(ProcessLookupError):
                        os.killpg(proof.pid, signal.SIGKILL)
                    proof.wait(timeout=5)
                print(out)
                assert proof.returncode == 0, err
                assert owners() == before, "Closing Electron changed the native owner"
                print("PASS: closing the real Electron shell preserved the shared isolated native owner")
        except BaseException:
            log.seek(0)
            print(log.read()[-5000:], file=sys.stderr)
            raise
        finally:
            if process.poll() is None:
                children = psutil.Process(process.pid).children(recursive=True)
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            _, alive = psutil.wait_procs(children, timeout=5)
            for child in alive:
                child.kill()
            _, alive = psutil.wait_procs(alive, timeout=5)
            assert not alive, "Frozen proof leaked owned children"


async def main():
    binary = shutil.which("codex")
    if not binary:
        raise RuntimeError("Native Codex unavailable")
    with tempfile.TemporaryDirectory(prefix="serena-history-proof-") as temporary:
        root = Path(temporary)
        home, project = root / "home", root / "project"
        (home / ".codex").mkdir(parents=True)
        skill = home / ".codex" / "skills" / "workspace-setting-proof" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: workspace-setting-proof\ndescription: Isolated skill configuration proof\n---\nNo model invocation is needed.\n")
        project.mkdir()
        mention_fixture = project / "workspace-mention-proof.py"
        mention_fixture.write_text("# Isolated native file-search fixture\n")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home),
               "CODEX_HOME": str(home / ".codex"), "XDG_CONFIG_HOME": str(home / ".config"),
               "OPENAI_BASE_URL": "http://127.0.0.1:9/v1"}
        rpc, owner = WorkspaceRpc(), None
        try:
            await rpc.start([binary, "app-server", "--stdio"], cwd=project, env=env)
            await rpc.request("initialize", {"clientInfo": {"name": "serena-history-proof", "version": "1"},
                                             "capabilities": {"experimentalApi": True}})
            await rpc.notify("initialized", {})
            sid = (await rpc.request("thread/start", {"cwd": str(project)}))["thread"]["id"]
            for number in range(51):
                await rpc.request("thread/shellCommand", {"threadId": sid,
                    "command": f"printf SERENA_HISTORY_{number:03d}", "timeoutMs": 5000})
                async with asyncio.timeout(15):
                    while True:
                        event = await rpc.events.get()
                        if event.get("method") == "turn/completed":
                            assert event["params"]["turn"]["status"] == "completed", event
                            break
            print("PASS: 51 native print-only turns persisted without inference")
            await rpc.close()
            events = []
            completed = asyncio.Event()
            async def publish(event):
                events.append(event)
                if event.get("method") == "turn/completed":
                    completed.set()
            owner = CodexWorkspace(session_id=sid, cwd=project, publish=publish,
                                   lease_factory=lambda session: SessionLease(session, directory=root / "leases"))
            history = await owner.open(binary=binary, env=env)
            assert len(history["thread"]["turns"]) == 50, len(history["thread"]["turns"])
            assert "SERENA_HISTORY_050" in json.dumps(history)
            assert "SERENA_HISTORY_000" not in json.dumps(history)
            assert owner.history_cursor
            host = WorkspaceHost(journal=WorkspaceJournal(root / "receipts.db"), resolve=None)
            host._sessions[sid] = (owner, "codex")
            refusal = await host._command(sid, "stale-read", "load_earlier", {"cursor": "not-the-current-cursor"})
            assert not refusal["ok"] and refusal["retryable"]
            receipt = await host._command(sid, "valid-read", "load_earlier", {"cursor": owner.history_cursor})
            assert receipt["ok"], receipt
            page = receipt["result"]
            assert len(page["turns"]) == 1 and page["historyCursor"] is None, page
            assert "SERENA_HISTORY_000" in json.dumps(page)
            assert events[-1]["method"] == "workspace/historyPage"
            assert owner.state == "ready" and owner.session_id == sid
            print("PASS: native resumed history loads 50 recent turns, then exact oldest full turn on demand")
            print("PASS: host rejects stale history with retryable receipt and routes valid read to unchanged native owner")
            await owner.close()
            reopened = await owner.open(binary=binary, env=env)
            assert reopened["thread"]["id"] == sid
            assert len(reopened["thread"]["turns"]) == 50
            page = await owner.load_earlier(owner.history_cursor)
            assert len(page["turns"]) == 1 and page["historyCursor"] is None
            assert "SERENA_HISTORY_000" in json.dumps(page)
            print("PASS: same adapter reopened exact persisted session and loaded older history without stale cursor state")
            completed.clear()
            start = len(events)
            payload = {"command": "printf SERENA_EXPLICIT_SHELL", "confirmed": True}
            shell = await host._command(sid, "explicit-shell", "shell_command", payload)
            assert shell["ok"], shell
            await asyncio.wait_for(completed.wait(), 10)
            assert await host._command(sid, "explicit-shell", "shell_command", payload) == shell
            assert owner.state == "ready"
            results = [e["params"]["item"] for e in events[start:] if e.get("method") == "item/completed"]
            assert any("SERENA_EXPLICIT_SHELL" in json.dumps(item) and item.get("exitCode") == 0 for item in results), results
            assert sum(e.get("method") == "turn/completed" for e in events[start:]) == 1
            print("PASS: explicit host shell command produced native output and exit 0 exactly once; repeated receipt did not rerun")
        finally:
            if owner:
                await owner.close()
            await rpc.close()
        if len(sys.argv) > 1:
            await asyncio.to_thread(frozen_browser_proof, sid, root, project, env, sys.argv[1])
        else:
            await asyncio.to_thread(browser_proof, sid, root, project, env, binary)
        mention_fixture.unlink()
        assert not list(project.iterdir())
        print("PASS: isolated project untouched; native owners closed; no credentials used")


if __name__ == "__main__":
    asyncio.run(main())
