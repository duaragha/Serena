"""Truthful end-to-end acceptance report for Serena's laptop voice loop."""

from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_METRICS = Path.home() / ".config" / "serena" / "desk_metrics.jsonl"
DEFAULT_WAKE_REPORT = (
    Path.home() / ".local" / "state" / "serena" / "wake-two-stage-report.json"
)
DEFAULT_REPORT = (
    Path.home() / ".local" / "state" / "serena" / "desk-acceptance-report.json"
)

_FAILURE_EVENTS = {
    "error",
    "sequence.gap",
    "playback.underrun",
    "queue.overflow",
    "desk.response_timeout",
    "desk.server_error",
    "desk.connection_failed",
    "desk.disconnected",
}


@dataclass(frozen=True, slots=True)
class DeskAcceptancePolicy:
    minimum_completed_turns: int = 3
    maximum_eou_to_first_write_p90_ms: float = 1_500.0
    require_barge_in: bool = True
    require_clean_hangup: bool = True


def _timestamp(row: dict[str, Any]) -> float | None:
    value = row.get("ts") or row.get("timestamp")
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _latest_call_id(rows: list[dict[str, Any]]) -> str | None:
    candidates: dict[str, float] = {}
    for row in rows:
        call_id = str(row.get("call_id") or "")
        timestamp = _timestamp(row)
        if call_id.startswith("desk-") and timestamp is not None:
            candidates[call_id] = max(timestamp, candidates.get(call_id, timestamp))
    return max(candidates, key=candidates.get) if candidates else None


def analyze_desk_acceptance(
    rows: list[dict[str, Any]],
    *,
    wake_report: dict[str, Any] | None,
    call_id: str | None = None,
    heard_clean: bool = False,
    policy: DeskAcceptancePolicy | None = None,
) -> dict[str, Any]:
    policy = policy or DeskAcceptancePolicy()
    selected = call_id or _latest_call_id(rows)
    call_rows = [
        row for row in rows if str(row.get("call_id") or "") == str(selected or "")
    ]
    by_generation: dict[int, list[dict[str, Any]]] = {}
    for row in call_rows:
        generation = row.get("generation")
        if isinstance(generation, int) and generation > 0:
            by_generation.setdefault(generation, []).append(row)

    completed = []
    latencies = []
    incomplete: dict[int, list[str]] = {}
    required = {
        "endpoint.detected",
        "stt.done",
        "brain.first_delta",
        "desk.response_first_write",
        "audio.end",
    }
    for generation, generation_rows in sorted(by_generation.items()):
        names = {str(row.get("event") or "") for row in generation_rows}
        missing = sorted(required - names)
        if missing:
            incomplete[generation] = missing
            continue
        completed.append(generation)
        endpoint_times = [
            value
            for row in generation_rows
            if row.get("event") == "endpoint.detected"
            and (value := _timestamp(row)) is not None
        ]
        write_times = [
            value
            for row in generation_rows
            if row.get("event") == "desk.response_first_write"
            and (value := _timestamp(row)) is not None
        ]
        if endpoint_times and write_times:
            latency = (min(write_times) - min(endpoint_times)) * 1_000
            if latency >= 0:
                latencies.append(latency)

    failure_events = sorted(
        {
            str(row.get("event") or "")
            for row in call_rows
            if str(row.get("event") or "") in _FAILURE_EVENTS
        }
    )
    barge_ins = sum(row.get("event") == "desk.barge_in" for row in call_rows)
    clean_hangup = any(
        row.get("event") == "call.end" and row.get("clean_hangup") is True
        for row in call_rows
    )
    p90 = _percentile(latencies, 0.9)
    conversation_failures = []
    if not selected:
        conversation_failures.append("no desk call exists in the metrics")
    if len(completed) < policy.minimum_completed_turns:
        conversation_failures.append("not enough complete physical desk turns")
    if len(latencies) < policy.minimum_completed_turns:
        conversation_failures.append("not enough end-of-utterance to first-write timings")
    if p90 is not None and p90 > policy.maximum_eou_to_first_write_p90_ms:
        conversation_failures.append("first audible response p90 is above policy")
    if failure_events:
        conversation_failures.append("the selected call contains runtime failures")
    if policy.require_barge_in and barge_ins < 1:
        conversation_failures.append("no physical barge-in was observed")
    if policy.require_clean_hangup and not clean_hangup:
        conversation_failures.append("the selected call did not end cleanly")
    if not heard_clean:
        conversation_failures.append("human heard-clean confirmation is missing")

    wake_accepted = bool(
        isinstance(wake_report, dict)
        and wake_report.get("acceptance_claim") is True
    )
    wake_failures = (
        []
        if wake_accepted
        else ["the two-stage wake livability report is not accepted"]
    )
    conversation_accepted = not conversation_failures
    return {
        "schema_version": 1,
        "call_id": selected,
        "policy": asdict(policy),
        "privacy": {
            "raw_audio_read": False,
            "transcripts_read": False,
        },
        "coverage": {
            "rows": len(call_rows),
            "generations": sorted(by_generation),
            "completed_generations": completed,
            "incomplete_generations": incomplete,
            "eou_to_first_write_ms": [round(value, 3) for value in latencies],
            "eou_to_first_write_p90_ms": round(p90, 3) if p90 is not None else None,
            "barge_ins": barge_ins,
            "clean_hangup": clean_hangup,
            "failure_events": failure_events,
            "heard_clean": heard_clean,
        },
        "conversation_failures": conversation_failures,
        "conversation_acceptance_claim": conversation_accepted,
        "wake_failures": wake_failures,
        "wake_acceptance_claim": wake_accepted,
        "acceptance_claim": conversation_accepted and wake_accepted,
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    try:
        lines = path.expanduser().read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics", default=str(DEFAULT_METRICS))
    parser.add_argument("--wake-report", default=str(DEFAULT_WAKE_REPORT))
    parser.add_argument("--call-id")
    parser.add_argument("--heard-clean", action="store_true")
    parser.add_argument("--output", default=str(DEFAULT_REPORT))
    args = parser.parse_args(argv)
    report = analyze_desk_acceptance(
        _load_jsonl(Path(args.metrics)),
        wake_report=_load_json(Path(args.wake_report)),
        call_id=args.call_id,
        heard_clean=args.heard_clean,
    )
    _write_atomic(Path(args.output), report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["acceptance_claim"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
