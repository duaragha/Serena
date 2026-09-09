"""Prefer an existing structured owner without creating or focusing a pane."""

from uuid import uuid4

from flask import current_app, request

from ui.workspace_web import local_workspace_request


def structured_bridge(provider, sid, prompt, timeout):
    host = current_app.extensions.get("workspace_host")
    if host is None:
        return None
    if local_workspace_request() is not None:
        return {"ok": False, "response": "", "message": "Structured bridge is local-only"}
    data = request.get_json(silent=True) or {}
    request_id = data.get("request_id") or str(uuid4())
    try:
        result = host.bridge(sid, provider, prompt, request_id, timeout=timeout)
        return None if result is None else {**result, "request_id": request_id}
    except (ValueError, RuntimeError) as error:
        # Once a structured owner is involved, never retry through a terminal.
        return {"ok": False, "response": "", "message": str(error)}
