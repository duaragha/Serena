"""Call telemetry with no transcript or raw audio persistence."""

from __future__ import annotations

import json
import math
import statistics
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_METRICS_PATH = Path.home() / ".config" / "serena" / "call_metrics.jsonl"
MAX_RTT_SAMPLE_AGE_MS = 15_000.0


class CallTelemetry:
    def __init__(
        self,
        call_id: str,
        path: Path | None = None,
        *,
        lifecycle_id: str | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.call_id = call_id
        self.lifecycle_id = str(uuid.UUID(lifecycle_id)) if lifecycle_id else str(uuid.uuid4())
        self.path = Path(path or DEFAULT_METRICS_PATH)
        self._clock_ns = clock_ns
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._rtts: deque[float] = deque(maxlen=20)
        self._path = "unknown"
        self._path_source = "unknown"
        self._rtt_sample_id: str | None = None
        self._last_rtt_ns: int | None = None
        self._speech_end_ns: dict[int, int] = {}
        self._first_audio_ns: dict[int, int] = {}
        self._first_content_audio_ns: dict[int, int] = {}
        self._first_content_playback_ns: dict[int, int] = {}
        self._stage_ns: dict[int, dict[str, int]] = {}

    def retire_generation(self, generation: int) -> None:
        """Release per-turn timing state once no later metric can use it."""
        self._speech_end_ns.pop(generation, None)
        self._first_audio_ns.pop(generation, None)
        self._first_content_audio_ns.pop(generation, None)
        self._first_content_playback_ns.pop(generation, None)
        self._stage_ns.pop(generation, None)

    @property
    def rolling_rtt_ms(self) -> float | None:
        if not self._rtts:
            return None
        return round(statistics.fmean(self._rtts), 3)

    @property
    def network_path(self) -> str:
        return self._path

    def _rtt_age_ms(self, now_ns: int | None = None) -> float | None:
        if self._last_rtt_ns is None:
            return None
        current = self._clock_ns() if now_ns is None else now_ns
        return round((current - self._last_rtt_ns) / 1_000_000, 3)

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        now_ns = self._clock_ns()
        wall = self._wall_clock()
        row = {
            "schema_version": 1,
            "ts": datetime.fromtimestamp(wall, timezone.utc).isoformat(),
            "monotonic_us": now_ns // 1_000,
            "clock_domain": "server_monotonic",
            "call_id": self.call_id,
            "lifecycle_id": self.lifecycle_id,
            "event": event,
            **_json_safe(fields),
        }
        generation = row.get("generation")
        if isinstance(generation, int) and not isinstance(generation, bool):
            row.setdefault("turn_id", f"{self.call_id}:{generation}")
        data = json.dumps(row, separators=(",", ":"), sort_keys=True)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(data + "\n")
        return row

    def receive_batch(
        self,
        *,
        generation: int,
        sequence: int,
        samples: int,
        queue_depth: int,
        client_monotonic_us: int,
    ) -> None:
        now_ns = self._clock_ns()
        rtt_age_ms = self._rtt_age_ms(now_ns)
        self.record(
            "receive.batch",
            generation=generation,
            sequence=sequence,
            samples=samples,
            queue_depth=queue_depth,
            client_monotonic_us=client_monotonic_us,
            rolling_ws_rtt_ms=self.rolling_rtt_ms,
            rtt_sample_age_ms=rtt_age_ms,
            rtt_stale=(rtt_age_ms is not None and rtt_age_ms > MAX_RTT_SAMPLE_AGE_MS),
            network_sample_id=self._rtt_sample_id,
            path=self.network_path,
            path_source=self._path_source,
        )

    def sequence_gap(
        self, *, direction: str, generation: int, expected: int, received: int
    ) -> None:
        self.record(
            "sequence.gap",
            direction=direction,
            generation=generation,
            expected=expected,
            received=received,
        )

    def report_rtt(
        self,
        rtt_ms: float,
        path: str,
        *,
        path_source: str = "unknown",
        sample_id: str | None = None,
    ) -> None:
        if path not in {"direct", "relay", "unknown"}:
            raise ValueError("network path must be direct, relay, or unknown")
        if path_source not in {"client_config", "tailscale_probe", "unknown"}:
            raise ValueError("network path source is invalid")
        self._rtts.append(float(rtt_ms))
        self._path = path
        self._path_source = path_source
        self._rtt_sample_id = sample_id
        self._last_rtt_ns = self._clock_ns()
        self.record(
            "network.rtt",
            ws_rtt_ms=round(float(rtt_ms), 3),
            rolling_ws_rtt_ms=self.rolling_rtt_ms,
            path=path,
            path_source=path_source,
            network_sample_id=sample_id,
        )

    def speech_end(
        self,
        generation: int,
        *,
        source: str,
        trailing_silence_samples: int = 0,
        sample_rate: int = 16_000,
        client_eou_us: int | None = None,
    ) -> None:
        endpoint_delay_ns = int(trailing_silence_samples * 1_000_000_000 / sample_rate)
        self._speech_end_ns[generation] = self._clock_ns() - endpoint_delay_ns
        self.record(
            "speech.end",
            generation=generation,
            source=source,
            endpoint_delay_ms=round(endpoint_delay_ns / 1_000_000, 3),
            client_eou_us=client_eou_us,
        )

    def stage(self, generation: int, stage: str, **fields: Any) -> bool:
        """Record one monotonic stage marker for a generation.

        The cumulative value starts at the server's reconstructed EOU. The
        segment value starts at the preceding stage marker, which makes the
        slowest local stage visible without comparing clocks across devices.
        """
        now_ns = self._clock_ns()
        stages = self._stage_ns.setdefault(generation, {})
        if stage in stages:
            return False
        previous_stage = next(reversed(stages), None)
        previous_ns = stages.get(previous_stage) if previous_stage else None
        stages[stage] = now_ns
        speech_end = self._speech_end_ns.get(generation)
        rtt_age_ms = self._rtt_age_ms(now_ns)
        self.record(
            "latency.stage",
            generation=generation,
            stage=stage,
            from_eou_ms=(
                None if speech_end is None else round((now_ns - speech_end) / 1_000_000, 3)
            ),
            since_previous_stage_ms=(
                None if previous_ns is None else round((now_ns - previous_ns) / 1_000_000, 3)
            ),
            previous_stage=previous_stage,
            rolling_ws_rtt_ms=self.rolling_rtt_ms,
            rtt_sample_age_ms=rtt_age_ms,
            rtt_stale=(rtt_age_ms is not None and rtt_age_ms > MAX_RTT_SAMPLE_AGE_MS),
            network_sample_id=self._rtt_sample_id,
            path=self.network_path,
            path_source=self._path_source,
            **fields,
        )
        return True

    def first_audio_send(self, generation: int, *, sequence: int, kind: str = "content") -> None:
        now_ns = self._clock_ns()
        if generation in self._first_audio_ns:
            return
        self._first_audio_ns[generation] = now_ns
        self.stage(
            generation,
            "server.first_audio_send",
            sequence=sequence,
            kind=kind,
        )
        self.record(
            "audio.first_send",
            generation=generation,
            sequence=sequence,
            kind=kind,
        )
        speech_end = self._speech_end_ns.get(generation)
        if speech_end is None:
            return
        server_ms = (now_ns - speech_end) / 1_000_000
        rtt_ms = self.rolling_rtt_ms
        rtt_age_ms = self._rtt_age_ms(now_ns)
        rtt_stale = rtt_age_ms is None or rtt_age_ms > MAX_RTT_SAMPLE_AGE_MS
        self.record(
            "latency.eou_first_playable_estimate",
            generation=generation,
            server_ms=round(server_ms, 3),
            round_trip_rtt_ms=rtt_ms,
            estimate_ms=(None if rtt_ms is None or rtt_stale else round(server_ms + rtt_ms, 3)),
            rtt_sample_age_ms=rtt_age_ms,
            rtt_stale=rtt_stale,
            network_sample_id=self._rtt_sample_id,
            path=self.network_path,
            path_source=self._path_source,
            measurement="server_monotonic_plus_rolling_rtt",
            kind=kind,
        )

    def first_content_audio_send(self, generation: int, *, sequence: int) -> None:
        now_ns = self._clock_ns()
        if generation in self._first_content_audio_ns:
            return
        self._first_content_audio_ns[generation] = now_ns
        self.stage(
            generation,
            "server.first_content_audio_send",
            sequence=sequence,
        )
        self.record(
            "audio.first_content_send",
            generation=generation,
            sequence=sequence,
        )
        speech_end = self._speech_end_ns.get(generation)
        if speech_end is None:
            return
        server_ms = (now_ns - speech_end) / 1_000_000
        rtt_ms = self.rolling_rtt_ms
        rtt_age_ms = self._rtt_age_ms(now_ns)
        rtt_stale = rtt_age_ms is None or rtt_age_ms > MAX_RTT_SAMPLE_AGE_MS
        self.record(
            "latency.eou_first_content_playable_estimate",
            generation=generation,
            server_ms=round(server_ms, 3),
            round_trip_rtt_ms=rtt_ms,
            estimate_ms=(None if rtt_ms is None or rtt_stale else round(server_ms + rtt_ms, 3)),
            rtt_sample_age_ms=rtt_age_ms,
            rtt_stale=rtt_stale,
            network_sample_id=self._rtt_sample_id,
            path=self.network_path,
            path_source=self._path_source,
            measurement="server_monotonic_plus_rolling_rtt",
        )

    def playback_started(
        self,
        generation: int,
        *,
        sequence: int | None,
        timestamp_us: int | None = None,
        eou_to_playback_ms: float | None = None,
        first_output_to_playback_ms: float | None = None,
        first_pcm_write_to_playback_ms: float | None = None,
        play_return_to_head_ms: float | None = None,
        measurement_point: str | None = None,
        kind: str = "content",
    ) -> None:
        now_ns = self._clock_ns()
        self.record(
            "playback.started",
            generation=generation,
            sequence=sequence,
            client_monotonic_us=timestamp_us,
            eou_to_playback_ms=eou_to_playback_ms,
            first_output_to_playback_ms=first_output_to_playback_ms,
            first_pcm_write_to_playback_ms=first_pcm_write_to_playback_ms,
            play_return_to_head_ms=play_return_to_head_ms,
            measurement_point=measurement_point,
            kind=kind,
        )
        self.stage(
            generation,
            "server.playback_ack_received",
            sequence=sequence,
            measurement_point=measurement_point,
        )
        if eou_to_playback_ms is not None and _finite_number(eou_to_playback_ms):
            event = (
                "latency.eou_first_playable_phone"
                if measurement_point == "playback_head_advanced"
                else "latency.eou_audio_track_play_return"
            )
            self.record(
                event,
                generation=generation,
                elapsed_ms=round(float(eou_to_playback_ms), 3),
                measurement="single_phone_monotonic_clock",
                measurement_clock_domain="android_monotonic",
                measurement_point=measurement_point,
                first_output_to_playback_ms=first_output_to_playback_ms,
                first_pcm_write_to_playback_ms=first_pcm_write_to_playback_ms,
                play_return_to_head_ms=play_return_to_head_ms,
                rolling_ws_rtt_ms=self.rolling_rtt_ms,
                rtt_sample_age_ms=self._rtt_age_ms(now_ns),
                network_sample_id=self._rtt_sample_id,
                path=self.network_path,
                path_source=self._path_source,
                kind=kind,
            )
        speech_end = self._speech_end_ns.get(generation)
        if speech_end is not None:
            self.record(
                "latency.eou_first_playable_ack",
                generation=generation,
                elapsed_ms=round((now_ns - speech_end) / 1_000_000, 3),
                measurement="server_receive_to_ack_proxy",
                rolling_ws_rtt_ms=self.rolling_rtt_ms,
                path=self.network_path,
                path_source=self._path_source,
                kind=kind,
            )

    def content_playback_started(
        self,
        generation: int,
        *,
        sequence: int,
        timestamp_us: int | None = None,
        eou_to_playback_ms: float | None = None,
        measurement_point: str | None = None,
    ) -> None:
        if generation in self._first_content_playback_ns:
            return
        self._first_content_playback_ns[generation] = self._clock_ns()
        self.record(
            "playback.content_started",
            generation=generation,
            sequence=sequence,
            client_monotonic_us=timestamp_us,
            eou_to_playback_ms=eou_to_playback_ms,
            measurement_point=measurement_point,
        )
        self.stage(
            generation,
            "server.content_playback_ack_received",
            sequence=sequence,
            measurement_point=measurement_point,
        )
        if eou_to_playback_ms is not None and _finite_number(eou_to_playback_ms):
            self.record(
                "latency.eou_first_content_playable_phone",
                generation=generation,
                elapsed_ms=round(float(eou_to_playback_ms), 3),
                measurement="single_phone_monotonic_clock",
                measurement_clock_domain="android_monotonic",
                measurement_point=measurement_point,
                sequence=sequence,
                rolling_ws_rtt_ms=self.rolling_rtt_ms,
                rtt_sample_age_ms=self._rtt_age_ms(),
                network_sample_id=self._rtt_sample_id,
                path=self.network_path,
                path_source=self._path_source,
            )

    def call_hello(
        self,
        generation: int,
        *,
        app_uptime_ms: int,
        cold_start: bool,
    ) -> None:
        self.record(
            "call.hello",
            generation=generation,
            app_uptime_ms=app_uptime_ms,
            cold_start=cold_start,
            measurement="android_process_uptime_to_content_playback_head",
        )


def percentile_summary(
    path: Path = DEFAULT_METRICS_PATH,
    *,
    event: str = "latency.eou_first_playable_estimate",
    field: str = "estimate_ms",
) -> dict[str, Any]:
    values: list[float] = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        value = row.get(field)
        if row.get("event") == event and _finite_number(value):
            values.append(float(value))
    values.sort()
    return summarize_values(values, event=event, field=field)


def stage_percentile_summary(
    path: Path = DEFAULT_METRICS_PATH,
    *,
    field: str = "since_previous_stage_ms",
    completed_only: bool = True,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[float]] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    parsed: list[dict[str, Any]] = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    completed = {
        (row.get("call_id"), row.get("generation"))
        for row in parsed
        if row.get("event") == "latency.eou_first_playable_phone"
    }
    for row in parsed:
        stage = row.get("stage")
        value = row.get(field)
        if (
            row.get("event") == "latency.stage"
            and (not completed_only or (row.get("call_id"), row.get("generation")) in completed)
            and isinstance(stage, str)
            and stage
            and _finite_number(value)
        ):
            grouped.setdefault(stage, []).append(float(value))
    return {
        stage: summarize_values(
            values,
            event=f"latency.stage.{stage}",
            field=field,
        )
        for stage, values in sorted(grouped.items())
    }


def summarize_values(values: Iterable[float], *, event: str, field: str) -> dict[str, Any]:
    ordered = sorted(float(value) for value in values if _finite_number(value))
    return {
        "event": event,
        "field": field,
        "count": len(ordered),
        "p50": _percentile(ordered, 50),
        "p90": _percentile(ordered, 90),
        "p95": _percentile(ordered, 95),
        "p99": _percentile(ordered, 99),
        "min": ordered[0] if ordered else None,
        "max": ordered[-1] if ordered else None,
    }


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 3)
    rank = (len(values) - 1) * percentile / 100
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return round(values[low], 3)
    weight = rank - low
    return round(values[low] * (1 - weight) + values[high] * weight, 3)


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _json_safe(fields: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        if (
            value is None
            or isinstance(value, (str, int, bool))
            or isinstance(value, float)
            and math.isfinite(value)
        ):
            safe[key] = value
        elif isinstance(value, dict):
            safe[key] = _json_safe(value)
        elif isinstance(value, (list, tuple)):
            safe[key] = [
                item for item in value if item is None or isinstance(item, (str, int, float, bool))
            ]
        else:
            safe[key] = str(value)
    return safe
