"""Development opt-in mount for the real pane; default app remains unchanged."""

import json
import secrets

from flask import Blueprint, Response, abort

from core.workspace_admission import resolve_workspace_session
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
    host = WorkspaceHost(journal=WorkspaceJournal(state_path), resolve=resolve, factories=factories)
    app.register_blueprint(workspace_blueprint(host, token=token))
    pages = Blueprint("workspace_pages", __name__)
    pages.before_request(local_workspace_request)

    @pages.get("/workspace/<sid>")
    def page(sid):
        session = describe(sid)
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
<button id="workspace-connect" type="button">Resume session</button><main id="workspace-pane"></main>
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
