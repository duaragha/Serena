"""Exercise fresh native creation through the production Python/JSONL bridge."""
import ast
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
from flask import Flask, jsonify, request
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
    async def deny_permission(*args):
        raise AssertionError("Local command requested permission")
    client = ClaudeTypeScriptClient(options=SimpleNamespace(resume=sid, cwd=cwd, cli_path=cli,
                                    env=dict(os.environ), can_use_tool=deny_permission), sdk_path=sdk, node_path=node)
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
    from core import indexer, metadata
    from core.parser import parse_full
    assert indexer.DB_PATH.resolve().is_relative_to(Path(cwd).resolve())
    assert metadata.METADATA_DIR.resolve().is_relative_to(Path(cwd).resolve())
    def owner_factory(**kwargs):
        instance = ClaudeWorkspace(**kwargs, client_factory=factory,
                                   lease_factory=lambda sid: SessionLease(sid, directory=lease_dir))
        created.append(instance)
        return instance
    app = Flask(__name__, static_folder=str(Path(__file__).resolve().parents[1] / "ui/static"))
    host = install_workspace(app, Path(cwd) / "ui.db", describe=indexer.get_session,
                             factories={"claude": owner_factory})
    web_source = Path(__file__).resolve().parents[1] / "ui/web.py"
    definitions = [item for item in ast.parse(web_source.read_text()).body
                   if isinstance(item, ast.FunctionDef) and item.name in {"api_rename", "api_sessions", "_decorate_sessions",
                                                                        "_pending_workspace_meta", "api_conversation"}]
    namespace = {"app": app, "jsonify": jsonify, "request": request, "Path": Path, "parse_full": parse_full,
                 "get_session": indexer.get_session, "list_sessions": indexer.list_sessions,
                 "set_title": indexer.set_title, "_include_permanent_serena_session": lambda rows: rows,
                 "_ambiguous_shorts": lambda: set(), "_get_session_cwd": lambda session: session["cwd"],
                 "_resolve_project_cwd": lambda project, cwd: cwd,
                 "_shorten_project": lambda project, cwd: project, "_external_runtime_active": lambda sid: False}
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(web_source), "exec"), namespace)
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
                    title = f"Native new Claude {width}"
                    renamed = page.evaluate("async ({sid,title}) => (await fetch('/api/rename/'+sid,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title})})).json()", {"sid": sid, "title": title})
                    assert renamed == {"ok": True, "title": title}
                    assert metadata.get_meta(sid)["custom_title"] == title
                    pending = page.evaluate("async sid => (await fetch('/api/conversation/'+sid)).json()", sid)
                    assert pending["native_persistence_pending"] and pending["messages"] == [] and pending["title"] == title
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
                    deadline = time.monotonic() + 10
                    while host.journal.pending_target(sid) and time.monotonic() < deadline:
                        page.wait_for_timeout(50)
                    assert indexer.get_session(sid) and host.journal.pending_target(sid) is None
                    rows = page.evaluate("async () => (await fetch('/api/sessions')).json()")
                    matching = [row for row in rows if row["session_id"] == sid]
                    assert len(matching) == 1 and matching[0]["display_title"] == title and not matching[0].get("native_persistence_pending")
                    persisted = page.evaluate("async sid => (await fetch('/api/conversation/'+sid)).json()", sid)
                    assert persisted["messages"] and persisted["title"] == title and not persisted.get("native_persistence_pending")
                    complete = [entry["event"]["params"]["turn"]["providerOriginal"] for entry in host.journal.read(sid)["events"]
                                if entry["event"]["method"] == "turn/completed"]
                    assert complete and all(turn["total_cost_usd"] == 0 and turn["num_turns"] == 0 for turn in complete)
                    assert len(created) == before + 1 and not errors, errors
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(artifacts / f"new-claude-output-{width}.png"))
                    page.close()
                    assert psutil.pid_exists(pid) and native.state == "ready"
                    print(f"PASS: {width}px real Claude New Chat, reload, exact open, local input and named single-row indexing; one owner survives page close")
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
