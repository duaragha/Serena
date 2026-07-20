from __future__ import annotations

import pytest

from core import voice_inbox, voice_work_supervisor
from core.voice_inbox import VoiceInboxStore
from ui import pty_terminal
from ui.web import app


@pytest.fixture(autouse=True)
def _no_resident_worker(monkeypatch) -> None:
    monkeypatch.setattr(
        voice_work_supervisor,
        "resident_worker_available",
        lambda: False,
    )


def test_voice_inbox_http_claim_and_ack(tmp_path, monkeypatch) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    queued = store.enqueue(
        "fix the spoken task bridge",
        call_id="call-web",
        turn_id="call-web:1",
    )
    monkeypatch.setattr(voice_inbox, "_DEFAULT_STORE", store)
    monkeypatch.setattr(
        pty_terminal,
        "tid_for_session",
        lambda sid: "terminal-1" if sid == "codex-session" else None,
    )

    client = app.test_client()
    claim = client.post(
        "/api/voice-inbox/claim",
        json={"target_sid": "codex-session"},
    )
    assert claim.status_code == 200
    item = claim.get_json()["item"]
    assert item["item_id"] == queued.item_id
    assert "spoken task bridge" in item["prompt"]

    ack = client.post(
        f"/api/voice-inbox/{queued.item_id}/ack",
        json={"target_sid": "codex-session"},
    )
    assert ack.status_code == 200
    assert store.pending_count() == 0


def test_voice_inbox_http_does_not_claim_without_a_live_terminal(
    tmp_path,
    monkeypatch,
) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    store.enqueue(
        "build the next part",
        call_id="call-wait",
        turn_id="call-wait:1",
    )
    monkeypatch.setattr(voice_inbox, "_DEFAULT_STORE", store)
    monkeypatch.setattr(pty_terminal, "tid_for_session", lambda _sid: None)

    response = app.test_client().post(
        "/api/voice-inbox/claim",
        json={"target_sid": "closed-session"},
    )

    assert response.status_code == 409
    assert store.pending_count() == 1


def test_voice_inbox_http_can_reserve_and_start_a_spawned_pane(
    tmp_path,
    monkeypatch,
) -> None:
    marker = tmp_path / "voice_working"
    store = VoiceInboxStore(
        tmp_path / "voice.sqlite3",
        work_marker_path=marker,
    )
    queued = store.enqueue(
        "spawn codex now",
        call_id="call-spawn",
        turn_id="call-spawn:1",
    )
    monkeypatch.setattr(voice_inbox, "_DEFAULT_STORE", store)
    monkeypatch.setattr(pty_terminal, "tid_for_session", lambda _sid: None)
    client = app.test_client()

    claim = client.post(
        "/api/voice-inbox/claim",
        json={"target_sid": "new-voice-pane", "spawn": True},
    )
    assert claim.status_code == 200
    assert claim.get_json()["item"]["item_id"] == queued.item_id

    started = client.post(
        f"/api/voice-inbox/{queued.item_id}/start",
        json={"target_sid": "new-voice-pane", "cwd": "/tmp/project"},
    )
    assert started.status_code == 200
    assert store.pending_count() == 0
    assert store.working_count() == 1
    assert marker.is_file()


def test_voice_inbox_http_yields_to_resident_worker(tmp_path, monkeypatch) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    store.enqueue(
        "keep this for the resident worker",
        call_id="call-resident",
        turn_id="call-resident:1",
    )
    monkeypatch.setattr(voice_inbox, "_DEFAULT_STORE", store)
    monkeypatch.setattr(
        voice_work_supervisor,
        "resident_worker_available",
        lambda: True,
    )

    response = app.test_client().post(
        "/api/voice-inbox/claim",
        json={"target_sid": "new-voice-pane", "spawn": True},
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "item": None, "resident": True}
    assert store.pending_count() == 1
