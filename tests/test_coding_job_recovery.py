"""Focused durable lifecycle tests for coding-job automatic recovery."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from core import voice_inbox, voice_work_supervisor
from core.voice_inbox import (
    AUTOMATIC_RECOVERY_EVENT,
    AUTOMATIC_RECOVERY_TERMINAL_EVENT,
    VoiceInboxStore,
)


def _working(
    store: VoiceInboxStore,
    name: str,
    *,
    session_id: str = "persisted-session",
) -> tuple[str, str]:
    item = store.enqueue(
        f"work on {name}",
        call_id=f"call-{name}",
        turn_id=f"turn-{name}",
    )
    target = f"headless-voice-{name}"
    claimed = store.claim_next(target)
    assert claimed is not None
    assert store.acknowledge_started(item.item_id, target_sid=target, cwd="/tmp/project")
    if session_id:
        assert store.set_work_session(item.item_id, session_id)
    return item.item_id, target


def _queue(
    store: VoiceInboxStore,
    item_id: str,
    *,
    error: str,
    kind: str = "evidence",
    active_recovery_id: str = "",
) -> dict:
    return store.queue_automatic_recovery(
        item_id,
        error=error,
        kind=kind,
        max_recoveries=3,
        active_recovery_id=active_recovery_id,
    )


def test_recovery_is_delayed_then_continues_the_same_logical_job(
    tmp_path, monkeypatch
) -> None:
    clock = [1_000.0]
    monkeypatch.setattr(voice_inbox.time, "time", lambda: clock[0])
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    item_id, _target = _working(store, "continued")

    queued = _queue(
        store,
        item_id,
        error="mechanical completion evidence is incomplete: tests failed",
    )

    assert queued["queued"] is True
    assert queued["recoveries"] == 1
    latest = store.latest_automatic_recovery(item_id)
    assert latest["eligible_at"] == 1_002.0
    assert latest["state"] == "scheduled"
    assert VoiceInboxStore(store.path).latest_automatic_recovery(item_id)["state"] == (
        "scheduled"
    )
    assert store.work_record(item_id)["state"] == "resume_queued"
    assert store.claim_next("headless-voice-too-early") is None

    clock[0] = 1_002.0
    resumed = store.claim_next("headless-voice-recovery")
    assert resumed is not None
    assert resumed.item_id == item_id
    assert store.acknowledge_started(
        item_id,
        target_sid="headless-voice-recovery",
        cwd="/tmp/project",
    )
    latest = store.latest_automatic_recovery(item_id)
    assert latest is not None
    assert latest["state"] == "running"
    assert latest["kind"] == "evidence"
    assert latest["error"].endswith("tests failed")
    assert store.finish_work_item(item_id, summary="recovered job completed")
    assert store.latest_automatic_recovery(item_id)["state"] == "succeeded"


def test_duplicate_recovery_requests_create_one_durable_continuation(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    item_id, _target = _working(store, "duplicate")

    def queue_once() -> dict:
        return _queue(store, item_id, error="Codex exited with status 1", kind="provider")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _value: queue_once(), range(2)))

    assert sum(result.get("queued") is True for result in results) == 1
    assert sum("already queued" in str(result.get("reason")) for result in results) == 1
    snapshot = store.job_snapshot(item_id)
    recovery_events = [
        event for event in snapshot["events"]
        if event["kind"] == AUTOMATIC_RECOVERY_EVENT
    ]
    assert len(recovery_events) == 1
    recoveries = store.automatic_recoveries(item_id)
    assert len(recoveries) == 1
    assert recoveries[0]["state"] == "scheduled"


def test_prestart_capacity_failure_requeues_the_same_item_with_context(
    tmp_path, monkeypatch
) -> None:
    clock = [1_500.0]
    monkeypatch.setattr(voice_inbox.time, "time", lambda: clock[0])
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    item = store.enqueue(
        "run the accepted coding job",
        call_id="call-prestart-recovery",
        turn_id="turn-prestart-recovery",
    )
    assert store.claim_next("headless-voice-prestart-first") is not None

    result = _queue(
        store,
        item.item_id,
        error="no coding provider has capacity right now",
        kind="provider",
    )
    assert result["queued"] is True
    assert store.work_record(item.item_id) is None

    clock[0] += 2.0
    recovered = store.claim_next("headless-voice-prestart-second")
    assert recovered is not None
    assert recovered.item_id == item.item_id
    assert store.latest_automatic_recovery(item.item_id)["error"] == (
        "no coding provider has capacity right now"
    )


def test_recoverable_prestart_failure_advances_the_bounded_recovery(
    tmp_path, monkeypatch
) -> None:
    clock = [1_700.0]
    monkeypatch.setattr(voice_inbox.time, "time", lambda: clock[0])
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    item_id, _target = _working(store, "prestart-again")
    first = _queue(
        store,
        item_id,
        error="no coding provider has capacity right now",
        kind="provider",
    )
    assert first["queued"] is True

    clock[0] += 2.0
    assert store.claim_next("headless-voice-prestart-again") is not None
    active = store.latest_automatic_recovery(item_id)
    second = _queue(
        store,
        item_id,
        error="provider response timed out",
        kind="provider",
        active_recovery_id=str(active["recovery_id"]),
    )

    assert second["queued"] is True
    assert second["recoveries"] == 2
    assert store.work_record(item_id)["state"] == "resume_queued"
    assert [row["state"] for row in store.automatic_recoveries(item_id)] == [
        "failed",
        "scheduled",
    ]


def test_repeated_failure_stops_with_a_durable_no_progress_reason(
    tmp_path, monkeypatch
) -> None:
    clock = [2_000.0]
    monkeypatch.setattr(voice_inbox.time, "time", lambda: clock[0])
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    item_id, _target = _working(store, "repeated")
    error = "mechanical completion evidence is incomplete: pytest still fails"
    assert _queue(store, item_id, error=error)["queued"] is True

    clock[0] += 2.0
    claimed = store.claim_next("headless-voice-repeated-2")
    assert claimed is not None
    assert store.acknowledge_started(
        item_id,
        target_sid="headless-voice-repeated-2",
        cwd="/tmp/project",
    )
    active = store.latest_automatic_recovery(item_id)
    stopped = _queue(
        store,
        item_id,
        error=error,
        active_recovery_id=str(active["recovery_id"]),
    )

    assert stopped["terminal"] is True
    assert "same failure repeated without progress" in stopped["reason"]
    snapshot = store.job_snapshot(item_id)
    assert snapshot["queue"]["state"] == "delivered"
    assert snapshot["work"]["state"] == "failed"
    assert "without progress" in snapshot["work"]["last_error"]
    assert snapshot["events"][-1]["kind"] == AUTOMATIC_RECOVERY_TERMINAL_EVENT
    assert [entry["state"] for entry in snapshot["recoveries"]] == [
        "failed",
        "terminal",
    ]


def test_recovery_limit_is_hard_even_when_each_failure_is_different(
    tmp_path, monkeypatch
) -> None:
    clock = [3_000.0]
    monkeypatch.setattr(voice_inbox.time, "time", lambda: clock[0])
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    item_id, _target = _working(store, "bounded")

    failures = (
        "provider capacity unavailable",
        "provider connection reset",
        "provider response timed out",
    )
    for number, failure in enumerate(failures):
        active = store.latest_automatic_recovery(item_id)
        result = _queue(
            store,
            item_id,
            error=failure,
            kind="provider",
            active_recovery_id=(
                str(active["recovery_id"])
                if active and active.get("state") == "running"
                else ""
            ),
        )
        assert result["queued"] is True
        clock[0] += 31.0
        target = f"headless-voice-bounded-{number}"
        claimed = store.claim_next(target)
        assert claimed is not None
        assert store.acknowledge_started(item_id, target_sid=target, cwd="/tmp/project")

    active = store.latest_automatic_recovery(item_id)
    stopped = _queue(
        store,
        item_id,
        error="provider transient failure final",
        kind="provider",
        active_recovery_id=str(active["recovery_id"]),
    )
    assert stopped["terminal"] is True
    assert stopped["reason"] == "automatic recovery budget of 3 exhausted"
    assert store.work_record(item_id)["state"] == "failed"


def test_delayed_recovery_does_not_block_an_existing_queued_job(
    tmp_path, monkeypatch
) -> None:
    clock = [4_000.0]
    monkeypatch.setattr(voice_inbox.time, "time", lambda: clock[0])
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    recovering_id, _target = _working(store, "recovering")
    queued = store.enqueue(
        "independent queued work",
        call_id="call-independent",
        turn_id="turn-independent",
    )
    assert _queue(store, recovering_id, error="Codex exited with status 1", kind="provider")[
        "queued"
    ] is True

    claimed = store.claim_next("headless-voice-independent")
    assert claimed is not None
    assert claimed.item_id == queued.item_id


def test_scheduled_recovery_without_a_session_can_be_cancelled(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    item_id, _target = _working(store, "cancel-recovery", session_id="")
    assert _queue(
        store,
        item_id,
        error="Codex exited with status 1",
        kind="provider",
    )["queued"] is True
    assert store.work_record(item_id)["state"] == "failed"
    assert store.recent_jobs(limit=1)[0]["state"] == "queued"
    assert store.overlay_snapshot(item_id)["controls"]["can_cancel"] is True

    control_id = store.request_cancel(item_id)

    assert store.work_record(item_id)["state"] == "cancelled"
    assert store.latest_automatic_recovery(item_id)["state"] == "cancelled"
    assert any(
        control["control_id"] == control_id and control["state"] == "applied"
        for control in store.job_snapshot(item_id)["controls"]
    )
    assert store.claim_next("headless-voice-cancelled-recovery") is None


def test_unsafe_failure_during_recovery_records_a_terminal_reason(
    tmp_path, monkeypatch
) -> None:
    clock = [4_300.0]
    monkeypatch.setattr(voice_inbox.time, "time", lambda: clock[0])
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    item_id, _target = _working(store, "unsafe-terminal")
    assert _queue(
        store,
        item_id,
        error="Codex exited with status 1",
        kind="provider",
    )["queued"] is True
    clock[0] += 2.0
    target = "headless-voice-unsafe-terminal"
    assert store.claim_next(target) is not None
    assert store.acknowledge_started(item_id, target_sid=target, cwd="/tmp/project")

    assert store.finish_work_item(item_id, error="authority boundary denied the write")

    latest = store.latest_automatic_recovery(item_id)
    assert latest["state"] == "terminal"
    assert latest["terminal_reason"] == "authority boundary denied the write"


def test_repeated_supervisor_crash_is_terminal_instead_of_requeued_forever(
    tmp_path, monkeypatch
) -> None:
    clock = [4_500.0]
    monkeypatch.setattr(voice_inbox.time, "time", lambda: clock[0])
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    item_id, _target = _working(store, "crash")

    assert store.recover_headless_work() == 1
    clock[0] += 2.0
    target = "headless-voice-crash-recovery"
    claimed = store.claim_next(target)
    assert claimed is not None
    assert store.acknowledge_started(item_id, target_sid=target, cwd="/tmp/project")

    assert store.recover_headless_work() == 0
    snapshot = store.job_snapshot(item_id)
    assert snapshot["work"]["state"] == "failed"
    assert "without progress" in snapshot["work"]["last_error"]
    assert snapshot["events"][-1]["kind"] == AUTOMATIC_RECOVERY_TERMINAL_EVENT


def test_failure_classifier_recovers_only_known_safe_categories() -> None:
    assert voice_work_supervisor.automatic_recovery_kind(
        "mechanical completion evidence is incomplete: pytest failed"
    ) == "evidence"
    assert voice_work_supervisor.automatic_recovery_kind(
        "claude-opus-5 review still rejects the corrected scoped diff"
    ) == "review"
    assert voice_work_supervisor.automatic_recovery_kind(
        "Codex exited with status 1"
    ) == "provider"
    assert voice_work_supervisor.automatic_recovery_kind(
        "no coding provider has capacity right now"
    ) == "provider"
    assert voice_work_supervisor.automatic_recovery_kind(
        "accepted coding job has an invalid shared model policy"
    ) == ""
