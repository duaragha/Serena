from __future__ import annotations

import json
import os
from pathlib import Path

from core import brain_soak


def _health(
    *,
    session_id: str,
    rotations: int,
    files: int,
    epochs: list[dict],
    descendants: int = 1,
    rss: int = 100_000_000,
) -> dict:
    root_pid = 111
    process_token = f"token-{session_id}"
    processes = [
        {"pid": root_pid, "rss_bytes": 20_000_000, "create_time": 1},
        {"pid": 222, "rss_bytes": rss - 20_000_000, "create_time": 2},
    ]
    if descendants == 0:
        processes = processes[:1]
    return {
        "ok": True,
        "pid": root_pid,
        "lifetime": {
            "session_id": session_id,
            "process_token": process_token,
            "rotations": rotations,
            "baseline_child_rss_bytes": 80_000_000,
            "policy": {
                "max_child_rss_growth_bytes": 256 * 1024 * 1024,
                "max_child_rss_multiplier": 2,
            },
            "process": {
                "available": True,
                "root_pid": root_pid,
                "rss_bytes": rss,
                "descendants": descendants,
                "processes": processes,
            },
            "sdk_processes": {
                "available": True,
                "active_token": process_token,
                "process_count": 1,
                "tokens": [process_token],
                "token_count": 1,
                "active_processes": 1,
                "stale_processes": 0,
                "stale_tokens": [],
            },
            "session_store": {
                "available": True,
                "jsonl_files": files,
                "jsonl_bytes": files * 1_000,
                "current": {"session_id": session_id, "bytes": 1_000},
            },
            "journal": {
                "pending_entries": 0,
                "transport_uncertain_entries": 0,
            },
            "ledger": {"epochs": epochs},
            "last_error": None,
            "fatal_error": None,
        },
    }


def _epochs(count: int) -> list[dict]:
    return [
        {
            "session_id": f"session-{index}",
            "process_token": f"token-session-{index}",
            "ended_at": index if index < count else None,
            "end_reason": "rotation" if index < count else None,
        }
        for index in range(1, count + 1)
    ]


def test_full_day_analysis_requires_rotation_continuity_and_bounded_state() -> None:
    checkpoints = (0.0, 21_600.0, 43_200.0, 64_800.0, 86_400.0)
    started = 1_000_000.0
    events = [{"type": "run.start", "at": started, "elapsed_seconds": 0}]
    for index, elapsed in enumerate(checkpoints, 1):
        session_before = "session-boot" if index == 1 else f"session-{index - 1}"
        events.append(
            {
                "type": "checkpoint",
                "at": started + elapsed,
                "elapsed_seconds": elapsed,
                "checkpoint_index": index - 1,
                "ok": True,
                "stored_ok": True,
                "recalled_ok": True,
                "cross_rotation": True,
                "session_before": session_before,
                "session_after_store": f"session-{index}",
                "session_after": f"session-{index}",
            }
        )
        events.append(
            {
                "type": "health",
                "at": started + elapsed,
                "elapsed_seconds": elapsed,
                "ok": True,
                "health": _health(
                    session_id=f"session-{index}",
                    rotations=index - 1,
                    files=9 + index,
                    epochs=_epochs(index),
                ),
            }
        )
    events.append(
        {
            "type": "run.stop",
            "at": started + 86_401,
            "elapsed_seconds": 86_401,
            "interrupted": False,
        }
    )

    summary = brain_soak.analyze_events(
        events,
        duration_seconds=86_400,
        sample_seconds=21_600,
        checkpoints_seconds=checkpoints,
    )

    assert summary["status"] == "pass"
    assert summary["passed"] is True
    assert all(summary["criteria"].values())
    assert summary["failures"] == []


def test_analysis_fails_a_live_orphan_and_missing_final_recall() -> None:
    checkpoints = (0.0, 86_400.0)
    started = 1_000_000.0
    events = [
        {"type": "run.start", "at": started, "elapsed_seconds": 0},
        {
            "type": "checkpoint",
            "at": started,
            "elapsed_seconds": 0,
            "checkpoint_index": 0,
            "ok": True,
        },
        {
            "type": "checkpoint",
            "at": started + 86_400,
            "elapsed_seconds": 86_400,
            "checkpoint_index": 1,
            "ok": False,
        },
        {
            "type": "health",
            "at": started,
            "elapsed_seconds": 0,
            "ok": True,
            "health": _health(
                session_id="session-1",
                rotations=0,
                files=1,
                epochs=_epochs(1),
            ),
        },
        {
            "type": "health",
            "at": started + 86_400,
            "elapsed_seconds": 86_400,
            "ok": True,
            "health": _health(
                session_id="session-2",
                rotations=1,
                files=2,
                epochs=_epochs(2),
                descendants=0,
            ),
        },
        {
            "type": "run.stop",
            "at": started + 86_401,
            "elapsed_seconds": 86_401,
            "interrupted": False,
        },
    ]

    summary = brain_soak.analyze_events(
        events,
        duration_seconds=86_400,
        sample_seconds=86_400,
        checkpoints_seconds=checkpoints,
    )

    assert summary["status"] == "fail"
    assert summary["criteria"]["hour24_thread_awareness"] is False
    assert summary["criteria"]["no_orphaned_sessions"] is False
    assert any("orphaned" in failure for failure in summary["failures"])


def test_checkpoint_proves_the_exact_marker_across_the_turn_boundary(monkeypatch) -> None:
    health_values = iter(
        [
            {"lifetime": {"session_id": "session-1", "rotations": 0}},
            {"lifetime": {"session_id": "session-2", "rotations": 1}},
        ]
    )

    def fake_health(_path):
        return next(health_values)

    def fake_wait(_path, **_kwargs):
        return {"lifetime": {"session_id": "session-2", "rotations": 1}}

    marker_seen = ""

    def fake_turn(_path, text, **_kwargs):
        nonlocal marker_seen
        if "remember this marker" in text:
            marker_seen = text.split("exactly: stored ", 1)[1]
            return {"ok": True, "say": f"stored {marker_seen}", "elapsed": 1}
        return {"ok": True, "say": marker_seen, "elapsed": 1}

    monkeypatch.setattr(brain_soak, "fetch_health", fake_health)
    monkeypatch.setattr(brain_soak, "send_turn", fake_turn)
    monkeypatch.setattr(brain_soak, "wait_for_rotation", fake_wait)
    result = brain_soak.run_continuity_checkpoint(
        Path("unused"), checkpoint_index=2, elapsed_seconds=43_200
    )

    assert result["ok"] is True
    assert result["stored_ok"] is True
    assert result["recalled_ok"] is True
    assert result["session_before"] == "session-1"
    assert result["session_after_store"] == "session-2"
    assert result["session_after"] == "session-2"
    assert result["cross_rotation"] is True


def test_analysis_rejects_endpoint_only_samples_and_empty_epoch_evidence() -> None:
    duration = 86_400.0
    sample = 60.0
    started = 1_000_000.0
    checkpoints = (0.0, 21_600.0, 43_200.0, 64_800.0, 86_400.0)
    events = [{"type": "run.start", "at": started, "elapsed_seconds": 0.0}]
    for _ in range(int(duration // sample) + 1):
        events.append(
            {
                "type": "health",
                "at": started + duration,
                "elapsed_seconds": duration,
                "ok": True,
                "health": _health(
                    session_id="session-2",
                    rotations=1,
                    files=2,
                    epochs=[],
                ),
            }
        )
    for index, elapsed in enumerate(checkpoints):
        events.append(
            {
                "type": "checkpoint",
                "at": started + duration,
                "elapsed_seconds": elapsed,
                "checkpoint_index": index,
                "ok": True,
                "stored_ok": True,
                "recalled_ok": True,
                "cross_rotation": True,
                "session_before": f"old-{index}",
                "session_after_store": f"new-{index}",
                "session_after": f"new-{index}",
            }
        )
    events.append(
        {
            "type": "run.stop",
            "at": started + duration + 1,
            "elapsed_seconds": duration + 1,
            "interrupted": False,
        }
    )

    summary = brain_soak.analyze_events(
        events,
        duration_seconds=duration,
        sample_seconds=sample,
        checkpoints_seconds=checkpoints,
    )

    assert summary["passed"] is False
    assert summary["criteria"]["temporal_evidence"] is False
    assert summary["criteria"]["sample_coverage"] < 0.01
    assert summary["criteria"]["no_orphaned_sessions"] is False


def test_hour_24_recall_must_cross_its_own_rotation() -> None:
    checkpoints = (0.0, 21_600.0, 43_200.0, 64_800.0, 86_400.0)
    started = 1_000_000.0
    events = [{"type": "run.start", "at": started, "elapsed_seconds": 0.0}]
    for index, elapsed in enumerate(checkpoints, 1):
        cross_rotation = index < len(checkpoints)
        before = f"session-{max(1, index - 1)}"
        after = f"session-{index}" if cross_rotation else before
        events.extend(
            [
                {
                    "type": "checkpoint",
                    "at": started + elapsed,
                    "elapsed_seconds": elapsed,
                    "checkpoint_index": index - 1,
                    "ok": True,
                    "stored_ok": True,
                    "recalled_ok": True,
                    "cross_rotation": cross_rotation,
                    "session_before": before,
                    "session_after_store": after,
                    "session_after": after,
                },
                {
                    "type": "health",
                    "at": started + elapsed,
                    "elapsed_seconds": elapsed,
                    "ok": True,
                    "health": _health(
                        session_id=f"session-{index}",
                        rotations=index - 1,
                        files=9 + index,
                        epochs=_epochs(index),
                    ),
                },
            ]
        )
    events.append(
        {
            "type": "run.stop",
            "at": started + 86_401,
            "elapsed_seconds": 86_401,
            "interrupted": False,
        }
    )

    summary = brain_soak.analyze_events(
        events,
        duration_seconds=86_400,
        sample_seconds=21_600,
        checkpoints_seconds=checkpoints,
    )

    assert summary["passed"] is False
    assert summary["criteria"]["hour24_thread_awareness"] is False


def test_load_events_rejects_corrupt_ndjson(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text('{"type":"health"}\nnot-json\n', encoding="utf-8")

    try:
        brain_soak.load_events(events)
    except brain_soak.SoakError as exc:
        assert ":2:" in str(exc)
    else:
        raise AssertionError("corrupt soak log was accepted")


def test_load_events_rejects_non_object_rows(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text('{"type":"run.start"}\n[]\n', encoding="utf-8")

    try:
        brain_soak.load_events(events)
    except brain_soak.SoakError as exc:
        assert ":2" in str(exc)
    else:
        raise AssertionError("non-object soak event was accepted")


def test_analysis_rejects_malformed_successful_health_without_crashing() -> None:
    events = [
        {"type": "run.start", "at": 10.0, "elapsed_seconds": 0.0},
        {
            "type": "health",
            "at": 10.0,
            "elapsed_seconds": 0.0,
            "ok": True,
            "health": None,
        },
        {
            "type": "run.stop",
            "at": 11.0,
            "elapsed_seconds": 1.0,
            "interrupted": False,
        },
    ]

    summary = brain_soak.analyze_events(
        events,
        duration_seconds=1.0,
        sample_seconds=1.0,
        checkpoints_seconds=(),
    )

    assert summary["passed"] is False
    assert any("malformed" in failure for failure in summary["failures"])


def test_progress_summary_is_serializable(tmp_path: Path) -> None:
    config = brain_soak.SoakConfig(
        duration_seconds=1,
        sample_seconds=1,
        checkpoints_seconds=(0,),
        discovery_path=tmp_path / "brain.json",
        events_path=tmp_path / "events.jsonl",
        summary_path=tmp_path / "summary.json",
    )
    value = brain_soak._progress_summary([], config, running=True)
    json.dumps(value)
    assert value["status"] == "running"


def test_wait_for_brain_requires_a_warm_lifetime(monkeypatch) -> None:
    responses = iter(
        [
            {"ok": True, "lifetime": None},
            {
                "ok": True,
                "lifetime": {
                    "session_id": "session-ready",
                    "rotation_in_progress": False,
                },
            },
        ]
    )
    monkeypatch.setattr(brain_soak, "fetch_health", lambda _path: next(responses))
    monkeypatch.setattr(brain_soak.time, "sleep", lambda _seconds: None)

    health = brain_soak.wait_for_brain(Path("unused"), timeout=1)

    assert health["lifetime"]["session_id"] == "session-ready"


def test_soak_lock_recovers_a_stale_owner(tmp_path: Path) -> None:
    lock = tmp_path / "brain-soak.lock"
    lock.write_text("99999999\n", encoding="utf-8")

    with brain_soak.SoakLock(lock):
        assert int(lock.read_text(encoding="utf-8")) == os.getpid()
    assert not lock.exists()
