"""Deterministic fake benchmark and real telemetry percentile summary."""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

from .telemetry import (
    percentile_summary,
    stage_percentile_summary,
    summarize_values,
)


def run_deterministic_benchmark(
    iterations: int = 50, *, seed: int = 16
) -> dict[str, Any]:
    """Generate reproducible fake stage timings for pipeline regression tests."""
    if iterations < 1:
        raise ValueError("iterations must be positive")
    rng = random.Random(seed)
    estimates: list[float] = []
    stage_rows: list[dict[str, float]] = []
    for _ in range(iterations):
        row = {
            "endpoint_close_ms": 20.0 + rng.randrange(0, 41),
            "stt_ms": 210.0 + rng.randrange(0, 21),
            "brain_first_delta_ms": 1_000.0 + rng.randrange(0, 501),
            "first_sentence_ms": 90.0 + rng.randrange(0, 16),
            "tts_first_chunk_ms": 140.0 + rng.randrange(0, 26),
            "round_trip_rtt_ms": 36.0 + rng.randrange(0, 17),
        }
        estimate = sum(row.values())
        estimates.append(estimate)
        stage_rows.append(row)
    return {
        "mode": "deterministic_fake",
        "seed": seed,
        "iterations": iterations,
        "hardware_path_measured": False,
        "acceptance_claim": False,
        "note": (
            "Synthetic regression numbers only, using the measured warm brain "
            "range of 1.0 to 1.5 seconds. Run the physical Android, Tailnet, "
            "host, and audio path before evaluating acceptance."
        ),
        "eou_to_first_playable_estimate_ms": summarize_values(
            estimates,
            event="deterministic_fake.eou_first_playable",
            field="estimate_ms",
        ),
        "first_run_stages_ms": stage_rows[0],
    }


def summarize_metrics(metrics: Path) -> dict[str, Any]:
    """Summarize real telemetry without turning metadata into acceptance."""
    return {
        "mode": "telemetry_summary",
        "phone_eou_to_first_content_playback_ms": percentile_summary(
            metrics,
            event="latency.eou_first_content_playable_phone",
            field="elapsed_ms",
        ),
        "phone_eou_to_first_output_playback_ms": percentile_summary(
            metrics,
            event="latency.eou_first_playable_phone",
            field="elapsed_ms",
        ),
        "server_content_estimate_ms": percentile_summary(
            metrics,
            event="latency.eou_first_content_playable_estimate",
            field="estimate_ms",
        ),
        "server_first_output_estimate_ms": percentile_summary(metrics),
        "server_ack_proxy_ms": percentile_summary(
            metrics,
            event="latency.eou_first_playable_ack",
            field="elapsed_ms",
        ),
        "tailnet_rtt_ms": percentile_summary(
            metrics,
            event="network.rtt",
            field="ws_rtt_ms",
        ),
        "stage_segments_ms": stage_percentile_summary(metrics),
        "hardware_path_measured": "unknown",
        "acceptance_claim": False,
        "gate_metric": "phone_eou_to_first_content_playback_ms",
        "note": (
            "The gate metric is first real content on one Android monotonic "
            "clock. Acknowledgement playback is diagnostic only. This summary "
            "still does not prove an away-from-home physical run; inspect "
            "measured path evidence and hardware gates."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--seed", type=int, default=16)
    parser.add_argument("--metrics", type=Path)
    args = parser.parse_args(argv)
    if args.metrics:
        result = summarize_metrics(args.metrics)
    else:
        result = run_deterministic_benchmark(args.iterations, seed=args.seed)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
