"""Standalone, authenticated Serena call host for the native model machine."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import threading
from contextlib import suppress
from io import BytesIO
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, request, send_file
from flask_sock import Sock

from core.artifacts import (
    ArtifactReceiptCapacityError,
    artifact_client_allowed,
    get_default_artifact_registry,
)
from core.chat_daemon import get_or_create_token
from voice.call.tts import create_tts_backend
from voice.desk.greetings import DeskGreetingPool

from .browser_auth import (
    CALL_SOCKET_COOKIE,
    CALL_SOCKET_COOKIE_PATH,
    CALL_SOCKET_TICKET_TTL_SECONDS,
    browser_call_tickets,
)
from .orchestrator import (
    get_default_runtime,
    get_desk_runtime,
    handle_websocket,
    warm_default_runtime_background,
    warm_desk_runtime_background,
)

app = Flask(__name__)
sock = Sock(app)
_GREETING_POOL: DeskGreetingPool | None = None
_GREETING_POOL_LOCK = threading.Lock()


def get_desk_greeting_pool() -> DeskGreetingPool:
    global _GREETING_POOL
    if _GREETING_POOL is not None:
        return _GREETING_POOL
    with _GREETING_POOL_LOCK:
        if _GREETING_POOL is None:
            _GREETING_POOL = DeskGreetingPool(
                get_desk_runtime(),
                tts_factory=create_tts_backend,
            )
    return _GREETING_POOL


def request_is_authorized(expected: str) -> bool:
    authorization = request.headers.get("Authorization", "")
    provided = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
    return bool(provided) and hmac.compare_digest(provided, expected)


def call_socket_is_authorized(expected: str) -> bool:
    if request_is_authorized(expected):
        return True
    return browser_call_tickets.consume(request.cookies.get(CALL_SOCKET_COOKIE, ""))


def unauthorized_payload() -> dict[str, Any]:
    return {
        "type": "error",
        "code": "unauthorized",
        "message": "unauthorized",
        "fatal": True,
    }


@app.get("/health")
def health():
    runtime = get_default_runtime()
    desk = get_desk_runtime()
    return jsonify(
        {
            "ok": True,
            "ready": runtime.ready,
            "models": runtime.status,
            "model_details": runtime.model_details,
            "desk": {
                "ready": desk.ready,
                "models": desk.status,
                "model_details": desk.model_details,
                "greetings": get_desk_greeting_pool().status,
            },
        }
    )


@app.post("/api/call/socket-auth")
def issue_call_socket_authorization():
    if not request_is_authorized(get_or_create_token()):
        return jsonify(unauthorized_payload()), 401
    response = jsonify({"ok": True, "expires_in": CALL_SOCKET_TICKET_TTL_SECONDS})
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        CALL_SOCKET_COOKIE,
        browser_call_tickets.issue(),
        max_age=CALL_SOCKET_TICKET_TTL_SECONDS,
        secure=True,
        httponly=True,
        samesite="Strict",
        path=CALL_SOCKET_COOKIE_PATH,
    )
    return response


def _serve_call_socket(ws, *, desk: bool = False) -> None:
    if not call_socket_is_authorized(get_or_create_token()):
        with suppress(Exception):
            ws.send(json.dumps(unauthorized_payload()))
        return
    handle_websocket(
        ws,
        runtime=get_desk_runtime() if desk else get_default_runtime(),
        peer_host=request.remote_addr,
    )


@sock.route("/ws/call")
def call_socket(ws):
    _serve_call_socket(ws)


@sock.route("/ws/desk")
def desk_socket(ws):
    _serve_call_socket(ws, desk=True)


@app.get("/desk/greeting")
def desk_greeting():
    if not request_is_authorized(get_or_create_token()):
        return jsonify(unauthorized_payload()), 401
    greeting = get_desk_greeting_pool().take()
    if greeting is None:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "desk greeting cache is warming",
                }
            ),
            503,
            {"Retry-After": "1"},
        )
    return Response(
        greeting.pcm,
        status=200,
        mimetype="application/vnd.serena.pcm16",
        headers={
            "X-Serena-Greeting-Id": greeting.greeting_id,
            "X-Serena-Sample-Rate": str(greeting.sample_rate),
            "X-Serena-Greeting-Daypart": greeting.daypart,
            "Cache-Control": "no-store",
        },
    )


@app.get("/artifacts/<token>")
def serve_artifact(token: str):
    if not artifact_client_allowed(request.remote_addr):
        abort(404)
    registry = get_default_artifact_registry()
    payload = registry.read(token)
    if payload is None:
        abort(404)
    response = send_file(
        BytesIO(payload.data),
        mimetype=payload.link.content_type,
        download_name=payload.link.name,
        as_attachment=False,
        conditional=False,
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    try:
        response.headers["X-Serena-Artifact-Receipt"] = registry.issue_receipt(payload.link)
    except ArtifactReceiptCapacityError:
        return jsonify({"ok": False, "error": "artifact receipt capacity reached"}), 503, {
            "Retry-After": "1"
        }
    return response


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default=os.environ.get("SERENA_CALL_HOST", "0.0.0.0"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SERENA_CALL_PORT", "8766")),
    )
    args = parser.parse_args(argv)
    warm_default_runtime_background()
    wake_model = Path.home() / ".config" / "serena" / "models" / "hey_serena.onnx"
    if wake_model.is_file():
        warm_desk_runtime_background()
        get_desk_greeting_pool().start_refill()
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
