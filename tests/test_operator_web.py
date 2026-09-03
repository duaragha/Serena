from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask

from core.artifacts import ArtifactRegistry
from core.operator_workspace import OperatorWorkspaceStore
from ui import operator_web


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = OperatorWorkspaceStore(tmp_path / "operator.sqlite3")
    monkeypatch.setattr(operator_web, "_store", lambda: store)
    app = Flask(__name__)
    app.register_blueprint(operator_web.operator_bp)
    app.config.update(TESTING=True)
    return app.test_client(), store


def test_prompt_api_edits_and_transitions_durable_queue(client):
    http, store = client
    queued = http.post(
        "/api/operator/prompts",
        json={
            "session_id": "session-1",
            "provider": "codex",
            "text": "draft",
            "mode": "next_turn",
        },
    )
    assert queued.status_code == 200
    prompt_id = queued.get_json()["prompt"]["prompt_id"]

    edited = http.put(f"/api/operator/prompts/{prompt_id}", json={"text": "final"})
    paused = http.post(f"/api/operator/prompts/{prompt_id}/pause")
    resumed = http.post(f"/api/operator/prompts/{prompt_id}/resume")
    listing = http.get("/api/operator/prompts?session_id=session-1")

    assert edited.get_json()["prompt"]["revision"] == 2
    assert paused.get_json()["prompt"]["state"] == "paused"
    assert resumed.get_json()["prompt"]["state"] == "queued"
    assert listing.get_json()["prompts"][0]["text"] == "final"
    assert store.get(prompt_id).state == "queued"


def test_dispatch_uses_renderer_runtime_without_changing_layout(client, monkeypatch):
    from ui import pty_terminal

    http, store = client
    prompt = store.queue_prompt(
        session_id="session-1",
        provider="codex",
        text="correct this now",
        mode="correction",
    )
    writes: list[tuple[str, bytes]] = []
    monkeypatch.setattr(pty_terminal, "tid_for_session", lambda _sid: "terminal-1")
    monkeypatch.setattr(
        pty_terminal,
        "runtime_context_snapshot",
        lambda: {
            "focused_sid": "session-1",
            "split_pair": ["session-1", "session-2"],
            "runtimes": [
                {
                    "sid": "session-1",
                    "terminal_id": "terminal-1",
                    "agent": "codex",
                    "alive": True,
                    "state": "live",
                    "busy": True,
                    "draft": False,
                    "reserved": False,
                }
            ],
        },
    )
    monkeypatch.setattr(
        pty_terminal,
        "write_operator_prompt",
        lambda terminal_id, data, mode: (
            writes.append((terminal_id, data))
            or {
                "supported": True,
                "delivered": True,
                "reason": "prompt delivered to the native runtime",
                "wakes_runtime": False,
                "mode": mode,
            }
        ),
    )
    monkeypatch.setattr(
        pty_terminal,
        "resume",
        lambda _terminal_id: pytest.fail("a live correction must not resume the runtime"),
    )

    response = http.post(f"/api/operator/prompts/{prompt.prompt_id}/dispatch")

    assert response.status_code == 200
    assert writes == [("terminal-1", b"correct this now\r")]
    assert store.get(prompt.prompt_id).state == "sent"


def test_sleeping_runtime_only_wakes_for_explicit_next_turn(client, monkeypatch):
    from ui import pty_terminal

    http, store = client
    prompt = store.queue_prompt(
        session_id="session-1",
        provider="claude",
        text="next turn",
        mode="next_turn",
    )
    resumed: list[str] = []
    monkeypatch.setattr(pty_terminal, "tid_for_session", lambda _sid: "terminal-1")
    monkeypatch.setattr(
        pty_terminal,
        "runtime_context_snapshot",
        lambda: {
            "focused_sid": None,
            "split_pair": [],
            "runtimes": [
                {
                    "sid": "session-1",
                    "terminal_id": "terminal-1",
                    "agent": "claude",
                    "alive": True,
                    "state": "paused",
                    "busy": False,
                    "draft": False,
                    "reserved": False,
                }
            ],
        },
    )
    monkeypatch.setattr(
        pty_terminal,
        "write_operator_prompt",
        lambda terminal_id, _data, _mode: (
            resumed.append(terminal_id)
            or {
                "supported": True,
                "delivered": True,
                "reason": "prompt delivered to the native runtime",
                "wakes_runtime": True,
            }
        ),
    )

    response = http.post(f"/api/operator/prompts/{prompt.prompt_id}/dispatch")

    assert response.status_code == 200
    assert resumed == ["terminal-1"]


def test_dispatch_rechecks_runtime_state_at_the_write_boundary(client, monkeypatch):
    from ui import pty_terminal

    http, store = client
    prompt = store.queue_prompt(
        session_id="session-1",
        provider="codex",
        text="next turn",
        mode="next_turn",
    )
    monkeypatch.setattr(pty_terminal, "tid_for_session", lambda _sid: "terminal-1")
    monkeypatch.setattr(
        pty_terminal,
        "runtime_context_snapshot",
        lambda: {
            "runtimes": [
                {
                    "terminal_id": "terminal-1",
                    "agent": "codex",
                    "alive": True,
                    "state": "live",
                    "busy": False,
                    "draft": False,
                    "reserved": False,
                }
            ]
        },
    )
    monkeypatch.setattr(
        pty_terminal,
        "write_operator_prompt",
        lambda *_args: {
            "supported": False,
            "delivered": False,
            "reason": "wait for the active turn or queue a correction",
        },
    )

    response = http.post(f"/api/operator/prompts/{prompt.prompt_id}/dispatch")

    assert response.status_code == 409
    assert "active turn" in response.get_json()["error"]
    assert store.get(prompt.prompt_id).state == "queued"


def test_atomic_operator_write_refuses_a_stale_next_turn(monkeypatch):
    from ui import pty_terminal

    class FakeProcess:
        def __init__(self):
            self.writes: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.writes.append(data)

    process = FakeProcess()
    terminal = pty_terminal.Terminal(
        id="terminal-1",
        proc=process,
        cols=80,
        rows=24,
        agent="codex",
        runtime_busy=True,
    )
    monkeypatch.setattr(pty_terminal, "get", lambda _tid: terminal)

    result = pty_terminal.write_operator_prompt(
        "terminal-1", b"must not land\r", "next_turn"
    )

    assert result["supported"] is False
    assert result["delivered"] is False
    assert process.writes == []


def test_unsupported_provider_correction_is_refused_and_remains_queued(
    client, monkeypatch
):
    from ui import pty_terminal

    http, store = client
    prompt = store.queue_prompt(
        session_id="session-1",
        provider="claude",
        text="unsafe correction",
        mode="correction",
    )
    monkeypatch.setattr(pty_terminal, "tid_for_session", lambda _sid: "terminal-1")
    monkeypatch.setattr(
        pty_terminal,
        "runtime_context_snapshot",
        lambda: {
            "runtimes": [
                {
                    "terminal_id": "terminal-1",
                    "agent": "claude",
                    "alive": True,
                    "state": "live",
                    "busy": True,
                    "draft": False,
                    "reserved": False,
                }
            ]
        },
    )
    monkeypatch.setattr(
        pty_terminal,
        "write_operator_prompt",
        lambda *_args: pytest.fail("unsupported steering must not write to the PTY"),
    )

    response = http.post(f"/api/operator/prompts/{prompt.prompt_id}/dispatch")

    assert response.status_code == 409
    assert "no verified safe" in response.get_json()["error"]
    assert store.get(prompt.prompt_id).state == "queued"


def test_inspection_and_gallery_are_loopback_only(client, tmp_path, monkeypatch):
    import core.artifacts as artifacts_module

    http, _store = client
    monkeypatch.setattr(
        operator_web,
        "inspect_session",
        lambda sid: {"session_id": sid, "focus": {"focused": True}},
    )
    inspection = http.get("/api/operator/sessions/session-1/inspect")
    denied = http.get(
        "/api/operator/sessions/session-1/inspect",
        environ_base={"REMOTE_ADDR": "192.0.2.9"},
    )
    assert inspection.get_json()["inspection"]["focus"]["focused"] is True
    assert denied.status_code == 403

    registry = ArtifactRegistry(
        root=tmp_path / "artifacts",
        db_path=tmp_path / "artifacts.sqlite3",
        key_path=tmp_path / "artifact.key",
    )
    job_id = "22222222-2222-4222-8222-222222222222"
    path = registry.write_job_artifact(job_id=job_id, name="proof.txt", content="proof")
    registry.register(
        job_id=job_id,
        path=path,
        name="proof.txt",
        origin_session_id="session-1",
        fleet_run_id="run-1",
        fleet_worker_key="codex:a",
    )
    monkeypatch.setattr(artifacts_module, "get_default_artifact_registry", lambda: registry)
    gallery = http.get("/api/operator/artifacts?q=codex")
    item = gallery.get_json()["artifacts"][0]
    assert item["origin_session_id"] == "session-1"
    assert item["fleet_run_id"] == "run-1"
    assert item["url"].startswith("/artifacts/")
    assert "path" not in item


def test_command_palette_preserves_renderer_owned_terminal_boundaries():
    root = Path(__file__).resolve().parents[1]
    javascript = (root / "ui" / "static" / "operator_workspace.js").read_text(
        encoding="utf-8"
    )
    web = (root / "ui" / "web.py").read_text(encoding="utf-8")

    assert "Ctrl/Cmd+K" in javascript
    assert "openConv(artifact.origin_session_id)" in javascript
    assert "openFleetRun(artifact.fleet_run_id)" in javascript
    assert "typeof fleetWindow.selectRun !== 'function'" in javascript
    assert "fleetWindow.selectRun(runId)" in javascript
    assert "frame.addEventListener('load', selectRun, { once: true })" in javascript
    assert "switchTab('fleet')" in javascript
    assert "toggleFocusMode()" in javascript
    assert "fitTerminal" not in javascript
    assert "gtkSend" not in javascript
    assert "app.register_blueprint(operator_bp)" in web
    assert 'src="/static/operator_workspace.js"' in web
