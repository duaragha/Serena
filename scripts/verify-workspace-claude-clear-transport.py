"""Real worker handoff proof; invoked with the isolated native clear fixture."""

import ast
import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psutil

from core.workspace_claude import ClaudeWorkspace
from core.workspace_claude_client import ClaudeTypeScriptClient
from core.workspace_claude_transport import ClaudeSdkTransport
from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from core.workspace_lease import SessionLease
from core.workspace_rpc import WorkspaceRpcError


async def main():
    sdk, cli, node, root, source = sys.argv[1:]
    assert Path(os.environ["HOME"]).resolve() == Path(root).resolve()
    assert os.environ["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9"
    output = []
    completed = asyncio.Event()
    message_id = str(uuid4())

    async def publish(message):
        output.append(message)
        if message.get("type") == "transport_error":
            completed.set()
        if message.get("type") == "result" and message.get("user_message_uuid") == message_id:
            completed.set()

    async def deny(*args):
        raise AssertionError("Local clear unexpectedly requested interaction")

    transport = ClaudeSdkTransport(session_id=source, cwd=root, sdk_path=sdk,
                                   cli_path=cli, node_path=node, publish=publish, request=deny)
    assert transport.rpc.process is None
    native_pid = None
    try:
        await transport.open()
        native_pid = transport.owned_pid
        wrapper_pid = transport.rpc.process.pid
        target = (await transport.begin_clear())["sessionId"]
        assert source != target and transport.session_id == source
        for operation in (transport.send({"session_id": source}),
                          transport.control("supportedAgents"), transport.commit_clear(source)):
            try:
                await operation
            except WorkspaceRpcError:
                pass
            else:
                raise AssertionError("Input or wrong acknowledgement accepted during handoff")
        await transport.commit_clear(target)
        assert transport.session_id == target and not transport.transition_pending
        await transport.send({"type": "user", "uuid": message_id, "session_id": target,
                              "parent_tool_use_id": None,
                              "message": {"role": "user", "content": "/effort low"}})
        await asyncio.wait_for(completed.wait(), 25)
        assert not transport.failure, str(transport.failure)
        result = next(message for message in output if message.get("type") == "result"
                      and message.get("user_message_uuid") == message_id)
        assert result["session_id"] == target
        assert result["total_cost_usd"] == 0 and result["num_turns"] == 0
        assert transport.owned_pid == native_pid and transport.rpc.process.pid == wrapper_pid
        assert psutil.Process(native_pid).ppid() == wrapper_pid
        print(json.dumps({"source": source, "target": target, "nativePid": native_pid,
                          "sameNativeProcess": True, "modelTurns": 0, "cost": 0}))
    finally:
        await transport.close()
    assert transport.rpc.process is None
    assert native_pid is not None and not psutil.pid_exists(native_pid)
    print("PASS: real Python/JSONL/native clear handoff, blocked input, exact subsequent session, child reaped")

    old_events, new_events = [], []
    finished = asyncio.Event()

    async def old_publish(event):
        old_events.append(event)

    async def new_publish(event):
        new_events.append(event)
        if event["method"] == "turn/completed":
            finished.set()

    leases = Path(root) / "leases"
    owner = ClaudeWorkspace(session_id=source, cwd=root, publish=old_publish,
                            client_factory=lambda options: ClaudeTypeScriptClient(
                                options=options, sdk_path=sdk, node_path=node),
                            lease_factory=lambda sid: SessionLease(sid, directory=leases))
    try:
        await owner.open()
        native_pid = owner.client.owned_pid
        original_events = list(old_events)
        target = (await owner.begin_clear())["session_id"]
        await owner.commit_clear(target, publish=new_publish)
        assert owner.state == "ready" and owner.client.options.resume == target
        assert owner._lease.record["child"]["pid"] == native_pid
        assert owner.client.owned_pid == native_pid and old_events == original_events
        assert new_events[0]["params"]["thread"]["id"] == target
        finished.clear()
        await owner.submit([{"type": "text", "text": "/effort low"}])
        await asyncio.wait_for(finished.wait(), 25)
        assert owner.state == "ready" and owner.client.owned_pid == native_pid
        completed_turn = next(event["params"]["turn"] for event in reversed(new_events)
                              if event["method"] == "turn/completed")
        assert completed_turn["providerOriginal"]["total_cost_usd"] == 0
        assert completed_turn["providerOriginal"]["num_turns"] == 0
        assert old_events == original_events
        print(json.dumps({"ownerSource": source, "ownerTarget": target,
                          "sameNativeProcess": True, "leaseTransferred": True}))
    finally:
        await owner.close()
    assert not psutil.pid_exists(native_pid)

    def browser_clear(width):
        from flask import Flask, jsonify, request
        from playwright.sync_api import sync_playwright
        from werkzeug.serving import make_server

        from core import indexer, metadata
        from core.workspace_catalog import register_fork
        from ui.workspace_app import install_workspace

        repo = Path(__file__).resolve().parents[1]
        app = Flask(__name__, static_folder=str(repo / "ui/static"))
        browser_host = install_workspace(app, Path(root) / f"browser-{width}.db", resolve=resolve,
                                         factories={"claude": create_owner},
                                         describe=lambda sid: {"session_id": sid, "agent": "claude"} if sid == source else None)
        browser_host.register_fork = register_fork
        assert indexer.DB_PATH.resolve().is_relative_to(Path(root).resolve())
        assert metadata.METADATA_DIR.resolve().is_relative_to(Path(root).resolve())
        web_source = repo / "ui/web.py"
        definitions = [item for item in ast.parse(web_source.read_text()).body
                       if isinstance(item, ast.FunctionDef) and item.name in {"api_rename", "api_sessions", "_decorate_sessions",
                                                                            "_pending_workspace_meta", "_toggle_workspace_done",
                                                                            "api_star", "api_done", "api_bulk_done"}]
        namespace = {"app": app, "jsonify": jsonify, "request": request,
                     "get_session": indexer.get_session, "list_sessions": indexer.list_sessions,
                     "set_title": indexer.set_title, "toggle_star": indexer.toggle_star,
                     "_include_permanent_serena_session": lambda rows: rows,
                     "_ambiguous_shorts": lambda: set(), "_get_session_cwd": lambda session: session["cwd"],
                     "_resolve_project_cwd": lambda project, cwd: cwd,
                     "_shorten_project": lambda project, cwd: project, "_external_runtime_active": lambda sid: False}
        exec(compile(ast.Module(body=definitions, type_ignores=[]), str(web_source), "exec"), namespace)
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        pid = None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                try:
                    page = browser.new_page(viewport={"width": width, "height": 900})
                    errors = []
                    failures = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("response", lambda response: failures.append((response.status, response.url))
                            if response.status >= 400 and not response.url.endswith("favicon.ico") else None)
                    page.goto(f"http://127.0.0.1:{server.server_port}/workspace/{source}")
                    page.get_by_role("textbox", name="Message Claude").wait_for()
                    assert not browser_host._sessions
                    page.get_by_role("button", name="Resume session").click()
                    page.get_by_role("button", name="Resume session").wait_for(state="hidden")
                    pid = browser_host._sessions[source][0].client.owned_pid
                    page.get_by_role("button", name="Clear context", exact=True).click()
                    dialog = page.get_by_role("dialog", name="Clear context", exact=True)
                    dialog.get_by_role("button", name="Confirm clear context", exact=True).click()
                    dialog.get_by_text("Context cleared", exact=True).wait_for()
                    target = dialog.locator("code").inner_text()
                    assert target != source and browser_host._sessions[target][0].client.owned_pid == pid
                    assert browser_host.include_pending_sessions([])[0]["session_id"] == target
                    title = f"Clear proof {width}"
                    renamed = page.evaluate("async ({sid,title}) => (await fetch('/api/rename/'+sid,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title})})).json()", {"sid": target, "title": title})
                    assert renamed == {"ok": True, "title": title}
                    assert metadata.get_meta(target)["custom_title"] == title
                    pending_rows = page.evaluate("async () => (await fetch('/api/sessions')).json()")
                    pending_row = next(row for row in pending_rows if row["session_id"] == target)
                    assert pending_row["display_title"] == title and pending_row["native_persistence_pending"]
                    assert page.evaluate("async sid => (await fetch('/api/star/'+sid,{method:'POST'})).json()", target) == {"starred": True}
                    assert page.evaluate("async sid => (await fetch('/api/done/'+sid,{method:'POST'})).json()", target) == {"ok": True, "done": True}
                    marked = metadata.get_meta(target)
                    assert marked["starred"] and marked["done"] and marked["done_at"]
                    page.reload()
                    page.get_by_role("button", name="Clear context", exact=True).click()
                    dialog = page.get_by_role("dialog", name="Clear context", exact=True)
                    dialog.get_by_text("Context cleared", exact=True).wait_for()
                    dialog.get_by_role("button", name="Open new conversation", exact=True).click()
                    page.wait_for_url(f"**/workspace/{target}")
                    page.get_by_role("button", name="Resume session").click()
                    page.get_by_role("button", name="Resume session").wait_for(state="hidden")
                    assert browser_host._sessions[target][0].client.owned_pid == pid
                    page.get_by_role("textbox", name="Message Claude").fill("/effort low")
                    page.get_by_role("button", name="Send message", exact=True).click()
                    deadline = time.monotonic() + 25
                    while time.monotonic() < deadline:
                        complete = [entry["event"]["params"]["turn"] for entry in browser_host.events(target)["events"]
                                    if entry["event"]["method"] == "turn/completed"]
                        if any(turn["providerOriginal"].get("result", "").lower().find("low") >= 0 for turn in complete):
                            break
                        page.wait_for_timeout(50)
                    else:
                        raise AssertionError("Browser local command did not complete")
                    assert all(turn["providerOriginal"]["total_cost_usd"] == 0 for turn in complete)
                    assert all(turn["providerOriginal"]["num_turns"] == 0 for turn in complete)
                    page.wait_for_function("() => document.querySelector('.aw-state').textContent === 'completed'")
                    deadline = time.monotonic() + 10
                    while browser_host.journal.uncataloged_clears() and time.monotonic() < deadline:
                        page.wait_for_timeout(50)
                    indexed = indexer.get_session(target)
                    assert indexed and not indexed.get("is_teammate"), indexed
                    indexed_rows = page.evaluate("async () => (await fetch('/api/sessions')).json()")
                    matching = [row for row in indexed_rows if row["session_id"] == target]
                    assert len(matching) == 1 and matching[0]["display_title"] == title
                    assert not matching[0].get("native_persistence_pending")
                    assert matching[0]["starred"] and not matching[0]["is_done"], {"row": matching[0], "native": Path(indexed["file_path"]).read_text()}
                    assert not browser_host.journal.uncataloged_clears()
                    assert not errors, errors
                    assert not failures, failures
                    assert page.locator("body").evaluate("el=>el.scrollWidth<=innerWidth")
                    directory = repo / "apps/desktop/build/workspace-proof"
                    directory.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(directory / f"native-clear-{width}.png"))
                    page.goto(f"http://127.0.0.1:{server.server_port}/workspace/{source}")
                    page.get_by_role("button", name="Resume original conversation", exact=True).click()
                    page.get_by_role("button", name="Resume original conversation", exact=True).wait_for(state="hidden")
                    assert not page.get_by_role("button", name="Send message", exact=True).is_disabled()
                    assert browser_host._sessions[source][0].session_id == source
                    assert browser_host._sessions[source][0].client.owned_pid != pid
                    assert browser_host._sessions[target][0].client.owned_pid == pid
                    assert page.evaluate("sid=>sessionStorage.getItem('serena-workspace-clear:'+sid)", source) == "null"
                    original_pid = browser_host._sessions[source][0].client.owned_pid
                    page.get_by_role("button", name="Disconnect session", exact=True).click()
                    disconnect = page.get_by_role("dialog", name="Disconnect session", exact=True)
                    assert psutil.pid_exists(original_pid)
                    disconnect.get_by_role("button", name="Disconnect", exact=True).click()
                    disconnect.wait_for(state="hidden")
                    assert not psutil.pid_exists(original_pid)
                    assert psutil.pid_exists(pid), "Disconnecting original must preserve target runtime"
                    assert not errors and not failures
                finally:
                    browser.close()
            assert psutil.pid_exists(pid), "Closing the page must preserve native ownership"
            print(f"PASS: {width}px browser confirmed native clear, recovered receipt on reload, opened exact target, sent local command; one retained PID")
            print(f"PASS: {width}px pending rename used synced metadata and survived real native transcript indexing without a duplicate row")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
            browser_host.shutdown()
        assert pid is not None and not psutil.pid_exists(pid)

    print("PASS: real owner/client lease and event routing handoff; new input completed; original events untouched")

    def create_owner(**kwargs):
        return ClaudeWorkspace(**kwargs, client_factory=lambda options: ClaudeTypeScriptClient(
            options=options, sdk_path=sdk, node_path=node),
            lease_factory=lambda sid: SessionLease(sid, directory=leases))

    def resolve(sid):
        assert sid == source, "Target must attach to retained owner, not launch a new process"
        return {"session_id": sid, "provider": "claude", "cwd": root}

    host = WorkspaceHost(journal=WorkspaceJournal(Path(root) / "host.db"), resolve=resolve,
                         factories={"claude": create_owner})
    try:
        assert (await asyncio.to_thread(host.attach, source))["ok"]
        native_pid = host._sessions[source][0].client.owned_pid
        receipt = await asyncio.to_thread(host.command, source, "explicit-clear", "clear_session", {"confirmed": True})
        assert receipt["ok"], receipt
        target = receipt["result"]["session_id"]
        assert source not in host._sessions
        assert (await asyncio.to_thread(host.command, source, "explicit-clear", "clear_session", {"confirmed": True})) == receipt
        assert (await asyncio.to_thread(host.attach, target))["ok"]
        assert host._sessions[target][0].client.owned_pid == native_pid
        before = host.events(source)
        sent = await asyncio.to_thread(host.command, target, "new-input", "submit",
                                      {"inputs": [{"type": "text", "text": "/effort low"}]})
        assert sent["ok"], sent
        turn_id = sent["result"]["turn"]["id"]
        async with asyncio.timeout(25):
            while True:
                completions = [entry["event"]["params"]["turn"] for entry in host.events(target)["events"]
                               if entry["event"]["method"] == "turn/completed"]
                completed_turn = next((turn for turn in completions if turn["id"] == turn_id), None)
                if completed_turn:
                    break
                await asyncio.sleep(0.02)
        assert completed_turn["providerOriginal"]["total_cost_usd"] == 0
        assert completed_turn["providerOriginal"]["num_turns"] == 0
        assert host.events(source) == before
        reopened = WorkspaceJournal(host.journal.path)
        assert reopened.clear_target(target)["committed"]
        assert not reopened.has_pending_clear(source)
        print("PASS: real host clear checkpoint, exact target attach without spawn, source receipt replay, target input and durable journal routing")
    finally:
        await asyncio.to_thread(host.shutdown)
    assert not psutil.pid_exists(native_pid)
    for width in (1440, 390):
        await asyncio.to_thread(browser_clear, width)


asyncio.run(main())
