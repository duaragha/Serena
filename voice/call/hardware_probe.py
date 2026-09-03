"""Probe the real local VAD, STT, and TTS stages without a phone or brain."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import wave
from pathlib import Path
from typing import Any


def _elapsed_ms(started: int) -> float:
    return round((time.monotonic_ns() - started) / 1_000_000, 3)


async def _probe_vad() -> dict[str, Any]:
    from .endpoint import SileroProcessPool

    pool = SileroProcessPool(size=1)
    started = time.monotonic_ns()
    try:
        await asyncio.to_thread(pool.warm)
        warm_ms = _elapsed_ms(started)
        endpoint = pool.endpoint()
        endpoint.warm()
        inference_started = time.monotonic_ns()
        endpoint.process_pcm(b"\x00\x00" * 3_200)
        inference_ms = _elapsed_ms(inference_started)
        endpoint.close(reusable=False)
        return {"ok": True, "warm_ms": warm_ms, "frame_ms": inference_ms}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "elapsed_ms": _elapsed_ms(started)}


async def _probe_tts(
    backend_name: str = "kokoro",
    model: str | None = None,
    voices: str | None = None,
) -> dict[str, Any]:
    from .tts import KokoroOnnxBackend, PocketTTSBackend

    if backend_name == "pocket":
        backend = PocketTTSBackend()
    else:
        backend = KokoroOnnxBackend(model_path=model, voices_path=voices)
    started = time.monotonic_ns()
    try:
        await backend.warm()
        warm_ms = _elapsed_ms(started)
        inference_started = time.monotonic_ns()
        first_chunk = None
        chunks = 0
        samples = 0
        async for chunk in backend.stream("hello raghav.", generation=1):
            chunks += 1
            samples += len(chunk.pcm) // 2
            if first_chunk is None:
                first_chunk = _elapsed_ms(inference_started)
        return {
            "ok": True,
            "backend": backend.name,
            "provider": backend.provider,
            "warm_ms": warm_ms,
            "first_chunk_ms": first_chunk,
            "total_ms": _elapsed_ms(inference_started),
            "chunks": chunks,
            "samples": samples,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "elapsed_ms": _elapsed_ms(started)}
    finally:
        await backend.cancel(None)


def _read_stt_wav(path: str | None) -> bytes | None:
    if not path:
        return None
    with wave.open(str(Path(path).expanduser()), "rb") as audio:
        if audio.getnchannels() != 1:
            raise ValueError("STT probe WAV must be mono")
        if audio.getsampwidth() != 2:
            raise ValueError("STT probe WAV must be PCM16")
        if audio.getframerate() != 16_000:
            raise ValueError("STT probe WAV must be 16000 Hz")
        if audio.getcomptype() != "NONE":
            raise ValueError("STT probe WAV must be uncompressed PCM")
        return audio.readframes(audio.getnframes())


async def _probe_stt(model: str | None, pcm16: bytes | None = None) -> dict[str, Any]:
    from .stt import FasterWhisperWorker

    backend = FasterWhisperWorker(model=model)
    model_path = Path(backend.model_ref).expanduser()
    if not model_path.exists():
        return {
            "ok": False,
            "error": f"local Whisper model is missing at {model_path}",
            "device": backend.device.device,
            "compute_type": backend.device.compute_type,
        }
    started = time.monotonic_ns()
    try:
        await backend.warm()
        warm_ms = _elapsed_ms(started)
        inference_started = time.monotonic_ns()
        audio = pcm16 if pcm16 is not None else b"\x00\x00" * 16_000
        text = await backend.transcribe(audio, generation=1)
        result = {
            "ok": True,
            "device": backend.device.device,
            "compute_type": backend.device.compute_type,
            "warm_ms": warm_ms,
            "text": text,
            "audio_ms": round(len(audio) / 2 / 16_000 * 1_000, 3),
        }
        timing_key = "transcribe_ms" if pcm16 is not None else "silence_transcribe_ms"
        result[timing_key] = _elapsed_ms(inference_started)
        return result
    except Exception as exc:
        return {"ok": False, "error": str(exc), "elapsed_ms": _elapsed_ms(started)}
    finally:
        backend.close()


async def probe(args: argparse.Namespace) -> dict[str, Any]:
    if args.provider:
        os.environ["SERENA_CALL_ONNX_PROVIDER"] = args.provider
    if args.pocket_python:
        os.environ["SERENA_CALL_POCKET_PYTHON"] = args.pocket_python
    if args.pocket_voice:
        os.environ["SERENA_CALL_POCKET_VOICE"] = args.pocket_voice
    if args.pocket_threads:
        os.environ["SERENA_CALL_POCKET_THREADS"] = str(args.pocket_threads)
    import onnxruntime as ort

    result: dict[str, Any] = {
        "providers": list(ort.get_available_providers()),
        "requested_provider": args.provider or None,
    }
    if not args.skip_vad:
        result["vad"] = await _probe_vad()
    if not args.skip_tts:
        result["tts"] = await _probe_tts(
            args.tts_backend, args.tts_model, args.tts_voices
        )
    if not args.skip_stt:
        try:
            stt_audio = _read_stt_wav(args.stt_wav)
        except (OSError, ValueError, wave.Error) as exc:
            result["stt"] = {"ok": False, "error": str(exc)}
        else:
            result["stt"] = await _probe_stt(args.whisper_model, stt_audio)
    result["ok"] = all(
        stage.get("ok") is True
        for name, stage in result.items()
        if name in {"vad", "tts", "stt"}
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="")
    parser.add_argument("--tts-backend", choices=("kokoro", "pocket"), default="kokoro")
    parser.add_argument("--tts-model")
    parser.add_argument("--tts-voices")
    parser.add_argument("--pocket-python")
    parser.add_argument("--pocket-voice")
    parser.add_argument("--pocket-threads", type=int)
    parser.add_argument("--whisper-model")
    parser.add_argument("--stt-wav")
    parser.add_argument("--skip-vad", action="store_true")
    parser.add_argument("--skip-tts", action="store_true")
    parser.add_argument("--skip-stt", action="store_true")
    args = parser.parse_args(argv)
    result = asyncio.run(probe(args))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
