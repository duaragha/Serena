"""The loopback approvals surface: list pending, approve, deny.

The broker pushes each confirmation to exactly one channel, but the web UI
always lists everything pending — and as a typed surface it is one of the
three places a tier-4 (secret) confirmation can be cleared.

Shape copies `ui.webhook_web`: the blueprint carries bytes, the broker
decides, and the management endpoints are loopback-only.
"""

from __future__ import annotations

import ipaddress
import threading
import time
from typing import Any

from flask import Blueprint, jsonify, request
from markupsafe import escape

from core.action_authority import TIER_NAMES, ActionAuthorityError

approvals_bp = Blueprint("approvals", __name__)

_BROKER: Any = None
_BROKER_LOCK = threading.Lock()


def _broker() -> Any:
    """One broker per process, so nonces and the authority are shared."""

    from core.approvals import ApprovalBroker

    global _BROKER
    with _BROKER_LOCK:
        if _BROKER is None:
            _BROKER = ApprovalBroker()
        return _BROKER


def _reset_broker_for_tests(broker: Any = None) -> None:
    """Point the blueprint at a temporary broker. Only tests call this."""

    global _BROKER
    with _BROKER_LOCK:
        _BROKER = broker


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


def _pending_rows(moment: float) -> list[dict[str, Any]]:
    rows = []
    for confirmation in _broker().authority.pending_confirmations(now=moment):
        rows.append({
            "id": confirmation.confirmation_id,
            "capability": confirmation.capability,
            "target": confirmation.target,
            "tier": confirmation.tier,
            "tier_name": TIER_NAMES.get(confirmation.tier, "unknown"),
            "prompt": confirmation.prompt,
            "age_seconds": round(moment - confirmation.requested_at, 1),
            "expires_in_seconds": round(
                max(0.0, confirmation.expires_at - moment), 1),
        })
    return rows


@approvals_bp.get("/api/approvals/pending")
def approvals_pending():
    denied = _local_only()
    if denied is not None:
        return denied
    return jsonify({"ok": True, "pending": _pending_rows(time.time())})


@approvals_bp.get("/approvals")
def approvals_page():
    """The pending list as a page: capability, target, tier, age, buttons."""

    denied = _local_only()
    if denied is not None:
        return denied
    moment = time.time()
    items = []
    for row in _pending_rows(moment):
        items.append(
            f"<li><strong>{escape(row['capability'])}</strong> — "
            f"{escape(row['target'] or '—')} "
            f"<em>(tier {row['tier']} {escape(row['tier_name'])}, "
            f"age {row['age_seconds']}s, "
            f"expires in {row['expires_in_seconds']}s)</em><br>"
            f"{escape(row['prompt'])}<br>"
            f"<form method='post' action='/api/approvals/{row['id']}/approve' "
            f"style='display:inline'>"
            f"<button type='submit'>approve</button></form> "
            f"<form method='post' action='/api/approvals/{row['id']}/deny' "
            f"style='display:inline'>"
            f"<button type='submit'>deny</button></form></li>")
    body = ("<ul>" + "".join(items) + "</ul>") if items else "<p>nothing pending.</p>"
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>approvals</title></head><body><h1>approvals</h1>"
            f"{body}</body></html>")


def _answer(confirmation_id: str, approved: bool):
    denied = _local_only()
    if denied is not None:
        return denied
    payload = request.get_json(silent=True) or {}
    actor = str(payload.get("actor") or request.form.get("actor")
                or "raghav").strip() or "raghav"
    try:
        record = _broker().answer_confirmation(
            confirmation_id, approved=approved, surface="ui", actor=actor)
    except ActionAuthorityError as error:
        message = str(error)
        if "unknown confirmation" in message:
            return jsonify({"ok": False, "error": "unknown confirmation"}), 404
        return jsonify({"ok": False, "error": message}), 400
    # First wins underneath: an approve landing on an already-denied
    # confirmation (or the reverse) is a conflict, not a success.
    ok_states = {"approved", "used"} if approved else {"denied"}
    if record.state not in ok_states:
        return jsonify({"ok": False, "error": "already answered",
                        "id": record.confirmation_id,
                        "state": record.state}), 409
    return jsonify({"ok": True, "id": record.confirmation_id,
                    "state": record.state,
                    "resolved_by": record.resolved_by})


@approvals_bp.post("/api/approvals/<confirmation_id>/approve")
def approvals_approve(confirmation_id: str):
    return _answer(confirmation_id, True)


@approvals_bp.post("/api/approvals/<confirmation_id>/deny")
def approvals_deny(confirmation_id: str):
    return _answer(confirmation_id, False)
