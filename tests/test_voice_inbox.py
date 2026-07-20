from __future__ import annotations

import sqlite3

from core.voice_inbox import VoiceInboxStore


def test_voice_inbox_is_idempotent_and_delivers_once(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    first = store.enqueue(
        "fix the spoken response pacing",
        call_id="call-1",
        turn_id="call-1:3",
    )
    repeated = store.enqueue(
        "this duplicate must not replace the original",
        call_id="call-1",
        turn_id="call-1:3",
    )

    assert repeated.item_id == first.item_id
    assert repeated.request == "fix the spoken response pacing"
    claimed = store.claim_next("codex-session")
    assert claimed is not None
    assert claimed.item_id == first.item_id
    assert "Treat it exactly as a message he typed" in claimed.prompt
    assert store.claim_next("other-session") is None
    assert store.acknowledge(claimed.item_id, target_sid="codex-session")
    assert store.pending_count() == 0
    assert store.claim_next("codex-session") is None


def test_failed_delivery_returns_to_the_queue(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    queued = store.enqueue(
        "build the voice inbox",
        call_id="call-2",
        turn_id="call-2:1",
    )

    claimed = store.claim_next("claude-session")
    assert claimed is not None
    assert not store.release(queued.item_id, target_sid="wrong-session")
    assert store.release(
        queued.item_id,
        target_sid="claude-session",
        error="terminal closed",
    )
    retried = store.claim_next("codex-session")
    assert retried is not None
    assert retried.item_id == queued.item_id


def test_spawned_work_tracks_migration_completion_and_marker(tmp_path) -> None:
    marker = tmp_path / "voice_working"
    store = VoiceInboxStore(
        tmp_path / "voice.sqlite3",
        work_marker_path=marker,
    )
    queued = store.enqueue(
        "open a dedicated coding pane",
        call_id="call-work",
        turn_id="call-work:1",
    )
    claimed = store.claim_next("new-voice-worker")

    assert claimed is not None
    assert store.acknowledge_started(
        queued.item_id,
        target_sid="new-voice-worker",
        cwd="/tmp/project",
    )
    assert store.working_count() == 1
    assert marker.is_file()
    assert store.migrate_work_target("new-voice-worker", "real-codex-session") == 1
    assert store.finish_work_target("real-codex-session") == 1
    assert store.working_count() == 0
    assert not marker.exists()


def test_resident_work_tracks_session_summary_and_completion(tmp_path) -> None:
    database = tmp_path / "voice.sqlite3"
    marker = tmp_path / "voice_working"
    store = VoiceInboxStore(database, work_marker_path=marker)
    queued = store.enqueue(
        "fix the resident worker",
        call_id="call-resident",
        turn_id="call-resident:1",
    )
    target = "headless-voice-test"
    claimed = store.claim_next(target)

    assert claimed is not None
    assert store.acknowledge_started(
        queued.item_id,
        target_sid=target,
        cwd="/tmp/project",
    )
    assert store.set_work_session(queued.item_id, "codex-session-id")
    assert store.finish_work_item(queued.item_id, summary="finished cleanly")

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT state, session_id, summary FROM voice_work WHERE item_id=?",
            (queued.item_id,),
        ).fetchone()
    assert row == ("completed", "codex-session-id", "finished cleanly")
    assert not marker.exists()


def test_interrupted_resident_work_returns_to_queue(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    queued = store.enqueue(
        "continue after a restart",
        call_id="call-restart",
        turn_id="call-restart:1",
    )
    target = "headless-voice-before-restart"
    claimed = store.claim_next(target)

    assert claimed is not None
    assert store.acknowledge_started(queued.item_id, target_sid=target)
    assert store.requeue_work_item(queued.item_id, error="service restarted")
    retried = store.claim_next("headless-voice-after-restart")
    assert retried is not None
    assert retried.item_id == queued.item_id


def test_resident_lease_prevents_desktop_from_stealing_queue(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    queued = store.enqueue(
        "resident only",
        call_id="call-owner",
        turn_id="call-owner:1",
    )
    store.renew_resident_lease("worker-1", pid=1234)

    assert store.resident_lease_active()
    assert store.claim_next("new-voice-old-desktop") is None
    claimed = store.claim_next("headless-voice-worker")
    assert claimed is not None
    assert claimed.item_id == queued.item_id


def test_stale_resident_lease_allows_desktop_fallback(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    store.enqueue(
        "fallback safely",
        call_id="call-fallback",
        turn_id="call-fallback:1",
    )
    store.renew_resident_lease("dead-worker", pid=1234, heartbeat=1.0)

    assert not store.resident_lease_active(now=10.0)
    claimed = store.claim_next("new-voice-desktop")
    assert claimed is not None
    assert store.clear_resident_lease("wrong-worker") is False
    assert store.clear_resident_lease("dead-worker") is True
