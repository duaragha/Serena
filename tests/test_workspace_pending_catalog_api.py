"""Exercise the production sessions route without starting the global web host."""

import ast
from pathlib import Path

from flask import Flask, jsonify, request

from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal


def test_sessions_route_includes_only_matching_committed_pending_chat(tmp_path):
    app = Flask(__name__)
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "pending.db"), resolve=lambda sid: None)
    app.extensions["workspace_host"] = host
    indexed = []
    source = Path(__file__).resolve().parents[1] / "ui/web.py"
    tree = ast.parse(source.read_text())
    route = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "api_sessions")
    namespace = {"app": app, "jsonify": jsonify, "request": request,
                 "list_sessions": lambda **kwargs: indexed,
                 "_decorate_sessions": lambda rows: rows,
                 "_include_permanent_serena_session": lambda rows: rows}
    exec(compile(ast.Module(body=[route], type_ignores=[]), str(source), "exec"), namespace)
    target = {"session_id": "11111111-2222-4333-8444-555555555555", "provider": "claude", "cwd": str(tmp_path)}
    try:
        host.journal.claim_command("source", "clear", {"action": "clear_session", "payload": {"confirmed": True}})
        host.journal.prepare_clear("source", "clear", target)
        client = app.test_client()
        assert client.get("/api/sessions").json == []
        host.journal.complete_clear("source", "clear")
        rows = client.get("/api/sessions").json
        assert [row["session_id"] for row in rows] == [target["session_id"]]
        assert client.get("/api/sessions?project=unrelated-project").json == []
        indexed.append({"session_id": target["session_id"], "display_title": "User title"})
        assert client.get("/api/sessions").json == indexed
        indexed.clear()
        assert client.get("/api/sessions").json == []
        assert host._loop is None and not host._sessions
    finally:
        host.shutdown()
