"""Native create/checkpoint/lease/input/resume proof with no model invocation."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from flask import Flask
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_catalog import NativeTranscriptPending
from core.workspace_codex import CodexWorkspace
from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from core.workspace_lease import SessionLease, SessionOwnedError
from ui.workspace_app import install_workspace
from ui.workspace_web import workspace_blueprint


async def main():
    binary = shutil.which("codex")
    assert binary, "Native Codex executable missing"
    with tempfile.TemporaryDirectory(prefix="workspace-create-proof-") as temporary:
        root = Path(temporary)
        project, home = root / "project", root / "home"
        project.mkdir()
        home.mkdir()
        (home / ".codex").mkdir()
        env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex"),
               "XDG_CONFIG_HOME": str(home / ".config"), "PATH": os.environ["PATH"],
               "OPENAI_BASE_URL": "http://127.0.0.1:9/v1"}
        reservation = "new:" + str(uuid4())
        journal = WorkspaceJournal(root / "journal.db")
        events = []
        complete = asyncio.Event()
        async def publish(event):
            events.append(event)
            if event.get("method") == "turn/completed":
                assert event["params"]["turn"]["status"] == "completed"
                complete.set()
        async def checkpoint(target):
            assert not events
            journal.append(reservation, {"method": "workspace/created", "params": target})
        owner = CodexWorkspace(session_id=reservation, cwd=project, publish=publish,
                               lease_factory=lambda sid: SessionLease(sid, directory=root / "leases"))
        try:
            result = await owner.create(checkpoint=checkpoint, binary=binary, env=env)
            sid = owner.session_id
            assert result["thread"]["id"] == sid and result["thread"]["turns"] == []
            assert journal.read(reservation)["events"][0]["event"]["params"]["session_id"] == sid
            pid = owner.rpc.process.pid
            try:
                competing = SessionLease(sid, directory=root / "leases")
            except SessionOwnedError:
                pass
            else:
                competing.release()
                raise AssertionError("New native session was not exclusively owned")
            await owner.shell_command("printf SERENA_CREATED_NATIVE", True)
            await asyncio.wait_for(complete.wait(), 15)
            assert owner.rpc.process.pid == pid
            assert "SERENA_CREATED_NATIVE" in json.dumps(events)
            await owner.close()
            assert owner.rpc.process is None
            resumed = await owner.open(binary=binary, env=env)
            assert resumed["thread"]["id"] == sid
            assert "SERENA_CREATED_NATIVE" in json.dumps(resumed)
            paths = list((home / ".codex" / "sessions").rglob("*.jsonl"))
            assert len(paths) == 1, paths
            assert not list(project.iterdir())
            print("PASS: native creation checkpointed before output, exclusive lease transferred, same process accepted input")
            print("PASS: exact new session resumed with real output; one persisted transcript, zero inference, isolated project unchanged")
        except BaseException:
            print("\n".join(owner.rpc.stderr), file=sys.stderr)
            raise
        finally:
            await owner.close()
            assert owner.rpc.process is None
        print("PASS: native child reaped")
        created = []
        class NativeOwner(CodexWorkspace):
            async def create(self, *, checkpoint):
                return await super().create(checkpoint=checkpoint, binary=binary, env=env)
        def factory(**kwargs):
            instance = NativeOwner(**kwargs, lease_factory=lambda sid: SessionLease(sid, directory=root / "leases"))
            created.append(instance)
            return instance
        request_id = str(uuid4())
        indexed = []
        def register(target):
            result = subprocess.run([sys.executable, "-c", """
import json, sys
from core.workspace_catalog import register_fork, NativeTranscriptPending
from core.indexer import get_session
target = json.loads(sys.argv[1])
try:
    register_fork(target)
except NativeTranscriptPending:
    sys.exit(75)
print(json.dumps(get_session(target['session_id'])))
""", json.dumps(target)], env={**env, "CHATS_DATA_DIR": str(root / "index"),
                              "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                                    capture_output=True, text=True, timeout=15)
            if result.returncode == 75:
                raise NativeTranscriptPending("Native transcript is pending")
            assert result.returncode == 0, result.stderr
            indexed.append(json.loads(result.stdout))
        host = WorkspaceHost(journal=journal, resolve=lambda sid: None, factories={"codex": factory}, register_fork=register)
        app = Flask(__name__)
        token = "proof-token-" + str(uuid4())
        app.register_blueprint(workspace_blueprint(host, token=token))
        server = make_server("127.0.0.1", 0, app, threaded=True)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        def create_request():
            body = json.dumps({"request_id": request_id, "provider": "codex", "cwd": str(project), "confirmed": True}).encode()
            request = Request(f"http://127.0.0.1:{server.server_port}/api/workspace/create", data=body,
                              headers={"Content-Type": "application/json", "X-Serena-Workspace-Token": token})
            with urlopen(request, timeout=40) as response:
                return json.load(response)
        try:
            assert not created
            receipts = await asyncio.gather(*(asyncio.to_thread(create_request) for _ in range(4)))
            assert all(receipt == receipts[0] and receipt["ok"] for receipt in receipts)
            assert len(created) == 1
            target = receipts[0]["result"]["session_id"]
            assert journal.creation_target(request_id)["committed"]
            rows = host.include_pending_sessions([])
            assert len(rows) == 1 and rows[0]["agent"] == "codex" and rows[0]["session_id"] == target
            assert rows[0]["native_persistence_pending"]
            pid = created[0].rpc.process.pid
            command = await asyncio.to_thread(host.command, target, "native-input", "shell_command",
                                              {"command": "printf SERENA_HOST_CREATED", "confirmed": True})
            assert command["ok"], command
            async with asyncio.timeout(15):
                while not any(row["event"].get("method") == "turn/completed" for row in journal.read(target)["events"]):
                    await asyncio.sleep(0.02)
                while journal.pending_target(target) is not None:
                    await asyncio.sleep(0.02)
            assert "SERENA_HOST_CREATED" in json.dumps(journal.read(target))
            assert created[0].rpc.process.pid == pid
            assert len(indexed) == 1 and indexed[0]["session_id"] == target
            assert host.include_pending_sessions(indexed) == indexed
            assert host.include_pending_sessions([]) == []
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            serving.join(timeout=5)
            assert not serving.is_alive()
            await asyncio.to_thread(host.shutdown)
        assert created[0].rpc.process is None
        restored = WorkspaceHost(journal=journal, resolve=lambda sid: None, factories={"codex": factory})
        try:
            assert await asyncio.to_thread(restored.create, request_id, "codex", str(project), confirmed=True) == receipts[0]
            assert len(created) == 1 and restored._sessions == {}
        finally:
            await asyncio.to_thread(restored.shutdown)
        print("PASS: four concurrent authenticated HTTP requests created one native owner; exact input worked; restart replay did not launch again")
        print("PASS: real native first command indexed the exact pending Codex identity; placeholder retired without duplicate or revival")
        repo = Path(__file__).resolve().parents[1]
        app = Flask("native-creation-ui", static_folder=str(repo / "ui/static"))
        ui_host = install_workspace(app, root / "ui-journal.db", factories={"codex": factory},
                                    resolve=lambda sid: None, describe=lambda sid: None)
        ui_host.register_fork = register
        server = make_server("127.0.0.1", 0, app, threaded=True)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        def browser_proof():
            from playwright.sync_api import sync_playwright
            artifacts = repo / "apps/desktop/build/workspace-proof"
            artifacts.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as p:
                browser = p.chromium.launch()
                try:
                    for width in (1440, 390):
                        page = browser.new_page(viewport={"width": width, "height": 900})
                        errors = []
                        page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                        base = f"http://127.0.0.1:{server.server_port}"
                        before = len(created)
                        page.goto(base + "/workspace/new?" + urlencode({"source": f"new-{width}", "cwd": str(project)}))
                        page.get_by_role("button", name="Create Codex chat", exact=True).wait_for()
                        assert len(created) == before
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        page.screenshot(path=str(artifacts / f"new-codex-{width}.png"))
                        page.get_by_role("button", name="Create Codex chat", exact=True).click()
                        page.get_by_role("button", name="Open conversation", exact=True).wait_for()
                        native = created[-1]
                        assert len(created) == before + 1
                        pid, sid = native.rpc.process.pid, native.session_id
                        page.reload()
                        page.get_by_role("button", name="Open conversation", exact=True).click()
                        page.wait_for_url(base + "/workspace/" + sid)
                        page.get_by_role("button", name="Resume session", exact=True).click()
                        page.get_by_role("button", name="Run shell command", exact=True).click()
                        shell = page.get_by_role("dialog", name="Run shell command")
                        shell.get_by_role("textbox", name="Shell command").fill("printf SERENA_NEW_UI_NATIVE")
                        shell.get_by_role("checkbox").check()
                        shell.get_by_role("button", name="Run command", exact=True).click()
                        page.locator("summary").filter(has_text="SERENA_NEW_UI_NATIVE").first.click()
                        page.get_by_text("SERENA_NEW_UI_NATIVE", exact=True).wait_for()
                        assert len(created) == before + 1 and native.rpc.process.pid == pid
                        assert not errors, errors
                        page.screenshot(path=str(artifacts / f"new-codex-output-{width}.png"))
                        page.close()
                        assert native.rpc.process.pid == pid
                        print(f"PASS: {width}px real New Chat creation, reload, exact open and native input; one owner retained after page close")
                finally:
                    browser.close()
        try:
            await asyncio.to_thread(browser_proof)
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            serving.join(timeout=5)
            await asyncio.to_thread(ui_host.shutdown)
        assert all(owner.rpc.process is None for owner in created)
        print("PASS: native creation UI children reaped")


asyncio.run(main())
