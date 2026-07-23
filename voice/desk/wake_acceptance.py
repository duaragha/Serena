"""Privacy-safe acceptance report for Serena's two-stage desk wake path."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from voice.desk.wake_listener import (
    DEFAULT_PHRASE_MODEL,
    TARGET_WAKE_PHRASE,
    WAKE_EVENT_PREFIX,
    phrase_model_sha256,
)

DEFAULT_ATTEMPTS = (
    Path.home() / ".local" / "state" / "serena" / "wake-attempts.jsonl"
)
DEFAULT_REPORT = (
    Path.home() / ".local" / "state" / "serena" / "wake-two-stage-report.json"
)


@dataclass(frozen=True, slots=True)
class WakeAcceptancePolicy:
    minimum_background_hours: float = 7.0
    minimum_active_days: int = 7
    minimum_attempts: int = 20
    maximum_miss_rate: float = 0.05
    maximum_false_accepts: int = 0
    maximum_heartbeat_gap_seconds: float = 90.0


def parse_journal_messages(lines: Iterable[str]) -> list[dict[str, Any]]:
    events = []
    for line in lines:
        marker = line.find(WAKE_EVENT_PREFIX)
        if marker < 0:
            continue
        raw = line[marker + len(WAKE_EVENT_PREFIX) :].strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and str(value.get("event") or "").startswith(
            "wake."
        ):
            events.append(value)
    return events


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.expanduser().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    values = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def _observed_seconds(
    events: list[dict[str, Any]],
    *,
    maximum_gap: float,
) -> tuple[float, int]:
    by_listener: dict[str, list[int]] = {}
    active_dates: set[str] = set()
    for event in events:
        if event.get("event") not in {
            "wake.listener_started",
            "wake.heartbeat",
            "wake.listener_stopped",
        }:
            continue
        listener = str(event.get("listener_id") or "")
        wall_ns = int(event.get("wall_ns") or 0)
        if not listener or wall_ns <= 0:
            continue
        by_listener.setdefault(listener, []).append(wall_ns)
        active_dates.add(datetime.fromtimestamp(wall_ns / 1e9).date().isoformat())
    seconds = 0.0
    for values in by_listener.values():
        ordered = sorted(set(values))
        for first, second in zip(ordered, ordered[1:], strict=False):
            gap = max(0.0, (second - first) / 1e9)
            if gap <= maximum_gap:
                seconds += gap
    return seconds, len(active_dates)


def build_report(
    events: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    *,
    current_phrase_model_sha256: str,
    policy: WakeAcceptancePolicy | None = None,
) -> dict[str, Any]:
    policy = policy or WakeAcceptancePolicy()
    events = sorted(events, key=lambda row: int(row.get("wall_ns") or 0))
    accepted = [
        row for row in events if row.get("event") == "wake.accepted"
    ]
    candidates = [
        row for row in events if row.get("event") == "wake.phrase_candidate"
    ]
    valid_attempts = [
        row
        for row in attempts
        if int(row.get("start_wall_ns") or 0) > 0
        and int(row.get("end_wall_ns") or 0)
        > int(row.get("start_wall_ns") or 0)
    ]
    detected_attempts: set[str] = set()
    intended_accept_event_ids: set[int] = set()
    for attempt in valid_attempts:
        start = int(attempt["start_wall_ns"])
        end = int(attempt["end_wall_ns"])
        for index, event in enumerate(accepted):
            wall_ns = int(event.get("wall_ns") or 0)
            if start <= wall_ns <= end:
                detected_attempts.add(str(attempt.get("attempt_id") or index))
                intended_accept_event_ids.add(index)
                break
    misses = len(valid_attempts) - len(detected_attempts)
    miss_rate = misses / len(valid_attempts) if valid_attempts else None
    false_accepts = len(accepted) - len(intended_accept_event_ids)
    background_seconds, active_days = _observed_seconds(
        events,
        maximum_gap=policy.maximum_heartbeat_gap_seconds,
    )
    observed_hashes = {
        str(row.get("phrase_model_sha256") or "")
        for row in events
        if row.get("phrase_model_sha256")
    }
    model_match = bool(observed_hashes) and observed_hashes == {
        current_phrase_model_sha256
    }
    failures = []
    if background_seconds / 3600 < policy.minimum_background_hours:
        failures.append("not enough structured two-stage background time")
    if active_days < policy.minimum_active_days:
        failures.append("not enough active observation days")
    if len(valid_attempts) < policy.minimum_attempts:
        failures.append("not enough marked wake attempts")
    if miss_rate is not None and miss_rate > policy.maximum_miss_rate:
        failures.append("wake miss rate is above policy")
    if false_accepts > policy.maximum_false_accepts:
        failures.append("unintended final wake accepts are above policy")
    if not model_match:
        failures.append("observed phrase verifier identity is missing or changed")
    return {
        "schema_version": 1,
        "generated_at": time.time(),
        "privacy": {
            "raw_audio_stored": False,
            "transcripts_stored": False,
            "stored_event_data": [
                "timestamps",
                "wake score",
                "accepted boolean",
                "recognized word count",
                "phrase model hash",
            ],
        },
        "policy": asdict(policy),
        "coverage": {
            "background_hours": round(background_seconds / 3600, 6),
            "active_days": active_days,
            "structured_events": len(events),
            "candidates": len(candidates),
            "attempts": len(valid_attempts),
            "detected_attempts": len(detected_attempts),
            "misses": misses,
            "miss_rate": round(miss_rate, 6) if miss_rate is not None else None,
            "final_accepts": len(accepted),
            "false_accepts": false_accepts,
        },
        "phrase_model_sha256": current_phrase_model_sha256,
        "observed_phrase_model_sha256": sorted(observed_hashes),
        "phrase_model_match": model_match,
        "failures": failures,
        "acceptance_claim": not failures,
    }


def _append_private(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(
            descriptor,
            (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            ),
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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


def command_attempt(args: argparse.Namespace) -> int:
    now = time.time_ns()
    start = now + int(max(0.0, args.lead) * 1e9)
    payload = {
        "attempt_id": uuid.uuid4().hex,
        "created_wall_ns": now,
        "start_wall_ns": start,
        "end_wall_ns": start + int(max(1.0, args.window) * 1e9),
        "phrase": TARGET_WAKE_PHRASE,
        "environment": args.environment,
        "distance": args.distance,
    }
    _append_private(Path(args.attempts), payload)
    print(json.dumps({"ok": True, **payload}, sort_keys=True))
    return 0


def _journal_lines(since: str) -> list[str]:
    result = subprocess.run(
        [
            "journalctl",
            "--user",
            "--unit",
            "serena-wake-listener.service",
            "--since",
            since,
            "--output",
            "cat",
            "--no-pager",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError(result.stderr.strip() or "journalctl failed")
    return result.stdout.splitlines()


def command_report(args: argparse.Namespace) -> int:
    phrase_hash = phrase_model_sha256(Path(args.phrase_model))
    events = parse_journal_messages(_journal_lines(args.since))
    attempts = _load_jsonl(Path(args.attempts))
    report = build_report(
        events,
        attempts,
        current_phrase_model_sha256=phrase_hash,
    )
    _write_atomic(Path(args.output), report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["acceptance_claim"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    attempt = subparsers.add_parser("attempt")
    attempt.add_argument("--attempts", default=str(DEFAULT_ATTEMPTS))
    attempt.add_argument("--lead", type=float, default=0.5)
    attempt.add_argument("--window", type=float, default=8.0)
    attempt.add_argument("--environment", default="normal-desk")
    attempt.add_argument("--distance", default="near")
    attempt.set_defaults(func=command_attempt)

    report = subparsers.add_parser("report")
    report.add_argument("--attempts", default=str(DEFAULT_ATTEMPTS))
    report.add_argument("--phrase-model", default=str(DEFAULT_PHRASE_MODEL))
    report.add_argument("--since", default="30 days ago")
    report.add_argument("--output", default=str(DEFAULT_REPORT))
    report.set_defaults(func=command_report)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
