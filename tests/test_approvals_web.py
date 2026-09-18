"""Spec: approval routes, Phase 2 — loopback approvals web surface."""

import pytest
from flask import Flask

from core.action_authority import (
    TIER_IRREVERSIBLE,
    TIER_SECRET,
    ActionAuthority,
)


@pytest.fixture()
def authority(tmp_path):
    return ActionAuthority(
        tmp_path / "action.sqlite3",
        audit_path=tmp_path / "action.jsonl",
        publish_events=False,
    )


@pytest.fixture()
def client(tmp_path, authority, monkeypatch):
    from core import approvals
    from ui import approvals_web

    monkeypatch.setenv("SERENA_CONTROL_PLANE_DB_PATH",
                       str(tmp_path / "control.sqlite3"))
    broker = approvals.ApprovalBroker(
        path=tmp_path / "approvals.sqlite3", authority=authority,
        notifications=None, in_call=lambda: False)
    approvals_web._reset_broker_for_tests(broker)
    app = Flask(__name__)
    app.register_blueprint(approvals_web.approvals_bp)
    return app.test_client()


def _pending(authority, **overrides):
    import time

    fields = {"capability": "data.delete_one", "target": "old-cache",
              "tier": TIER_IRREVERSIBLE, "ttl_seconds": 600,
              "now": time.time()}
    fields.update(overrides)
    return authority.request_confirmation(**fields)


def test_pending_lists_capability_target_tier_and_age(client, authority):
    record = _pending(authority)
    response = client.get("/api/approvals/pending")
    assert response.status_code == 200
    (entry,) = response.get_json()["pending"]
    assert entry["id"] == record.confirmation_id
    assert entry["capability"] == "data.delete_one"
    assert entry["target"] == "old-cache"
    assert entry["tier"] == TIER_IRREVERSIBLE
    assert entry["tier_name"] == "irreversible"
    assert entry["age_seconds"] >= 0
    assert "prompt" in entry


def test_page_lists_pending_with_buttons(client, authority):
    record = _pending(authority)
    response = client.get("/approvals")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "data.delete_one" in html
    assert "old-cache" in html
    assert "irreversible" in html
    assert f"/api/approvals/{record.confirmation_id}/approve" in html
    assert f"/api/approvals/{record.confirmation_id}/deny" in html
    remote = {"REMOTE_ADDR": "192.0.2.1"}
    assert client.get("/approvals",
                      environ_overrides=remote).status_code == 403


def test_non_loopback_is_refused(client, authority):
    _pending(authority)
    remote = {"REMOTE_ADDR": "192.0.2.1"}
    assert client.get("/api/approvals/pending",
                      environ_overrides=remote).status_code == 403
    assert client.post("/api/approvals/x/approve",
                       environ_overrides=remote).status_code == 403
    assert client.post("/api/approvals/x/deny",
                       environ_overrides=remote).status_code == 403


def test_approve_and_deny_resolve_exactly_once(client, authority):
    first = _pending(authority)
    second = _pending(authority, target="other-cache")
    approved = client.post(f"/api/approvals/{first.confirmation_id}/approve",
                           json={"actor": "raghav"})
    assert approved.status_code == 200
    assert approved.get_json()["state"] == "approved"
    denied = client.post(f"/api/approvals/{second.confirmation_id}/deny")
    assert denied.status_code == 200
    assert denied.get_json()["state"] == "denied"
    # Networks retry the same answer: still a success, first wins.
    repeat = client.post(f"/api/approvals/{first.confirmation_id}/approve")
    assert repeat.status_code == 200
    assert repeat.get_json()["state"] == "approved"
    assert authority.confirmation(first.confirmation_id).resolved_by == (
        "raghav via ui")
    # But a contradicting answer is a conflict, not a success.
    conflict = client.post(f"/api/approvals/{first.confirmation_id}/deny")
    assert conflict.status_code == 409
    assert conflict.get_json()["state"] == "approved"
    assert authority.confirmation(first.confirmation_id).state == "approved"


def test_ui_clears_tier_4(client, authority):
    record = _pending(authority, tier=TIER_SECRET, target="api-key")
    response = client.post(f"/api/approvals/{record.confirmation_id}/approve")
    assert response.status_code == 200
    assert response.get_json()["state"] == "approved"


def test_unknown_confirmation_is_404(client):
    assert client.post("/api/approvals/nope/approve").status_code == 404
    assert client.post("/api/approvals/nope/deny").status_code == 404


def test_blueprint_is_registered_on_the_app():
    """ui.web cannot be imported under the worker sandbox (its workspace
    journal lives outside the writable roots, which breaks every ui.web
    import here), so assert the registration lines directly."""

    from pathlib import Path

    source = Path("ui/web.py").read_text(encoding="utf-8")
    assert "from ui.approvals_web import approvals_bp" in source
    assert "app.register_blueprint(approvals_bp)" in source
