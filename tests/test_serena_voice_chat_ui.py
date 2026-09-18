from __future__ import annotations

from ui import web


def test_permanent_serena_chat_has_its_own_badge_and_top_section() -> None:
    html = web.HTML

    assert "const SERENA_VOICE_SESSION_ID = 'serena-voice-main'" in html
    assert "normalized === 'serena-voice'" in html
    assert 'agent-icon serena" title="Serena"' in html
    assert '<div class="group-header serena-header">Serena</div>' in html
    assert html.index("if (serenaVoice.length)") < html.index("if (active.length)")


def test_permanent_serena_chat_survives_project_filters(monkeypatch) -> None:
    serena = {"session_id": "serena-voice-main", "agent": "serena-voice"}
    ordinary = {"session_id": "project-chat", "agent": "claude"}
    monkeypatch.setattr(web, "get_session", lambda _sid: serena)

    assert web._include_permanent_serena_session([ordinary]) == [serena, ordinary]
    assert web._include_permanent_serena_session([serena, ordinary]) == [
        serena,
        ordinary,
    ]


def test_permanent_serena_chat_cannot_enter_a_resumable_code_view() -> None:
    html = web.HTML

    assert "const readOnly = externallyRunning || serenaVoice" in html
    assert "'hidden', serenaVoice || fleetWorker" in html
    assert "if (mode === 'live' && (_isSerenaVoiceSession" in html
    assert "if (!opts.isNew && (_isSerenaVoiceSession" in html
    assert "if (_isSerenaVoiceSession(local || sid) || _isFleetSession(local))" in html


def test_permanent_serena_chat_cannot_be_deleted_or_resumed(monkeypatch, tmp_path) -> None:
    transcript = tmp_path / "serena-main.jsonl"
    transcript.touch()
    session = {
        "session_id": "serena-voice-main",
        "agent": "serena-voice",
        "file_path": str(transcript),
        "display_title": "Serena",
    }
    monkeypatch.setattr(web, "get_session", lambda _sid: session)
    monkeypatch.setattr(
        web,
        "delete_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not delete")),
    )
    client = web.app.test_client()

    assert client.delete("/api/session/serena-voice-main").status_code == 403
    assert client.post("/api/sessions/bulk-delete", json={"ids": ["serena-voice-main"]}).status_code == 200
    assert client.post("/api/resume/serena-voice-main").status_code == 409
    assert client.post("/api/spawn-terminal", json={"session_id": "serena-voice-main"}).status_code == 409


def _script_line(needle: str) -> str:
    """The one line of the page's inline script that does `needle`.

    These used to be whole-expression substring matches, which meant adding an
    unrelated clause to a condition failed the test without anything about the
    behaviour having changed. Asserting on the operands of the right line says
    what actually matters and survives the next clause.
    """

    lines = [line.strip() for line in web.HTML.splitlines() if needle in line]
    assert len(lines) == 1, f"expected exactly one line doing {needle!r}, got {len(lines)}"
    return lines[0]


def test_serena_read_view_polls_for_new_turns() -> None:
    """A Serena transcript grows from outside this process, so read mode polls."""

    scheduling = _script_line(") _scheduleExternalReadRefresh(sid);")
    assert "_isSerenaVoiceSession(data.agent || sid)" in scheduling
    assert "convMode === 'read'" in scheduling


def test_voice_conversation_api_exposes_agent_for_serena_label(
    monkeypatch, tmp_path
) -> None:
    transcript = tmp_path / "serena-main.jsonl"
    transcript.touch()
    session = {
        "session_id": "serena-voice-main",
        "agent": "serena-voice",
        "file_path": str(transcript),
        "display_title": "Serena",
        "first_timestamp": "2026-08-02T12:00:00Z",
    }
    monkeypatch.setattr(web, "get_session", lambda _sid: session)
    monkeypatch.setattr(web, "parse_full", lambda _path: [])
    monkeypatch.setattr(web, "_external_runtime_active", lambda _sid: False)

    response = web.app.test_client().get("/api/conversation/serena-voice-main")

    assert response.status_code == 200
    assert response.get_json()["agent"] == "serena-voice"
    # ...and the page turns that agent into her name, not Claude's.
    label = _script_line("const defaultAgentLabel")
    assert "data.agent === 'serena-voice'" in label and "'Serena'" in label
    assert "data.agent === 'codex' ? 'Codex'" in _script_line("? 'Codex'")
