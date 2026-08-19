from __future__ import annotations

import pytest

from core import voice_inbox
from core.voice_inbox import VoiceInboxStore
from ui.web import app


@pytest.mark.parametrize("target_sid", ["codex-session", "new-voice-pane"])
def test_web_shell_cannot_claim_or_spawn_voice_coding_jobs(
    tmp_path, monkeypatch, target_sid: str
) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    store.enqueue(
        "fix the spoken task bridge",
        call_id="call-web",
        turn_id="call-web:1",
    )
    monkeypatch.setattr(voice_inbox, "_DEFAULT_STORE", store)

    response = app.test_client().post(
        "/api/voice-inbox/claim",
        json={"target_sid": target_sid, "spawn": True},
    )

    assert response.status_code == 410
    assert "resident supervisor" in response.get_json()["error"]
    assert store.pending_count() == 1


@pytest.mark.parametrize("action", ["ack", "start", "release"])
def test_web_shell_cannot_acknowledge_or_finish_resident_jobs(
    tmp_path, monkeypatch, action: str
) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    item = store.enqueue(
        "fix the spoken task bridge",
        call_id="call-web",
        turn_id="call-web:1",
    )
    monkeypatch.setattr(voice_inbox, "_DEFAULT_STORE", store)

    response = app.test_client().post(
        f"/api/voice-inbox/{item.item_id}/{action}",
        json={"target_sid": "new-voice-pane"},
    )

    assert response.status_code == 410
    assert store.pending_count() == 1
