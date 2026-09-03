from __future__ import annotations

import json
from pathlib import Path

from voice.call.benchmark import run_deterministic_benchmark, summarize_metrics
from voice.call.telemetry import (
    CallTelemetry,
    percentile_summary,
    stage_percentile_summary,
)


class Clock:
    def __init__(self) -> None:
        self.ns = 1_000_000_000

    def __call__(self) -> int:
        return self.ns

    def advance_ms(self, milliseconds: float) -> None:
        self.ns += int(milliseconds * 1_000_000)


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_rtt_and_eou_estimate_include_both_network_legs(tmp_path: Path) -> None:
    path = tmp_path / "metrics.jsonl"
    clock = Clock()
    telemetry = CallTelemetry("call", path, clock_ns=clock, wall_clock=lambda: 1.0)
    telemetry.report_rtt(
        40,
        "relay",
        path_source="tailscale_probe",
        sample_id="ping-7",
    )
    telemetry.receive_batch(
        generation=2,
        sequence=7,
        samples=3_200,
        queue_depth=1,
        client_monotonic_us=123_456,
    )
    telemetry.speech_end(
        2, source="silero", trailing_silence_samples=10_240
    )
    clock.advance_ms(100)
    telemetry.first_audio_send(2, sequence=0)
    estimate = [
        row
        for row in rows(path)
        if row["event"] == "latency.eou_first_playable_estimate"
    ][0]
    assert estimate["server_ms"] == 740.0
    assert estimate["round_trip_rtt_ms"] == 40.0
    assert estimate["estimate_ms"] == 780.0
    assert estimate["path"] == "relay"
    batch = [row for row in rows(path) if row["event"] == "receive.batch"][0]
    assert batch["client_monotonic_us"] == 123_456
    assert batch["rolling_ws_rtt_ms"] == 40.0
    assert batch["rtt_sample_age_ms"] == 0.0
    assert batch["network_sample_id"] == "ping-7"
    assert batch["path"] == "relay"
    assert batch["path_source"] == "tailscale_probe"
    assert batch["clock_domain"] == "server_monotonic"
    assert batch["schema_version"] == 1
    summary = percentile_summary(path)
    assert summary["count"] == 1
    assert summary["p90"] == 780.0


def test_phone_metric_and_stage_segments_stay_on_monotonic_clocks(
    tmp_path: Path,
) -> None:
    path = tmp_path / "metrics.jsonl"
    clock = Clock()
    telemetry = CallTelemetry("call", path, clock_ns=clock, wall_clock=lambda: 1.0)
    telemetry.speech_end(3, source="ptt", client_eou_us=55_000)
    telemetry.stage(3, "endpoint.detected")
    clock.advance_ms(12)
    telemetry.stage(3, "stt.start")
    clock.advance_ms(205)
    telemetry.stage(3, "stt.done")
    clock.advance_ms(1_100)
    telemetry.playback_started(
        3,
        sequence=0,
        timestamp_us=1_500_000,
        eou_to_playback_ms=1_430.25,
        first_output_to_playback_ms=7.5,
        first_pcm_write_to_playback_ms=0.75,
        play_return_to_head_ms=5.0,
        measurement_point="playback_head_advanced",
        kind="acknowledgement",
    )
    telemetry.content_playback_started(
        3,
        sequence=4,
        timestamp_us=1_880_000,
        eou_to_playback_ms=1_810.5,
        measurement_point="playback_head_advanced",
    )

    phone = percentile_summary(
        path,
        event="latency.eou_first_playable_phone",
        field="elapsed_ms",
    )
    assert phone["p90"] == 1_430.25
    stages = stage_percentile_summary(path)
    assert stages["stt.start"]["p90"] == 12.0
    assert stages["stt.done"]["p90"] == 205.0
    exact = [
        row
        for row in rows(path)
        if row["event"] == "latency.eou_first_playable_phone"
    ][0]
    assert exact["measurement"] == "single_phone_monotonic_clock"
    assert exact["measurement_point"] == "playback_head_advanced"
    assert exact["first_output_to_playback_ms"] == 7.5
    assert exact["first_pcm_write_to_playback_ms"] == 0.75
    assert exact["play_return_to_head_ms"] == 5.0
    assert exact["kind"] == "acknowledgement"
    content = percentile_summary(
        path,
        event="latency.eou_first_content_playable_phone",
        field="elapsed_ms",
    )
    assert content["p90"] == 1_810.5


def test_deterministic_benchmark_is_repeatable_and_not_acceptance() -> None:
    first = run_deterministic_benchmark(20, seed=7)
    second = run_deterministic_benchmark(20, seed=7)
    assert first == second
    assert first["hardware_path_measured"] is False
    assert first["acceptance_claim"] is False


def test_real_summary_uses_content_playback_as_the_gate(tmp_path: Path) -> None:
    path = tmp_path / "metrics.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "latency.eou_first_playable_phone",
                        "elapsed_ms": 700.0,
                    }
                ),
                json.dumps(
                    {
                        "event": "latency.eou_first_content_playable_phone",
                        "elapsed_ms": 1_850.0,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = summarize_metrics(path)

    assert summary["gate_metric"] == "phone_eou_to_first_content_playback_ms"
    assert summary["phone_eou_to_first_content_playback_ms"]["p90"] == 1_850.0
    assert summary["phone_eou_to_first_output_playback_ms"]["p90"] == 700.0
    assert summary["acceptance_claim"] is False


def test_stale_rtt_is_logged_but_excluded_from_server_estimate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stale-rtt.jsonl"
    clock = Clock()
    telemetry = CallTelemetry("call", path, clock_ns=clock, wall_clock=lambda: 1.0)
    telemetry.report_rtt(40, "direct", sample_id="old")
    telemetry.speech_end(1, source="ptt")
    clock.advance_ms(15_001)
    telemetry.first_audio_send(1, sequence=0)

    estimate = [
        row
        for row in rows(path)
        if row["event"] == "latency.eou_first_playable_estimate"
    ][0]
    assert estimate["round_trip_rtt_ms"] == 40.0
    assert estimate["rtt_sample_age_ms"] == 15_001.0
    assert estimate["rtt_stale"] is True
    assert estimate["estimate_ms"] is None
    assert percentile_summary(path)["count"] == 0
