"""The HTTP mount for Serena's signed webhook ingress.

`core.webhook_ingress` decides; this only carries bytes to it. The split is
deliberate: every refusal rule stays testable without standing up a server, and
this layer owns exactly two decisions of its own.

The first is what an outside caller is told. A caller that failed gets its
delivery id and nothing else, because the reasons are diagnostic ("no webhook
secret is configured", "unknown route") and an unauthenticated stranger has not
earned them. They are in the durable audit trail, where Raghav can read them.

The second is who may approve. Posting a signed request is something the
internet may do; releasing a held one is not, so the management endpoints are
loopback-only and the public ingress is not.
"""

from __future__ import annotations

import ipaddress
import threading
from typing import Any

from flask import Blueprint, jsonify, request

from core.webhook_ingress import WebhookIngressError, default_ingress

webhook_bp = Blueprint("webhooks", __name__)

_INGRESS: Any = None
_INGRESS_LOCK = threading.Lock()


def _ingress() -> Any:
    """One ingress per process, so routes and the replay store are shared."""

    global _INGRESS
    with _INGRESS_LOCK:
        if _INGRESS is None:
            _INGRESS = default_ingress()
        return _INGRESS


def _reset_ingress_for_tests(ingress: Any = None) -> None:
    """Point the blueprint at a temporary ingress. Only tests call this."""

    global _INGRESS
    with _INGRESS_LOCK:
        _INGRESS = ingress


def _is_loopback() -> bool:
    raw = request.remote_addr
    if not raw:
        return False
    try:
        peer = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return False
    return peer.is_loopback


def _local_only():
    if not _is_loopback():
        return jsonify({"ok": False, "error": "available only from this computer"}), 403
    return None


@webhook_bp.post("/webhooks/<name>")
def receive_webhook(name: str):
    """The signed front door. Open to the network; the signature is the gate."""

    result = _ingress().handle(name, request.get_data(), dict(request.headers))
    body: dict[str, Any] = {"ok": result.accepted, "delivery_id": result.delivery_id}
    if result.decision == "held":
        body["status"] = "held for approval"
    elif not result.accepted:
        # Deliberately not result.reason: see the module docstring.
        body["error"] = "refused"
    return jsonify(body), result.status


@webhook_bp.get("/api/webhooks/pending")
def webhook_pending():
    denied = _local_only()
    if denied is not None:
        return denied
    return jsonify({"ok": True, "pending": _ingress().pending()})


@webhook_bp.get("/api/webhooks/history")
def webhook_history():
    denied = _local_only()
    if denied is not None:
        return denied
    route = request.args.get("route") or None
    decision = request.args.get("decision") or None
    try:
        history = _ingress().history(route=route, decision=decision)
    except WebhookIngressError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "history": history})


@webhook_bp.post("/api/webhooks/<delivery_id>/approve")
def webhook_approve(delivery_id: str):
    denied = _local_only()
    if denied is not None:
        return denied
    payload = request.get_json(silent=True) or {}
    actor = str(payload.get("actor") or request.form.get("actor") or "").strip()
    if not actor:
        return jsonify({"ok": False, "error": "approving requires a named actor"}), 400
    try:
        result = _ingress().approve(delivery_id, actor=actor)
    except KeyError:
        return jsonify({"ok": False, "error": "unknown delivery"}), 404
    except WebhookIngressError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": result.accepted, "result": result.to_dict()}), result.status
