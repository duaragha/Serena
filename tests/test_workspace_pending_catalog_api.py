"""Exercise the production sessions route without starting the global web host."""

import ast
from pathlib import Path

from flask import Flask, jsonify, request

from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal


def test_pending_rename_uses_synced_metadata_and_unknown_ids_stay_rejected(tmp_path, monkeypatch):
    from core import indexer, metadata

    monkeypatch.setattr(metadata, "METADATA_DIR", tmp_path / "metadata")
    monkeypatch.setattr(metadata, "METADATA_PATH", tmp_path / "legacy.json")
    monkeypatch.setattr(metadata, "_migrated", False)
    app = Flask(__name__)
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "pending.db"), resolve=lambda sid: None)
    app.extensions["workspace_host"] = host
    normal_calls = []

    def normal_title(sid, title):
        normal_calls.append((sid, title))
        raise ValueError("No indexed session")

    def missing(sid):
        raise ValueError("No indexed session")

    monkeypatch.setattr(indexer, "toggle_done", missing)

    source = Path(__file__).resolve().parents[1] / "ui/web.py"
    tree = ast.parse(source.read_text())
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name in {"api_rename", "api_sessions", "_decorate_sessions", "_pending_workspace_meta",
                                  "_toggle_workspace_done", "api_star", "api_done", "api_bulk_done"}]
    namespace = {"app": app, "jsonify": jsonify, "request": request, "set_title": normal_title,
                 "get_session": lambda sid: None, "list_sessions": lambda **kwargs: [], "toggle_star": missing,
                 "_include_permanent_serena_session": lambda rows: rows,
                 "_ambiguous_shorts": lambda: set(), "_get_session_cwd": lambda session: session["cwd"],
                 "_resolve_project_cwd": lambda project, cwd: cwd,
                 "_shorten_project": lambda project, cwd: project,
                 "_external_runtime_active": lambda sid: False}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), namespace)
    sid = "11111111-2222-4333-8444-555555555555"
    target = {"session_id": sid, "provider": "claude", "cwd": str(tmp_path)}
    try:
        host.journal.claim_command("source", "clear", {"action": "clear_session", "payload": {"confirmed": True}})
        host.journal.prepare_clear("source", "clear", target)
        client = app.test_client()
        assert client.post(f"/api/rename/{sid}", json={"title": "too early"}).status_code == 404
        for action in ("star", "done"):
            assert client.post(f"/api/{action}/{sid}").status_code == 404
        assert not (metadata.METADATA_DIR / f"{sid}.json").exists()
        host.journal.complete_clear("source", "clear")
        metadata.set_starred(sid, True)
        response = client.post(f"/api/rename/{sid}", json={"title": "My conversation"})
        assert response.status_code == 200 and response.json["title"] == "My conversation"
        stored = metadata.get_meta(sid)
        assert stored["custom_title"] == "My conversation" and stored["starred"] is True
        rows = client.get("/api/sessions").json
        assert len(rows) == 1 and rows[0]["display_title"] == "My conversation"
        assert rows[0]["starred"] is True and rows[0]["is_done"] is False
        assert client.post(f"/api/star/{sid}").json == {"starred": False}
        assert client.post(f"/api/star/{sid}").json == {"starred": True}
        assert client.post(f"/api/done/{sid}").json == {"ok": True, "done": True}
        marked = client.get("/api/sessions").json[0]
        assert marked["is_done"] is True and marked["done_at"]
        assert metadata.get_meta(sid)["custom_title"] == "My conversation"
        assert client.post("/api/bulk-done", json={"ids": [sid, "unknown"], "done": True}).json["count"] == 0
        assert client.post("/api/bulk-done", json={"ids": [sid, sid], "done": False}).json["count"] == 1
        assert not metadata.get_meta(sid).get("done_at")
        assert client.post("/api/bulk-done", json={"ids": [sid]}).json["count"] == 1
        assert client.post(f"/api/done/{sid}").json["done"] is False
        assert client.post("/api/rename/not-a-session", json={"title": "wrong"}).status_code == 404
        host.journal.mark_clear_cataloged(sid)
        assert client.post(f"/api/rename/{sid}", json={"title": "deleted"}).status_code == 404
        assert metadata.get_meta(sid)["custom_title"] == "My conversation"
        for action in ("star", "done"):
            assert client.post(f"/api/{action}/{sid}").status_code == 404
            assert client.post(f"/api/{action}/unknown").status_code == 404
        assert len(normal_calls) == 3
        assert host._loop is None and not host._sessions
        indexed_calls = []
        namespace["get_session"] = lambda value: {"session_id": value, "is_done": False}
        namespace["toggle_star"] = lambda value: indexed_calls.append(("star", value)) or True
        monkeypatch.setattr(indexer, "toggle_done", lambda value: indexed_calls.append(("done", value)) or True)
        assert client.post(f"/api/star/{sid}").json["starred"] is True
        assert client.post(f"/api/done/{sid}").json["done"] is True
        assert client.post("/api/bulk-done", json={"ids": [sid], "done": True}).json["count"] == 1
        assert indexed_calls == [("star", sid), ("done", sid), ("done", sid)]
    finally:
        host.shutdown()


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
