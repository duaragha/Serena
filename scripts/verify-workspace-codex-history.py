"""Prove native history pagination with print-only shell turns, no inference."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_codex import CodexWorkspace
from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from core.workspace_lease import SessionLease
from core.workspace_rpc import WorkspaceRpc


def browser_proof(sid, root, project, env, binary):
    from flask import Flask
    from playwright.sync_api import sync_playwright
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
    artifacts = repo / "apps" / "desktop" / "build" / "workspace-proof"
    artifacts.mkdir(parents=True, exist_ok=True)
    try:
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
                    page.goto(f"http://127.0.0.1:{server.server_port}/workspace/{sid}")
                    if pid is None:
                        assert not host._sessions, "Page load launched an owner"
                    page.get_by_role("button", name="Resume session", exact=True).click()
                    page.get_by_role("button", name="Run shell command", exact=True).wait_for(state="visible")
                    page.get_by_role("button", name="Run shell command", exact=True).click()
                    dialog = page.get_by_role("dialog", name="Run shell command")
                    token = f"SERENA_BROWSER_{label.upper()}"
                    dialog.get_by_role("textbox", name="Shell command").fill(f"printf {token}")
                    dialog.get_by_role("checkbox").check()
                    dialog.get_by_role("button", name="Run command", exact=True).click()
                    page.locator("summary").filter(has_text=token).first.click(timeout=15000)
                    page.locator("pre").filter(has_text=token).first.wait_for(timeout=15000)
                    owner = host._sessions[sid][0]
                    if pid is None:
                        pid = owner.rpc.process.pid
                    assert owner.rpc.process.pid == pid
                    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                    page.screenshot(path=str(artifacts / f"codex-native-{label}.png"))
                    if label == "desktop":
                        page.get_by_role("button", name="Load earlier messages", exact=True).click()
                        page.locator("summary").filter(has_text="SERENA_HISTORY_000").first.wait_for(state="attached", timeout=10000)
                    assert not errors, errors
                    page.close()
                    assert owner.rpc.process.returncode is None, "Closing page cancelled owner"
                    print(f"PASS: {label} real HTTP pane explicitly attached, ran native command and rendered output; same owner survived page close")
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        host.shutdown()


async def main():
    binary = shutil.which("codex")
    if not binary:
        raise RuntimeError("Native Codex unavailable")
    with tempfile.TemporaryDirectory(prefix="serena-history-proof-") as temporary:
        root = Path(temporary)
        home, project = root / "home", root / "project"
        (home / ".codex").mkdir(parents=True)
        project.mkdir()
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
        await asyncio.to_thread(browser_proof, sid, root, project, env, binary)
        assert not list(project.iterdir())
        print("PASS: isolated project untouched; native owners closed; no credentials used")


if __name__ == "__main__":
    asyncio.run(main())
