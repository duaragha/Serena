from __future__ import annotations

from ui import pty_terminal
from ui import web
from ui.web import app


def test_spawn_terminal_strips_metered_auth_from_both_runtimes(monkeypatch) -> None:
    captured: list[dict[str, str]] = []

    def fake_spawn(_argv, **kwargs):
        captured.append(kwargs["env"])
        return f"terminal-{len(captured)}"

    monkeypatch.setattr(pty_terminal, "spawn", fake_spawn)
    monkeypatch.setattr(pty_terminal, "get", lambda _tid: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-claude")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-codex")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-reach-codex")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "subscription-auth")

    client = app.test_client()
    for agent in ("claude", "codex"):
        response = client.post(
            "/api/spawn-terminal",
            json={"agent": agent, "cwd": "/tmp"},
        )
        assert response.status_code == 200

    assert len(captured) == 2
    for environment in captured:
        assert "ANTHROPIC_API_KEY" not in environment
        assert "OPENAI_API_KEY" not in environment
        assert "CODEX_API_KEY" not in environment
        assert environment["CLAUDE_CODE_OAUTH_TOKEN"] == "subscription-auth"


def test_spawn_terminal_refuses_session_owned_by_external_workflow(monkeypatch) -> None:
    monkeypatch.setattr(
        web,
        "get_session",
        lambda _sid: {
            "session_id": "workflow-session",
            "agent": "codex",
            "cwd": "/tmp",
        },
    )
    monkeypatch.setattr(web, "_external_runtime_active", lambda _sid: True)
    monkeypatch.setattr(
        pty_terminal,
        "spawn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("duplicate runtime must not spawn")
        ),
    )

    response = app.test_client().post(
        "/api/spawn-terminal",
        json={"session_id": "workflow-session"},
    )

    assert response.status_code == 409
    assert response.get_json()["external_runtime"] is True
