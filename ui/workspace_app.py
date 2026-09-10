"""Development opt-in mount for the real pane; default app remains unchanged."""

import json
import secrets
from pathlib import Path

from flask import Blueprint, Response, abort, request

from core.workspace_admission import resolve_workspace_session
from core.workspace_catalog import register_fork
from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from ui.workspace_web import local_workspace_request, workspace_blueprint


def _describe(sid):
    from core.indexer import get_session

    return get_session(sid)


def install_workspace(
    app, state_path, *, resolve=resolve_workspace_session, factories=None, describe=_describe
):
    token = secrets.token_urlsafe(32)
    host = WorkspaceHost(journal=WorkspaceJournal(state_path), resolve=resolve, factories=factories, register_fork=register_fork)
    app.register_blueprint(workspace_blueprint(host, token=token))
    pages = Blueprint("workspace_pages", __name__)
    pages.before_request(local_workspace_request)

    @pages.get("/workspace/new")
    def new_page():
        cwd = request.args.get("cwd", "")
        source = request.args.get("source", "")
        provider = request.args.get("provider", "codex")
        if (provider not in {"codex", "claude"} or not source or len(source) > 200
                or "\0" in source or not Path(cwd).is_absolute() or not Path(cwd).is_dir()):
            abort(400)
        label = {"codex": "Codex", "claude": "Claude"}[provider]
        boot = json.dumps({"source": source, "cwd": str(Path(cwd).resolve()), "provider": provider, "token": token}).replace("<", "\\u003c")
        response = Response("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>New """ + label + """ chat</title>
<link rel="stylesheet" href="/static/workspace-page.css"></head><body>
<main class="workspace-create"><h1>New """ + label + """ chat</h1><label for="creation-project">Project</label>
<input id="creation-project" readonly><p id="creation-status" role="status"></p>
<button id="creation-submit" type="button" disabled>Create """ + label + """ chat</button>
<button id="creation-open" type="button" hidden>Open conversation</button></main>
<script id="workspace-creation-boot" type="application/json">""" + boot + """</script>
<script type="module" src="/static/workspace-create.mjs"></script></body></html>""", mimetype="text/html")
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'none'"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    @pages.get("/workspace/<sid>")
    def page(sid):
        session = describe(sid) or host.describe_pending_session(sid)
        if not session or session.get("session_id") != sid:
            abort(404)
        provider = {"codex": "Codex", "claude": "Claude", "gemini": "Gemini"}.get(
            session.get("agent"), "Unknown"
        )
        boot = json.dumps({"sessionId": sid, "provider": provider, "token": token}).replace(
            "<", "\\u003c"
        )
        response = Response(
            """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Serena</title>
<link rel="stylesheet" href="/static/workspace-pane.css">
<link rel="stylesheet" href="/static/workspace-page.css"></head><body>
<button id="workspace-connect" type="button" disabled>Resume session</button><main id="workspace-pane"></main>
<script id="workspace-boot" type="application/json">"""
            + boot
            + """</script>
<script src="/static/vendor/lucide.min.js"></script>
<script type="module" src="/static/workspace-page.mjs"></script></body></html>""",
            mimetype="text/html",
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; frame-ancestors 'self'; object-src 'none'; base-uri 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    app.register_blueprint(pages)
    app.extensions["workspace_host"] = host
    return host
