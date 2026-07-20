"""Measure warm Pocket TTS first-PCM latency on the serving machine."""

from __future__ import annotations

import argparse
import json
import statistics
import time
import wave
from pathlib import Path
from typing import Any


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _run_once(model: Any, state: dict[str, Any], text: str) -> tuple[dict[str, Any], bytes]:
    import torch

    started = time.monotonic_ns()
    first_ms: float | None = None
    chunks = 0
    samples = 0
    pcm_parts: list[bytes] = []
    for chunk in model.generate_audio_stream(state, text):
        if first_ms is None:
            first_ms = (time.monotonic_ns() - started) / 1_000_000
        pcm = (chunk.clamp(-1, 1) * 32767).to(torch.int16).cpu().numpy().tobytes()
        pcm_parts.append(pcm)
        chunks += 1
        samples += len(pcm) // 2
    total_ms = (time.monotonic_ns() - started) / 1_000_000
    return (
        {
            "first_pcm_ms": round(first_ms or total_ms, 3),
            "total_ms": round(total_ms, 3),
            "chunks": chunks,
            "samples": samples,
        },
        b"".join(pcm_parts),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", default="hey, i'm here.")
    parser.add_argument("--voice", default="alba")
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--quantize", action="store_true")
    parser.add_argument("--output-wav")
    args = parser.parse_args(argv)

    import torch
    from pocket_tts import TTSModel

    if args.threads > 0:
        torch.set_num_threads(args.threads)
        torch.set_num_interop_threads(1)

    load_started = time.monotonic_ns()
    model = TTSModel.load_model(quantize=args.quantize)
    state = model.get_state_for_audio_prompt(args.voice)
    load_ms = (time.monotonic_ns() - load_started) / 1_000_000

    for _ in range(max(0, args.warmups)):
        _run_once(model, state, args.text)

    runs: list[dict[str, Any]] = []
    last_pcm = b""
    for _ in range(max(1, args.iterations)):
        run, last_pcm = _run_once(model, state, args.text)
        runs.append(run)

    first_values = [float(run["first_pcm_ms"]) for run in runs]
    total_values = [float(run["total_ms"]) for run in runs]
    result = {
        "ok": True,
        "voice": args.voice,
        "text": args.text,
        "quantized": args.quantize,
        "threads": torch.get_num_threads(),
        "sample_rate": model.sample_rate,
        "load_ms": round(load_ms, 3),
        "iterations": len(runs),
        "first_pcm_ms": {
            "p50": round(statistics.median(first_values), 3),
            "p90": round(_percentile(first_values, 0.9), 3),
            "p95": round(_percentile(first_values, 0.95), 3),
            "max": round(max(first_values), 3),
        },
        "total_ms": {
            "p50": round(statistics.median(total_values), 3),
            "p90": round(_percentile(total_values, 0.9), 3),
            "max": round(max(total_values), 3),
        },
        "runs": runs,
    }
    if args.output_wav:
        target = Path(args.output_wav).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(target), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(model.sample_rate)
            audio.writeframes(last_pcm)
        result["output_wav"] = str(target)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
