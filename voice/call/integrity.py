"""Close the physical 20-minute call and cold-open acceptance gates.

This analyzer deliberately consumes only metadata from call telemetry.  For
transcript completeness it opens Claude's local session JSONLs, verifies the
exact call/turn marker and a following assistant text record, and emits no
transcript content.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .telemetry import DEFAULT_METRICS_PATH

DEFAULT_MIN_DURATION_SECONDS = 20 * 60
DEFAULT_MIN_USER_TURNS = 3
DEFAULT_MAX_COLD_HELLO_MS = 5_000


@dataclass(frozen=True, slots=True)
class IntegrityCriteria:
    min_duration_seconds: float = DEFAULT_MIN_DURATION_SECONDS
    min_user_turns: int = DEFAULT_MIN_USER_TURNS
    max_cold_hello_ms: int = DEFAULT_MAX_COLD_HELLO_MS

    def __post_init__(self) -> None:
        if self.min_duration_seconds <= 0:
            raise ValueError("minimum duration must be positive")
        if self.min_user_turns < 1:
            raise ValueError("minimum user turns must be positive")
        if self.max_cold_hello_ms <= 0:
            raise ValueError("maximum cold hello must be positive")


def load_metrics(path: Path = DEFAULT_METRICS_PATH) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        handle = Path(path).expanduser().open("r", encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return rows
    try:
        with handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except UnicodeDecodeError:
        pass
    return rows


def latest_call_id(rows: Iterable[dict[str, Any]]) -> str | None:
    latest: str | None = None
    for row in rows:
        call_id = row.get("call_id")
        if row.get("event") == "call.start" and isinstance(call_id, str) and call_id:
            latest = call_id
    return latest


def _generation_set(
    rows: Iterable[dict[str, Any]], event: str
) -> set[int]:
    return {
        generation
        for row in rows
        if row.get("event") == event
        and isinstance((generation := row.get("generation")), int)
        and not isinstance(generation, bool)
    }


def _duration_seconds(rows: list[dict[str, Any]]) -> float | None:
    starts = [
        row for row in rows if row.get("event") == "call.start"
    ]
    ends = [row for row in rows if row.get("event") == "call.end"]
    if not starts or not ends:
        return None
    first = starts[0]
    last = ends[-1]
    start_mono = first.get("monotonic_us")
    end_mono = last.get("monotonic_us")
    if (
        isinstance(start_mono, int)
        and not isinstance(start_mono, bool)
        and isinstance(end_mono, int)
        and not isinstance(end_mono, bool)
        and end_mono >= start_mono
    ):
        return round((end_mono - start_mono) / 1_000_000, 3)
    try:
        start_wall = datetime.fromisoformat(str(first["ts"]))
        end_wall = datetime.fromisoformat(str(last["ts"]))
    except (KeyError, TypeError, ValueError):
        return None
    return round(max(0.0, (end_wall - start_wall).total_seconds()), 3)


def _message_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def _session_file_has_turn(path: Path, *, call_id: str, turn_id: str) -> bool:
    marker = (
        "<voice-turn-context>"
        + json.dumps(
            {"call_id": call_id, "turn_id": turn_id},
            separators=(",", ":"),
        )
        + "</voice-turn-context>"
    )
    saw_user = False
    try:
        handle = path.open("r", encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    try:
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                message = row.get("message")
                role = message.get("role") if isinstance(message, dict) else None
                text = _message_text(message)
                if role == "user":
                    if marker in text:
                        saw_user = True
                    elif saw_user:
                        return False
                elif role == "assistant" and saw_user and text.strip():
                    return True
    except UnicodeDecodeError:
        return False
    return False


def verify_session_turns(
    rows: list[dict[str, Any]],
    *,
    call_id: str,
    generations: set[int],
    projects_root: Path | None = None,
) -> dict[str, Any]:
    root = (projects_root or (Path.home() / ".claude" / "projects")).expanduser()
    done_rows = {
        row.get("generation"): row
        for row in rows
        if row.get("event") == "brain.done"
        and isinstance(row.get("generation"), int)
    }
    cache: dict[str, list[Path]] = {}
    verified: list[str] = []
    missing_session_id: list[str] = []
    missing_transcript: list[str] = []
    session_ids: set[str] = set()
    for generation in sorted(generations):
        turn_id = f"{call_id}:{generation}"
        meta = done_rows.get(generation, {}).get("meta")
        session_id = meta.get("session_id") if isinstance(meta, dict) else None
        if not isinstance(session_id, str) or not session_id.strip():
            missing_session_id.append(turn_id)
            continue
        session_id = session_id.strip()
        session_ids.add(session_id)
        if session_id not in cache:
            cache[session_id] = (
                list(root.rglob(f"{session_id}.jsonl")) if root.is_dir() else []
            )
        if any(
            _session_file_has_turn(path, call_id=call_id, turn_id=turn_id)
            for path in cache[session_id]
        ):
            verified.append(turn_id)
        else:
            missing_transcript.append(turn_id)
    complete = (
        len(verified) == len(generations)
        and not missing_session_id
        and not missing_transcript
    )
    return {
        "complete": complete,
        "expected_turns": len(generations),
        "verified_turns": len(verified),
        "session_ids": sorted(session_ids),
        "missing_session_id": missing_session_id,
        "missing_transcript": missing_transcript,
    }


def _event_rows_by_generation(
    rows: Iterable[dict[str, Any]], event: str
) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        generation = row.get("generation")
        if (
            row.get("event") == event
            and isinstance(generation, int)
            and not isinstance(generation, bool)
        ):
            grouped.setdefault(generation, []).append(row)
    return grouped


def _monotonic_us(row: dict[str, Any]) -> int | None:
    value = row.get("monotonic_us")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def verify_turn_pipeline(
    rows: list[dict[str, Any]],
    *,
    call_id: str,
    generations: set[int],
) -> dict[str, Any]:
    """Verify one causally ordered, session-linked completion per STT turn."""

    event_rows = {
        event: _event_rows_by_generation(rows, event)
        for event in (
            "stt.done",
            "brain.done",
            "audio.end",
            "playback.content_started",
        )
    }
    duplicate_events: list[str] = []
    invalid_metadata: list[str] = []
    ordering_violations: list[str] = []
    session_progress: dict[str, list[tuple[int, int, str]]] = {}

    for generation in sorted(generations):
        turn_id = f"{call_id}:{generation}"
        selected: dict[str, dict[str, Any]] = {}
        for event, grouped in event_rows.items():
            matches = grouped.get(generation, [])
            if len(matches) != 1:
                duplicate_events.append(f"{turn_id}:{event}:{len(matches)}")
                continue
            selected[event] = matches[0]

        brain_row = selected.get("brain.done")
        if brain_row is not None:
            meta = brain_row.get("meta")
            session_id = meta.get("session_id") if isinstance(meta, dict) else None
            done_turn_id = meta.get("turn_id") if isinstance(meta, dict) else None
            session_turns = (
                meta.get("session_turns") if isinstance(meta, dict) else None
            )
            valid_session_id = isinstance(session_id, str) and bool(
                session_id.strip()
            )
            valid_session_turns = (
                isinstance(session_turns, int)
                and not isinstance(session_turns, bool)
                and session_turns >= 1
            )
            if (
                not valid_session_id
                or done_turn_id != turn_id
                or not valid_session_turns
            ):
                invalid_metadata.append(turn_id)
            else:
                brain_time = _monotonic_us(brain_row)
                if brain_time is not None:
                    session_progress.setdefault(session_id.strip(), []).append(
                        (brain_time, session_turns, turn_id)
                    )

        if len(selected) != len(event_rows):
            continue
        times = {event: _monotonic_us(row) for event, row in selected.items()}
        if any(value is None for value in times.values()):
            ordering_violations.append(f"{turn_id}:missing_monotonic_time")
            continue
        stt_time = times["stt.done"]
        brain_time = times["brain.done"]
        audio_time = times["audio.end"]
        playback_time = times["playback.content_started"]
        assert stt_time is not None
        assert brain_time is not None
        assert audio_time is not None
        assert playback_time is not None
        if not stt_time <= brain_time <= audio_time:
            ordering_violations.append(f"{turn_id}:stt_brain_audio")
        if playback_time < stt_time:
            ordering_violations.append(f"{turn_id}:stt_playback")

    for progress in session_progress.values():
        ordered = sorted(progress)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current[1] <= previous[1]:
                ordering_violations.append(
                    f"{current[2]}:session_turns_not_increasing"
                )

    complete = not duplicate_events and not invalid_metadata and not ordering_violations
    return {
        "complete": complete,
        "duplicate_or_missing_events": duplicate_events,
        "invalid_done_metadata": invalid_metadata,
        "ordering_violations": ordering_violations,
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = position - lower
    return round(
        ordered[lower] * (1.0 - weight) + ordered[upper] * weight,
        3,
    )


def analyze_call(
    rows: Iterable[dict[str, Any]],
    *,
    call_id: str,
    criteria: IntegrityCriteria | None = None,
    projects_root: Path | None = None,
    heard_clean: bool = False,
) -> dict[str, Any]:
    criteria = criteria or IntegrityCriteria()
    selected = [row for row in rows if row.get("call_id") == call_id]
    if not selected:
        raise ValueError(f"call {call_id!r} has no telemetry")

    starts = [row for row in selected if row.get("event") == "call.start"]
    ends = [row for row in selected if row.get("event") == "call.end"]
    duration_seconds = _duration_seconds(selected)
    stt = _generation_set(selected, "stt.done")
    brain = _generation_set(selected, "brain.done")
    audio_end = _generation_set(selected, "audio.end")
    content_playback = _generation_set(selected, "playback.content_started")
    missing_brain = sorted(stt - brain)
    missing_audio_end = sorted(stt - audio_end)
    missing_playback = sorted(stt - content_playback)

    sequence_gaps = [row for row in selected if row.get("event") == "sequence.gap"]
    underruns = [row for row in selected if row.get("event") == "playback.underrun"]
    overflows = [
        row
        for row in selected
        if row.get("event") == "queue.depth" and row.get("full") is True
    ]
    errors = [row for row in selected if row.get("event") in {"error", "task.error"}]
    transcript = verify_session_turns(
        selected,
        call_id=call_id,
        generations=stt,
        projects_root=projects_root,
    )
    pipeline = verify_turn_pipeline(
        selected,
        call_id=call_id,
        generations=stt,
    )

    phone_latencies = [
        float(row["elapsed_ms"])
        for row in selected
        if row.get("event") == "latency.eou_first_content_playable_phone"
        and isinstance(row.get("elapsed_ms"), (int, float))
        and not isinstance(row.get("elapsed_ms"), bool)
    ]
    all_hello_rows = [
        row for row in selected if row.get("event") == "call.hello"
    ]
    hello_rows = [
        row
        for row in all_hello_rows
        if row.get("cold_start") is True
    ]
    cold_hello_ms = [
        int(row["app_uptime_ms"])
        for row in hello_rows
        if isinstance(row.get("app_uptime_ms"), int)
        and not isinstance(row.get("app_uptime_ms"), bool)
    ]
    cold_open_pass = (
        len(all_hello_rows) == 1
        and len(cold_hello_ms) == 1
        and cold_hello_ms[0] < criteria.max_cold_hello_ms
    )

    clean_hangup = bool(ends and ends[-1].get("clean_hangup") is True)
    duration_pass = (
        duration_seconds is not None
        and duration_seconds >= criteria.min_duration_seconds
    )
    turns_pass = len(stt) >= criteria.min_user_turns
    transport_integrity_pass = all(
        (
            bool(starts),
            clean_hangup,
            duration_pass,
            turns_pass,
            not sequence_gaps,
            not underruns,
            not overflows,
            not errors,
            not missing_brain,
            not missing_audio_end,
            not missing_playback,
            transcript["complete"],
            pipeline["complete"],
            cold_open_pass,
        )
    )
    objective_pass = transport_integrity_pass and heard_clean

    failures: list[str] = []
    if not starts:
        failures.append("call.start is missing")
    if not clean_hangup:
        failures.append("final call segment did not end with a clean hangup")
    if not duration_pass:
        failures.append(
            f"duration is below {criteria.min_duration_seconds:g} seconds"
        )
    if not turns_pass:
        failures.append(f"fewer than {criteria.min_user_turns} user turns completed")
    if sequence_gaps:
        failures.append(f"{len(sequence_gaps)} audio sequence gap events")
    if underruns:
        failures.append(f"{len(underruns)} Android playback underrun events")
    if overflows:
        failures.append(f"{len(overflows)} bounded queue overflow events")
    if errors:
        failures.append(f"{len(errors)} call error events")
    if missing_brain:
        failures.append(f"brain completion missing for generations {missing_brain}")
    if missing_audio_end:
        failures.append(f"audio end missing for generations {missing_audio_end}")
    if missing_playback:
        failures.append(f"content playback missing for generations {missing_playback}")
    if not transcript["complete"]:
        failures.append("one or more turns are not complete in the Claude session store")
    if not pipeline["complete"]:
        failures.append("one or more turns have invalid metadata or event ordering")
    if not cold_open_pass:
        failures.append("cold app hello evidence is missing, duplicated, or too slow")
    if not heard_clean:
        failures.append("physical acoustic clean-call attestation is missing")

    paths = Counter(
        str(row.get("path"))
        for row in selected
        if row.get("event") == "network.rtt"
        and row.get("path") in {"direct", "relay", "unknown"}
    )
    return {
        "ok": objective_pass,
        "acceptance_claim": objective_pass,
        "call_id": call_id,
        "criteria": {
            "min_duration_seconds": criteria.min_duration_seconds,
            "min_user_turns": criteria.min_user_turns,
            "max_cold_hello_ms": criteria.max_cold_hello_ms,
        },
        "duration_seconds": duration_seconds,
        "segments": len(starts),
        "reconnects": max(0, len(starts) - 1),
        "clean_hangup": clean_hangup,
        "user_turns": len(stt),
        "turns": {
            "stt_generations": sorted(stt),
            "brain_generations": sorted(brain),
            "missing_brain": missing_brain,
            "missing_audio_end": missing_audio_end,
            "missing_content_playback": missing_playback,
        },
        "transcript": transcript,
        "turn_pipeline": pipeline,
        "audio": {
            "sequence_gaps": len(sequence_gaps),
            "playback_underruns": len(underruns),
            "queue_overflows": len(overflows),
            "error_events": len(errors),
        },
        "network_paths": dict(sorted(paths.items())),
        "phone_eou_to_content_playback_ms": {
            "count": len(phone_latencies),
            "p90": _percentile(phone_latencies, 0.9),
            "max": round(max(phone_latencies), 3) if phone_latencies else None,
        },
        "cold_open": {
            "evidence": bool(cold_hello_ms),
            "hello_events": len(all_hello_rows),
            "hello_ms": cold_hello_ms,
            "pass": cold_open_pass,
        },
        "transport_integrity_pass": transport_integrity_pass,
        "heard_clean": heard_clean,
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS_PATH)
    parser.add_argument("--call-id")
    parser.add_argument("--projects-root", type=Path)
    parser.add_argument(
        "--min-duration-seconds",
        type=float,
        default=DEFAULT_MIN_DURATION_SECONDS,
    )
    parser.add_argument("--min-turns", type=int, default=DEFAULT_MIN_USER_TURNS)
    parser.add_argument(
        "--max-cold-hello-ms",
        type=int,
        default=DEFAULT_MAX_COLD_HELLO_MS,
    )
    parser.add_argument(
        "--heard-clean",
        action="store_true",
        help="attest that the physical phone call was audibly free of artifacts",
    )
    args = parser.parse_args(argv)
    rows = load_metrics(args.metrics)
    call_id = args.call_id or latest_call_id(rows)
    if not call_id:
        print(json.dumps({"ok": False, "error": "no call.start telemetry found"}))
        return 2
    try:
        report = analyze_call(
            rows,
            call_id=call_id,
            criteria=IntegrityCriteria(
                min_duration_seconds=args.min_duration_seconds,
                min_user_turns=args.min_turns,
                max_cold_hello_ms=args.max_cold_hello_ms,
            ),
            projects_root=args.projects_root,
            heard_clean=args.heard_clean,
        )
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
