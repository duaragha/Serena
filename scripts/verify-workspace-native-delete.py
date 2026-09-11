"""Prove recoverable native Codex deletion through Serena's real browser and HTTP path."""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import indexer, metadata
from core.workspace_archive import delete_codex_tree, inspect_codex_delete_tree
from core.workspace_catalog import register_fork, remove_deleted_codex_target
from core.workspace_lease import SessionLease
from core.workspace_rpc import WorkspaceRpc
from ui.workspace_app import install_workspace


class IdleOwner:
    def __init__(self, cwd):
        self.cwd = cwd
        self.state = "ready"
        self.active_turn = None
        self.questions = {}
        self.elicitations = {}
        self.active_agent_threads = set()
        self.events = type("Events", (), {"tasks": {}})()
        self.closes = 0

    async def list_background_tasks(self):
        return {"data": []}

    async def close(self):
        self.closes += 1
        self.state = "closed"

    def can_retry_attachment(self):
        return self.state == "closed"


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


async def create_family(binary, project, env, processes):
    rpc = WorkspaceRpc()
    try:
        await rpc.start([binary, "app-server", "--stdio"], cwd=project, env=env)
        processes.append(rpc.process)
        await rpc.request("initialize", {
            "clientInfo": {"name": "serena-delete-fixture", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        await rpc.notify("initialized", {})
        root = (await rpc.request("thread/start", {
            "cwd": str(project), "sandbox": "read-only", "approvalPolicy": "never",
        }))["thread"]
        await rpc.request("thread/inject_items", {"threadId": root["id"], "items": [{
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "SERENA_DELETE_FIXTURE"}],
        }]})
        child = (await rpc.request("thread/start", {
            "cwd": str(project), "sandbox": "read-only", "approvalPolicy": "never",
        }))["thread"]
        await rpc.request("thread/inject_items", {"threadId": child["id"], "items": [{
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "SERENA_DELETE_CHILD_FIXTURE"}],
        }]})
    finally:
        await rpc.close()

    source_record = {"subagent": {"thread_spawn": {
            "agent_nickname": "DeleteProof",
            "agent_path": None,
            "agent_role": "worker",
            "depth": 1,
            "parent_thread_id": root["id"],
        }}}
    source = json.dumps(source_record, separators=(",", ":"))
    with sqlite3.connect(Path(env["CODEX_HOME"]) / "state_5.sqlite") as connection:
        changed = connection.execute(
            "UPDATE threads SET source=?,thread_source='subagent',agent_nickname=?,"
            "agent_role='worker',agent_path=NULL WHERE id=?",
            (source, "DeleteProof", child["id"]),
        ).rowcount
        assert changed == 1
        connection.execute(
            "INSERT INTO thread_spawn_edges(parent_thread_id,child_thread_id,status) "
            "VALUES (?,?,'closed')",
            (root["id"], child["id"]),
        )

    rpc = WorkspaceRpc()
    try:
        await rpc.start([binary, "app-server", "--stdio"], cwd=project, env=env)
        processes.append(rpc.process)
        await rpc.request("initialize", {
            "clientInfo": {"name": "serena-delete-fixture", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        await rpc.notify("initialized", {})
        descendants = await rpc.request("thread/list", {
            "ancestorThreadId": root["id"],
            "archived": False,
            "limit": 50,
            "sourceKinds": ["subAgent", "subAgentReview", "subAgentCompact",
                            "subAgentThreadSpawn", "subAgentOther"],
        })
        assert [row["id"] for row in descendants["data"]] == [child["id"]], descendants
        await rpc.request("thread/archive", {"threadId": child["id"]})
    finally:
        await rpc.close()
    return root, child


def run_browser(root, sid, target, width, native_delete, native_inspect, catalog_remove):
    from playwright.sync_api import expect, sync_playwright
    from werkzeug.serving import WSGIRequestHandler, make_server

    repo = Path(__file__).resolve().parents[1]
    app = Flask(__name__, static_folder=str(repo / "ui/static"))
    host = install_workspace(
        app,
        root / "workspace.db",
        factories={},
        resolve=lambda identity: target if identity == sid else None,
    )
    owner = IdleOwner(Path(target["cwd"]))
    host._sessions[sid] = (owner, "codex")
    host.delete_catalog = catalog_remove
    host._dispatch(asyncio.sleep(0), 5)
    host.journal.append(sid, {
        "method": "workspace/history",
        "params": {"thread": {"id": sid, "turns": []}},
    })

    class SilentRequestHandler(WSGIRequestHandler):
        def log_request(self, *args, **kwargs):
            pass

    server = make_server(
        "127.0.0.1", 0, app, threaded=True, request_handler=SilentRequestHandler
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    requests = []
    errors = []
    try:
        with (
            patch("core.workspace_archive.delete_codex_tree", native_delete),
            patch("core.workspace_archive.inspect_codex_delete_tree", native_inspect),
            sync_playwright() as playwright,
        ):
            browser = playwright.chromium.launch(
                executable_path=(os.environ.get("SERENA_PROOF_BROWSER_EXECUTABLE")
                                 or shutil.which("microsoft-edge")),
                headless=True,
            )
            try:
                page = browser.new_page(viewport={"width": width, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: requests.append((request.method, request.url)))
                page.on(
                    "response",
                    lambda response: errors.append(f"{response.status} {response.url}")
                    if response.status >= 400 else None,
                )
                page.goto(f"http://127.0.0.1:{server.server_port}/workspace/{sid}")
                expect(page.locator(".aw-state")).to_have_text("ready")
                page.locator(".aw-composer textarea").fill("Keep exact delete draft")
                button = page.get_by_role("button", name="Delete conversation", exact=True)
                if not button.is_visible():
                    page.get_by_role("button", name="Session actions", exact=True).click()
                button.click()
                dialog = page.get_by_role("dialog", name="Delete conversation", exact=True)
                confirm = dialog.get_by_role(
                    "button", name="Confirm delete conversation", exact=True
                )
                expect(confirm).to_be_disabled()
                assert not any(url.endswith("/delete-session") for _, url in requests)
                dialog.get_by_role(
                    "checkbox", name="Confirm conversation deletion", exact=True
                ).check()
                with page.expect_response(
                    lambda response: response.url.endswith("/delete-session")
                ) as first_response:
                    confirm.click()
                first = first_response.value
                first_result = first.json()
                request_id = first.request.post_data_json["request_id"]
                assert first_result.get("uncertain") and not first_result.get("ok"), first_result
                expect(dialog.get_by_role("button", name="Check delete outcome", exact=True)).to_be_visible()
                assert page.locator(".aw-composer textarea").input_value() == "Keep exact delete draft"
                with page.expect_response(
                    lambda response: response.url.endswith("/reconcile-delete-session")
                ) as second_response:
                    dialog.get_by_role("button", name="Check delete outcome", exact=True).click()
                second = second_response.value
                result = second.json()
                assert second.request.post_data_json["request_id"] == request_id
                assert result["ok"], result
                expect(dialog.get_by_role("status")).to_have_text(
                    "Deleted 2 conversations from Codex and Serena. Local recovery was retained."
                )
                expect(page.locator(".aw-state")).to_have_text("unavailable")
                expect(page.locator("#workspace-connect")).to_have_text("Conversation deleted")
                expect(page.locator("#workspace-connect")).to_be_hidden()
                assert page.locator(".aw-composer textarea").input_value() == "Keep exact delete draft"
                assert page.get_by_role("button", name="Send message", exact=True).is_disabled()
                assert page.locator("body").evaluate("el=>el.scrollWidth<=innerWidth")
                delete_requests = [url for _, url in requests if url.endswith("/delete-session")]
                reconcile_requests = [
                    url for _, url in requests if url.endswith("/reconcile-delete-session")
                ]
                assert len(delete_requests) == 1 and len(reconcile_requests) == 1
                assert not any(url.endswith("/attach") or url.endswith("/commands")
                               for _, url in requests)
                assert page.url.endswith("/workspace/" + sid)
                assert not errors, errors
                proof = repo / "apps/desktop/build/workspace-proof"
                proof.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(proof / f"native-delete-{width}.png"))
                return result, request_id, owner.closes, host.journal
            finally:
                browser.close()
    finally:
        server.shutdown()
        server_thread.join(timeout=5)
        host.shutdown()
        assert not server_thread.is_alive()


async def main(width):
    binary = shutil.which("codex")
    if not binary:
        raise RuntimeError("Installed Codex is unavailable")
    with tempfile.TemporaryDirectory(prefix="workspace-native-delete-") as directory, ExitStack() as patches:
        root = Path(directory)
        home = root / "home"
        codex_home = home / ".codex"
        project = root / "project"
        home.mkdir()
        codex_home.mkdir()
        project.mkdir()
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home),
            "CODEX_HOME": str(codex_home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
        }
        for module, key, value in (
            (indexer, "DATA_DIR", root / "index-data"),
            (indexer, "DB_PATH", root / "index.db"),
            (indexer, "_schema_ready", False),
            (indexer, "_INDEX_LOCK_PATH", root / "index.lock"),
            (metadata, "METADATA_DIR", root / "metadata"),
            (metadata, "_migrated", True),
        ):
            patches.enter_context(patch.object(module, key, value))
        patches.enter_context(patch.dict(os.environ, {
            "HOME": str(home), "CODEX_HOME": str(codex_home),
            "XDG_CONFIG_HOME": str(home / ".config"),
        }))

        processes = []
        root_thread, child_thread = await create_family(binary, project, env, processes)
        sid, child_id = root_thread["id"], child_thread["id"]
        target = {"session_id": sid, "provider": "codex", "cwd": str(project)}
        child_target = {"session_id": child_id, "provider": "codex", "cwd": str(project)}
        register_fork(target)
        register_fork(child_target)
        assert indexer.get_session(sid) and indexer.get_session(child_id)
        assert len(list((codex_home / "sessions").rglob(f"*{sid}.jsonl"))) == 1
        assert len(list((codex_home / "archived_sessions").rglob(f"*{child_id}.jsonl"))) == 1

        native_methods = []
        native_mutations = []
        native_inspections = []

        class TrackedRpc(WorkspaceRpc):
            async def start(self, *args, **kwargs):
                await super().start(*args, **kwargs)
                processes.append(self.process)

            async def request(self, method, params, **kwargs):
                native_methods.append(method)
                return await super().request(method, params, **kwargs)

        def lease(identity):
            return SessionLease(identity, directory=root / "leases")

        async def isolated_delete(identity, cwd, recovery, **kwargs):
            native_mutations.append((identity, str(recovery)))
            return await delete_codex_tree(
                identity,
                cwd,
                recovery,
                binary=binary,
                env=env,
                rpc_factory=TrackedRpc,
                lease_factory=lease,
                **kwargs,
            )

        async def isolated_inspect(identity, cwd, checkpoint, **kwargs):
            native_inspections.append(identity)
            return await inspect_codex_delete_tree(
                identity,
                cwd,
                checkpoint,
                binary=binary,
                env=env,
                rpc_factory=TrackedRpc,
                lease_factory=lease,
                **kwargs,
            )

        fail_catalog_once = [True]

        def catalog_remove(item):
            if fail_catalog_once:
                fail_catalog_once.pop()
                raise RuntimeError("Proof-injected catalog interruption after native delete")
            return remove_deleted_codex_target(item)

        result, request_id, closes, journal = await asyncio.to_thread(
            run_browser,
            root,
            sid,
            target,
            width,
            isolated_delete,
            isolated_inspect,
            catalog_remove,
        )
        assert result["result"]["thread_ids"] == [sid, child_id]
        assert result["result"]["thread_count"] == 2
        assert closes == 1
        assert len(native_mutations) == 1 and native_mutations[0][0] == sid
        assert native_inspections == [sid]
        assert native_methods.count("thread/delete") == 2
        assert not any(method in native_methods for method in (
            "thread/resume", "thread/start", "turn/start", "thread/archive", "thread/unarchive"
        ))
        assert indexer.get_session(sid) is None and indexer.get_session(child_id) is None
        assert metadata.get_meta(sid) == {} and metadata.get_meta(child_id) == {}
        assert not list((codex_home / "sessions").rglob(f"*{sid}.jsonl"))
        assert not list((codex_home / "archived_sessions").rglob(f"*{child_id}.jsonl"))
        recovery = Path(result["result"]["recovery_dir"])
        manifest = json.loads((recovery / "recovery.json").read_text(encoding="utf-8"))
        assert manifest["root_session_id"] == sid and len(manifest["targets"]) == 2
        for item in manifest["targets"]:
            saved = Path(item["recovery_path"])
            assert saved.is_file() and saved.stat().st_size == item["size"]
            assert digest(saved) == item["sha256"]
        found, receipt = journal.command_receipt(
            sid,
            request_id,
            {"action": "delete_session", "payload": {"confirmed": True}},
        )
        assert found and receipt == result and not journal.has_pending_delete(sid)
        methods = [entry["event"]["method"] for entry in journal.read(sid)["events"]]
        assert methods.count("workspace/deletePrepared") == 1
        assert methods.count("workspace/deleted") == 1
        assert all(process.returncode is not None for process in processes)
        assert not list(project.iterdir())
        print(json.dumps({
            "ok": True,
            "root": sid,
            "archivedChild": child_id,
            "requestId": request_id,
            "explicitDeleteRequests": 1,
            "nativeFamilyDeleteCalls": native_methods.count("thread/delete"),
            "nativeInspections": len(native_inspections),
            "processesReaped": len(processes),
            "browserWidth": width,
            "draftRetained": True,
            "recoveryVerified": True,
            "catalogRemoved": True,
            "credentialsUsed": False,
            "inference": False,
        }, sort_keys=True))
        print("PASS: one explicit browser delete removed the real root and archived child")
        print("PASS: post-delete failure reconciled by inspection with the same receipt and no replay")
        print("PASS: recovery, catalog cleanup, process cleanup, draft retention and no-auto-attach verified")
    print("PASS: disposable profile removed; user sessions and project files were untouched")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-width", type=int, choices=(390, 1600), default=390)
    arguments = parser.parse_args()
    asyncio.run(main(arguments.browser_width))
