from types import SimpleNamespace

from flask import Flask

from ui.workspace_web import workspace_blueprint


def test_missing_sdk_returns_json_without_starting_another_owner():
    calls = []

    def attach(sid):
        calls.append(sid)
        raise ModuleNotFoundError("No module named 'claude_agent_sdk'")

    app = Flask(__name__)
    app.register_blueprint(workspace_blueprint(SimpleNamespace(attach=attach), token="x" * 32))
    response = app.test_client().post(
        "/api/workspace/exact/attach", json={},
        headers={"X-Serena-Workspace-Token": "x" * 32},
    )
    assert response.status_code == 503
    assert response.is_json
    assert "missing a required coding runtime" in response.json["error"]
    assert calls == ["exact"]
