"""Exercise fresh native creation through the production Python/JSONL bridge."""
import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psutil
from flask import Flask
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_claude import ClaudeWorkspace
from core.workspace_claude_client import ClaudeTypeScriptClient
from core.workspace_lease import SessionLease, SessionOwnedError
from ui.workspace_app import install_workspace


async def main():
    sdk, cli, node, cwd = sys.argv[1:]
    assert Path(os.environ["HOME"]).resolve() == Path(cwd).resolve()
    assert os.environ["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9"
    sid, prompt = str(uuid4()), str(uuid4())
    messages = []
    completed = asyncio.Event()
    async def request(*args):
        raise AssertionError("Local command requested permission")
    client = ClaudeTypeScriptClient(options=SimpleNamespace(resume=sid, cwd=cwd, cli_path=cli,
                                    env=dict(os.environ), can_use_tool=request), sdk_path=sdk, node_path=node)
    transport = client.transport
    async def consume():
        async for message in client.receive_messages():
            messages.append(message)
            if message.get("type") == "result" and message.get("user_message_uuid") == prompt:
                completed.set()
    reader = asyncio.create_task(consume())
    pid = None
    try:
        await client.create()
        pid = transport.owned_pid
        assert pid and psutil.pid_exists(pid)
        async def inputs():
            yield {"type": "user", "uuid": prompt, "session_id": sid,
                   "parent_tool_use_id": None, "message": {"role": "user", "content": "/effort low"}}
        await client.query(inputs(), sid)
        await asyncio.wait_for(completed.wait(), 20)
        result = next(message for message in messages if message.get("type") == "result")
        assert result["session_id"] == sid and result["num_turns"] == 0 and result["total_cost_usd"] == 0
        assert transport.owned_pid == pid
        assert all(message["session_id"] == sid for message in messages if message.get("session_id"))
    finally:
        try:
            await client.disconnect()
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
    assert transport.rpc.process is None and not psutil.pid_exists(pid)
    files = list((Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects").glob(f"*/{sid}.jsonl"))
    assert len(files) == 1
    print("PASS: real Python client create -> JSONL worker -> native UUID input/output; exact transcript, zero inference, child reaped")
    events, checkpointed = [], []
    completed = asyncio.Event()
    async def publish(event):
        events.append(event)
        if event["method"] == "turn/completed":
            completed.set()
    def factory(*, options):
        options.cli_path = cli
        return ClaudeTypeScriptClient(options=options, sdk_path=sdk, node_path=node)
    lease_dir = Path(cwd) / "leases"
    owner = ClaudeWorkspace(session_id="new:" + str(uuid4()), cwd=cwd, publish=publish,
                            client_factory=factory, lease_factory=lambda sid: SessionLease(sid, directory=lease_dir))
    async def checkpoint(target):
        assert owner.client.owned_pid is None
        try:
            duplicate = SessionLease(target["session_id"], directory=lease_dir)
        except SessionOwnedError:
            pass
        else:
            duplicate.release()
            raise AssertionError("Reserved session lease was not exclusive")
        with (Path(cwd) / "creation.json").open("w") as record:
            json.dump(target, record)
            record.flush()
            os.fsync(record.fileno())
        checkpointed.append(target)
    try:
        await owner.create(checkpoint=checkpoint)
        native_pid = owner.client.owned_pid
        assert native_pid and psutil.pid_exists(native_pid)
        assert checkpointed == [{"session_id": owner.session_id, "provider": "claude", "cwd": cwd}]
        assert json.loads((Path(cwd) / "creation.json").read_text()) == checkpointed[0]
        await owner.submit([{"type": "text", "text": "/effort low"}])
        await asyncio.wait_for(completed.wait(), 20)
        assert owner.state == "ready" and owner.client.owned_pid == native_pid
        assert not any(event["method"] == "workspace/error" for event in events)
    finally:
        await owner.close()
    assert not psutil.pid_exists(native_pid)
    assert len(list((Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects").glob(f"*/{owner.session_id}.jsonl"))) == 1
    released = SessionLease(owner.session_id, directory=lease_dir)
    released.release()
    print("PASS: real owner reserves exclusive UUID and durable checkpoint before spawn; same-process local turn; close reaps child and releases lease")
    created = []
    def owner_factory(**kwargs):
        instance = ClaudeWorkspace(**kwargs, client_factory=factory,
                                   lease_factory=lambda sid: SessionLease(sid, directory=lease_dir))
        created.append(instance)
        return instance
    app = Flask(__name__, static_folder=str(Path(__file__).resolve().parents[1] / "ui/static"))
    host = install_workspace(app, Path(cwd) / "ui.db", describe=lambda sid: None,
                             factories={"claude": owner_factory})
    server = make_server("127.0.0.1", 0, app, threaded=True)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    def browser_proof():
        from urllib.parse import urlencode

        from playwright.sync_api import sync_playwright
        artifacts = Path(__file__).resolve().parents[1] / "apps/desktop/build/workspace-proof"
        artifacts.mkdir(parents=True, exist_ok=True)
        base = f"http://127.0.0.1:{server.server_port}"
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                for width in (1440, 390):
                    page = browser.new_page(viewport={"width": width, "height": 900})
                    errors = []
                    page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                    before = len(created)
                    page.goto(base + "/workspace/new?" + urlencode({"source": f"new-claude-{width}", "provider": "claude", "cwd": cwd}))
                    assert len(created) == before
                    page.get_by_role("button", name="Create Claude chat", exact=True).click()
                    page.get_by_role("button", name="Open conversation", exact=True).wait_for()
                    assert len(created) == before + 1
                    native = created[-1]
                    sid, pid = native.session_id, native.client.owned_pid
                    page.reload()
                    page.get_by_role("button", name="Open conversation", exact=True).click()
                    page.wait_for_url(base + "/workspace/" + sid)
                    page.get_by_role("button", name="Resume session", exact=True).click()
                    page.get_by_role("textbox", name="Message Claude").fill("/effort low")
                    page.get_by_role("button", name="Send message", exact=True).click()
                    deadline = time.monotonic() + 20
                    while not any(event["event"]["method"] == "turn/completed" for event in host.journal.read(sid)["events"]):
                        assert time.monotonic() < deadline, "Native turn did not complete"
                        page.wait_for_timeout(50)
                    assert native.state == "ready" and native.client.owned_pid == pid
                    page.get_by_text("/effort low", exact=True).wait_for()
                    assert len(created) == before + 1 and not errors, errors
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(artifacts / f"new-claude-output-{width}.png"))
                    page.close()
                    assert psutil.pid_exists(pid) and native.state == "ready"
                    print(f"PASS: {width}px real Claude New Chat, reload, exact open and local input; one owner survives page close")
            finally:
                browser.close()
    try:
        await asyncio.to_thread(browser_proof)
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        serving.join(timeout=5)
        await asyncio.to_thread(host.shutdown)
    assert all(instance.client.transport.rpc.process is None for instance in created)
    print("PASS: browser/native owner children reaped")


asyncio.run(main())
