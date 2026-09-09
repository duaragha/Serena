"""Opt-in rich workspace endpoints. No import-time providers or automatic attach."""

from __future__ import annotations

import ipaddress
import secrets
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, request


def local_workspace_request():
    try:
        peer = ipaddress.ip_address(request.remote_addr or "")
        hostname = urlsplit(request.host_url).hostname or ""
        local_host = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
        if not peer.is_loopback or not local_host:
            raise ValueError()
    except ValueError:
        return jsonify(ok=False, error="Workspace control is loopback-only"), 403
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
        return jsonify(ok=False, error="Cross-origin workspace request rejected"), 403


def workspace_blueprint(host, *, token: str):
    if len(token) < 32:
        raise ValueError("Workspace control token is too short")
    bp = Blueprint("structured_workspace", __name__, url_prefix="/api/workspace")

    @bp.before_request
    def authorize():
        denied = local_workspace_request()
        if denied is not None:
            return denied
        supplied = request.headers.get("X-Serena-Workspace-Token", "")
        if not secrets.compare_digest(supplied, token):
            return jsonify(ok=False, error="Workspace authentication required"), 403

    @bp.errorhandler(ValueError)
    def invalid(error):
        return jsonify(ok=False, error=str(error)), 400

    @bp.errorhandler(RuntimeError)
    def unavailable(error):
        return jsonify(ok=False, error=str(error)), 409

    @bp.post("/<sid>/attach")
    def attach(sid):
        return jsonify(host.attach(sid))

    @bp.post("/<sid>/uploads")
    def upload(sid):
        from core.workspace_uploads import MAX_UPLOAD_BYTES

        request.max_content_length = MAX_UPLOAD_BYTES + 1024 * 1024
        file = request.files.get("file")
        if file is None or not file.filename:
            raise ValueError("An attachment file is required")
        return jsonify(
            ok=True, upload=host.uploads.save(sid, file.filename, file.stream, file.mimetype)
        )

    @bp.get("/<sid>/events")
    def events(sid):
        return jsonify(host.events(sid, after=int(request.args.get("after", "0"))))

    @bp.post("/<sid>/commands")
    def command(sid):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {"request_id", "action", "payload"}:
            raise ValueError("Expected request_id, action and payload")
        return jsonify(host.command(sid, data["request_id"], data["action"], data["payload"]))

    return bp
