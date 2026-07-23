"""Benchmark and select Serena's local laptop voice profile.

The hard gate is realtime behavior on this machine.  The preference order is
explicit and reviewable rather than pretending waveform statistics can prove
that a voice sounds human.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
import wave
from array import array
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_REPORT = (
    Path.home() / ".local" / "state" / "serena" / "voice-quality.json"
)
DEFAULT_SAMPLES = (
    Path.home() / ".local" / "state" / "serena" / "voice-quality-samples"
)
CORPUS = (
    "hey raghav. i'm here.",
    "give me a second, i want to check that properly before i answer.",
    "that makes sense. the part i'd change first is the way she handles pauses.",
)


@dataclass(frozen=True, slots=True)
class VoiceCandidate:
    name: str
    backend: str
    voice: str
    preference: int
    style: str


CANDIDATES = (
    VoiceCandidate("pocket-alba", "pocket", "alba", 0, "casual"),
    VoiceCandidate("pocket-anna", "pocket", "anna", 1, "neutral"),
    VoiceCandidate("kokoro-af-heart", "kokoro", "af_heart", 2, "neutral"),
)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def choose_candidate(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the preferred voice only after it clears laptop realtime gates."""

    def metric(row: dict[str, Any], name: str, fallback: float) -> float:
        value = row.get(name)
        return fallback if value is None else float(value)

    passing = [
        row
        for row in results
        if row.get("ok") is True
        and metric(row, "first_pcm_p90_ms", 1e9) <= 250
        and metric(row, "realtime_factor_p90", 1e9) <= 1.0
        and metric(row, "clipped_sample_ratio", 1.0) <= 0.001
    ]
    if not passing:
        return None
    return min(passing, key=lambda row: (int(row["preference"]), row["name"]))


def apply_selection(report: dict[str, Any]) -> dict[str, Any]:
    """Re-evaluate a completed hardware report without rerunning the models."""

    updated = dict(report)
    results = list(updated.get("results") or [])
    chosen = choose_candidate(results)
    updated["selected"] = chosen["name"] if chosen else None
    updated["selected_backend"] = chosen["backend"] if chosen else None
    updated["selected_voice"] = chosen["voice"] if chosen else None
    updated["acceptance_claim"] = chosen is not None
    return updated


def _quality_stats(pcm: bytes) -> tuple[int, float]:
    samples = array("h")
    samples.frombytes(pcm)
    if not samples:
        return 0, 1.0
    clipped = sum(abs(value) >= 32760 for value in samples)
    return len(samples), clipped / len(samples)


async def _benchmark(
    candidate: VoiceCandidate,
    *,
    samples_dir: Path | None,
) -> dict[str, Any]:
    from voice.call.tts import KokoroOnnxBackend, PocketTTSBackend

    if candidate.backend == "pocket":
        backend = PocketTTSBackend(voice=candidate.voice)
    else:
        backend = KokoroOnnxBackend(voice=candidate.voice, speed=1.0)
    warm_started = time.monotonic_ns()
    try:
        await backend.warm()
        warm_ms = (time.monotonic_ns() - warm_started) / 1_000_000
        first_values: list[float] = []
        realtime_factors: list[float] = []
        all_pcm = bytearray()
        sample_rate = 24_000
        clipped = 0
        total_samples = 0
        for generation, text in enumerate(CORPUS, start=1):
            started = time.monotonic_ns()
            first_ms: float | None = None
            pcm = bytearray()
            async for chunk in backend.stream(text, generation=generation):
                if first_ms is None:
                    first_ms = (time.monotonic_ns() - started) / 1_000_000
                sample_rate = chunk.sample_rate
                pcm.extend(chunk.pcm)
            elapsed_ms = (time.monotonic_ns() - started) / 1_000_000
            samples, clipped_ratio = _quality_stats(bytes(pcm))
            duration_ms = samples / sample_rate * 1_000 if sample_rate else 0.0
            first_values.append(first_ms if first_ms is not None else elapsed_ms)
            realtime_factors.append(elapsed_ms / duration_ms if duration_ms else 99.0)
            clipped += round(clipped_ratio * samples)
            total_samples += samples
            all_pcm.extend(pcm)
            all_pcm.extend(b"\0\0" * int(sample_rate * 0.2))
            backend.retire_generation(generation)
        if samples_dir is not None:
            samples_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            output = samples_dir / f"{candidate.name}.wav"
            with wave.open(str(output), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(sample_rate)
                audio.writeframes(bytes(all_pcm))
        else:
            output = None
        return {
            **asdict(candidate),
            "ok": True,
            "warm_ms": round(warm_ms, 3),
            "first_pcm_p50_ms": round(statistics.median(first_values), 3),
            "first_pcm_p90_ms": round(_percentile(first_values, 0.9), 3),
            "realtime_factor_p90": round(_percentile(realtime_factors, 0.9), 4),
            "clipped_sample_ratio": round(
                clipped / total_samples if total_samples else 1.0,
                8,
            ),
            "sample": str(output) if output is not None else None,
        }
    except Exception as exc:
        return {
            **asdict(candidate),
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        await backend.cancel(None)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.pocket_python:
        os.environ["SERENA_CALL_POCKET_PYTHON"] = args.pocket_python
    if args.hf_home:
        os.environ["HF_HOME"] = args.hf_home
    samples = None if args.no_samples else Path(args.samples_dir).expanduser()
    selected_names = set(args.candidate or [item.name for item in CANDIDATES])
    candidates = [item for item in CANDIDATES if item.name in selected_names]
    unknown = selected_names - {item.name for item in candidates}
    if unknown:
        raise ValueError("unknown voice candidate(s): " + ", ".join(sorted(unknown)))
    results = []
    for candidate in candidates:
        results.append(await _benchmark(candidate, samples_dir=samples))
    return apply_selection({
        "schema_version": 1,
        "generated_at": time.time(),
        "offline_only": True,
        "selection_policy": {
            "first_pcm_p90_ms_max": 250,
            "realtime_factor_p90_max": 1.0,
            "clipped_sample_ratio_max": 0.001,
            "human_preference": "explicit curated order; listen to exported samples",
        },
        "results": results,
    })


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
    parser.add_argument("--candidate", action="append")
    parser.add_argument("--pocket-python")
    parser.add_argument("--hf-home")
    parser.add_argument("--samples-dir", default=str(DEFAULT_SAMPLES))
    parser.add_argument("--no-samples", action="store_true")
    parser.add_argument("--output", default=str(DEFAULT_REPORT))
    parser.add_argument(
        "--reselect-existing",
        action="store_true",
        help="apply the current gates to the existing output without loading models",
    )
    args = parser.parse_args(argv)
    if args.reselect_existing:
        try:
            existing = json.loads(
                Path(args.output).expanduser().read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"cannot read the existing report: {exc}")
        report = apply_selection(existing)
    else:
        try:
            report = asyncio.run(run(args))
        except ValueError as exc:
            parser.error(str(exc))
    _write_atomic(Path(args.output), report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["acceptance_claim"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
