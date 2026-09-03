from __future__ import annotations

import json
from pathlib import Path

from voice.call.integrity import (
    IntegrityCriteria,
    analyze_call,
    latest_call_id,
    load_metrics,
)


def _row(event: str, monotonic_us: int, **fields):
    return {
        "schema_version": 1,
        "ts": "2026-07-17T00:00:00+00:00",
        "monotonic_us": monotonic_us,
        "call_id": "integrity-call",
        "event": event,
        **fields,
    }


def _write_session(root: Path, session_id: str, call_id: str, generation: int) -> None:
    path = root / "project" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True)
    turn_id = f"{call_id}:{generation}"
    prompt = (
        '<voice-turn-context>{"call_id":"'
        + call_id
        + '","turn_id":"'
        + turn_id
        + '"}</voice-turn-context>\n\nhello'
    )
    rows = [
        {"type": "user", "message": {"role": "user", "content": prompt}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "i'm here."}],
            },
        },
    ]
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def _passing_rows() -> list[dict]:
    return [
        _row("call.start", 0),
        _row("call.hello", 3_900_000, generation=0, cold_start=True, app_uptime_ms=3_900),
        _row("call.end", 300_000_000, generation=0, clean_hangup=False),
        _row("call.start", 301_000_000),
        _row("network.rtt", 400_000_000, path="relay"),
        _row("stt.done", 600_000_000, generation=1, text_chars=5),
        _row(
            "brain.done",
            601_000_000,
            generation=1,
            meta={
                "session_id": "session-1",
                "turn_id": "integrity-call:1",
                "session_turns": 1,
            },
        ),
        _row("audio.end", 602_000_000, generation=1, last_sequence=3),
        _row("playback.content_started", 603_000_000, generation=1, sequence=0),
        _row(
            "latency.eou_first_content_playable_phone",
            603_000_000,
            generation=1,
            elapsed_ms=1_750.0,
        ),
        _row("call.end", 1_201_000_000, generation=1, clean_hangup=True),
    ]


def test_integrity_report_closes_transport_store_and_acoustic_gates(
    tmp_path: Path,
) -> None:
    projects = tmp_path / "projects"
    _write_session(projects, "session-1", "integrity-call", 1)

    report = analyze_call(
        _passing_rows(),
        call_id="integrity-call",
        criteria=IntegrityCriteria(min_duration_seconds=1_200, min_user_turns=1),
        projects_root=projects,
        heard_clean=True,
    )

    assert report["ok"] is True
    assert report["acceptance_claim"] is True
    assert report["duration_seconds"] == 1_201.0
    assert report["segments"] == 2
    assert report["reconnects"] == 1
    assert report["transcript"]["complete"] is True
    assert report["cold_open"] == {
        "evidence": True,
        "hello_events": 1,
        "hello_ms": [3_900],
        "pass": True,
    }
    assert report["phone_eou_to_content_playback_ms"]["p90"] == 1_750.0
    assert report["network_paths"] == {"relay": 1}


def test_acoustic_attestation_cannot_be_inferred_from_clean_telemetry(
    tmp_path: Path,
) -> None:
    projects = tmp_path / "projects"
    _write_session(projects, "session-1", "integrity-call", 1)

    report = analyze_call(
        _passing_rows(),
        call_id="integrity-call",
        criteria=IntegrityCriteria(min_duration_seconds=1_200, min_user_turns=1),
        projects_root=projects,
    )

    assert report["transport_integrity_pass"] is True
    assert report["ok"] is False
    assert report["failures"] == [
        "physical acoustic clean-call attestation is missing"
    ]


def test_gap_error_and_missing_jsonl_fail_closed(tmp_path: Path) -> None:
    rows = _passing_rows()
    rows.insert(-1, _row("sequence.gap", 700_000_000, generation=1))
    rows.insert(-1, _row("playback.underrun", 701_000_000, generation=1))
    rows.insert(-1, _row("error", 702_000_000, generation=1, code="broken"))

    report = analyze_call(
        rows,
        call_id="integrity-call",
        criteria=IntegrityCriteria(min_duration_seconds=1_200, min_user_turns=1),
        projects_root=tmp_path / "missing",
        heard_clean=True,
    )

    assert report["ok"] is False
    assert report["audio"] == {
        "sequence_gaps": 1,
        "playback_underruns": 1,
        "queue_overflows": 0,
        "error_events": 1,
    }
    assert report["transcript"]["missing_transcript"] == ["integrity-call:1"]


def test_done_metadata_and_pipeline_order_fail_closed(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    _write_session(projects, "session-1", "integrity-call", 1)
    rows = _passing_rows()
    brain = next(row for row in rows if row["event"] == "brain.done")
    brain["meta"] = {
        "session_id": "session-1",
        "turn_id": "wrong-turn",
    }
    audio = next(row for row in rows if row["event"] == "audio.end")
    audio["monotonic_us"] = brain["monotonic_us"] - 1

    report = analyze_call(
        rows,
        call_id="integrity-call",
        criteria=IntegrityCriteria(min_duration_seconds=1_200, min_user_turns=1),
        projects_root=projects,
        heard_clean=True,
    )

    assert report["ok"] is False
    assert report["turn_pipeline"] == {
        "complete": False,
        "duplicate_or_missing_events": [],
        "invalid_done_metadata": ["integrity-call:1"],
        "ordering_violations": ["integrity-call:1:stt_brain_audio"],
    }


def test_pipeline_allows_content_playback_while_brain_is_still_streaming(
    tmp_path: Path,
) -> None:
    projects = tmp_path / "projects"
    _write_session(projects, "session-1", "integrity-call", 1)
    rows = _passing_rows()
    playback = next(
        row for row in rows if row["event"] == "playback.content_started"
    )
    playback["monotonic_us"] = 600_500_000

    report = analyze_call(
        rows,
        call_id="integrity-call",
        criteria=IntegrityCriteria(min_duration_seconds=1_200, min_user_turns=1),
        projects_root=projects,
        heard_clean=True,
    )

    assert report["turn_pipeline"]["complete"] is True
    assert report["ok"] is True


def test_duplicate_required_event_and_reused_session_turn_count_fail_closed(
    tmp_path: Path,
) -> None:
    projects = tmp_path / "projects"
    rows = _passing_rows()
    rows[-1]["monotonic_us"] = 1_300_000_000
    rows.extend(
        [
            _row("stt.done", 700_000_000, generation=2, text_chars=5),
            _row(
                "brain.done",
                701_000_000,
                generation=2,
                meta={
                    "session_id": "session-1",
                    "turn_id": "integrity-call:2",
                    "session_turns": 1,
                },
            ),
            _row("audio.end", 702_000_000, generation=2, last_sequence=3),
            _row("audio.end", 702_000_001, generation=2, last_sequence=3),
            _row(
                "playback.content_started",
                703_000_000,
                generation=2,
                sequence=0,
            ),
        ]
    )
    _write_session(projects, "session-1", "integrity-call", 1)
    path = projects / "project" / "session-1.jsonl"
    extra_root = tmp_path / "extra"
    _write_session(extra_root, "session-1", "integrity-call", 2)
    path.write_text(
        path.read_text(encoding="utf-8")
        + (extra_root / "project" / "session-1.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    report = analyze_call(
        rows,
        call_id="integrity-call",
        criteria=IntegrityCriteria(min_duration_seconds=1_200, min_user_turns=2),
        projects_root=projects,
        heard_clean=True,
    )

    assert report["ok"] is False
    assert report["turn_pipeline"]["duplicate_or_missing_events"] == [
        "integrity-call:2:audio.end:2"
    ]
    assert report["turn_pipeline"]["ordering_violations"] == [
        "integrity-call:2:session_turns_not_increasing"
    ]


def test_duplicate_hello_events_do_not_pass_cold_open_gate(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    _write_session(projects, "session-1", "integrity-call", 1)
    rows = _passing_rows()
    rows.insert(
        2,
        _row(
            "call.hello",
            4_000_000,
            generation=0,
            cold_start=True,
            app_uptime_ms=4_000,
        ),
    )

    report = analyze_call(
        rows,
        call_id="integrity-call",
        criteria=IntegrityCriteria(min_duration_seconds=1_200, min_user_turns=1),
        projects_root=projects,
        heard_clean=True,
    )

    assert report["cold_open"] == {
        "evidence": True,
        "hello_events": 2,
        "hello_ms": [3_900, 4_000],
        "pass": False,
    }
    assert report["transport_integrity_pass"] is False
    assert report["ok"] is False
    assert "cold app hello evidence is missing, duplicated, or too slow" in report[
        "failures"
    ]


def test_latest_call_and_malformed_metric_lines_are_bounded(tmp_path: Path) -> None:
    path = tmp_path / "metrics.jsonl"
    path.write_text(
        "not-json\n"
        + json.dumps(_row("call.start", 1))
        + "\n"
        + json.dumps({**_row("call.start", 2), "call_id": "latest"})
        + "\n",
        encoding="utf-8",
    )

    rows = load_metrics(path)
    assert len(rows) == 2
    assert latest_call_id(rows) == "latest"
