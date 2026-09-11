"""Opt-in rich workspace endpoints. No import-time providers or automatic attach."""

from __future__ import annotations

import ipaddress
import secrets
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, request, send_file


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

    @bp.get("/<sid>/observe")
    def observe(sid):
        return jsonify(host.observe(sid))

    @bp.post("/<sid>/view-context")
    def view_context(sid):
        return jsonify(host.note_view_context(sid, request.get_json(silent=True)))

    @bp.post("/<sid>/sleep")
    def sleep(sid):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {"sleeping"} or type(data["sleeping"]) is not bool:
            raise ValueError("An explicit boolean sleeping state is required")
        return jsonify(host.set_sleep(sid, data["sleeping"]))

    @bp.post("/<sid>/handoff")
    def handoff(sid):
        data = request.get_json(silent=True)
        if (not isinstance(data, dict) or set(data) != {"provider", "prompt", "request_id"}
                or data["provider"] not in {"claude", "codex"}
                or not isinstance(data["prompt"], str) or not data["prompt"].strip()
                or len(data["prompt"].encode("utf-8")) > 1024 * 1024
                or not isinstance(data["request_id"], str) or not 1 <= len(data["request_id"]) <= 100):
            raise ValueError("An exact handoff provider, prompt and request ID are required")
        # Explicit user handoff may attach the exact target, through the same
        # admission/lease checks as Resume. It never creates a session.
        attached = host.attach(sid)
        if not attached.get("ok"):
            return jsonify(attached)
        result = host.bridge(sid, data["provider"], data["prompt"], data["request_id"], timeout=1)
        if result is None:
            raise RuntimeError("Handoff target has no attached owner")
        return jsonify(result)

    @bp.get("/<sid>/sessions")
    def sessions(sid):
        from core.workspace_catalog import list_saved_sessions

        archived = request.args.getlist("archived")
        if archived and archived not in (["true"], ["false"]):
            raise ValueError("Expected one boolean archived filter")
        return jsonify(host.decorate_archive_restores(list_saved_sessions(request.args.get("provider"), request.args.get("q", ""),
                                          int(request.args.get("offset", "0")), archived=archived == ["true"])))

    @bp.post("/create")
    def create():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) - {"seed"} != {"request_id", "provider", "cwd", "confirmed"}:
            raise ValueError("Expected request_id, provider, cwd and confirmed")
        return jsonify(host.create(data["request_id"], data["provider"], data["cwd"], confirmed=data["confirmed"],
                                   **({"seed": data["seed"]} if "seed" in data else {})))

    @bp.post('/<sid>/restore-archive')
    @bp.post('/<sid>/reconcile-archive')
    def restore_archive(sid):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {'request_id', 'confirmed'}:
            raise ValueError('Expected request_id and explicit confirmation')
        return jsonify(host.restore_archive(sid, data['request_id'], confirmed=data['confirmed'],
                                           **({'reconcile': True} if request.path.endswith('/reconcile-archive') else {})))

    @bp.post("/<sid>/archive-session")
    @bp.post("/<sid>/reconcile-archive-session")
    def archive_session(sid):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {"request_id", "confirmed"}:
            raise ValueError("Expected request_id and explicit confirmation")
        return jsonify(host.archive_session(
            sid, data["request_id"], confirmed=data["confirmed"],
            **({"reconcile": True} if request.path.endswith("/reconcile-archive-session") else {}),
        ))

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

    @bp.get("/<sid>/attachments/<attachment_id>")
    def attachment(sid, attachment_id):
        path, record = host.uploads.resolve(sid, attachment_id)
        if not record["media_type"].startswith("image/"):
            raise ValueError("Only validated image previews are available")
        response = send_file(path, mimetype=record["media_type"], conditional=False)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @bp.post("/<sid>/commands")
    def command(sid):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {"request_id", "action", "payload"}:
            raise ValueError("Expected request_id, action and payload")
        return jsonify(host.command(sid, data["request_id"], data["action"], data["payload"]))

    return bp
