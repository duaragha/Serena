"""Offline Pocket TTS streaming child process."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .process_worker import _install_network_guard


def _send(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_manifest(assets: list[tuple[str, Path]]) -> dict[str, Any]:
    unique: dict[str, tuple[str, Path]] = {}
    for source, path in assets:
        resolved = path.expanduser().resolve(strict=True)
        unique[str(resolved)] = (source, resolved)
    rows = []
    for source, path in sorted(unique.values(), key=lambda item: str(item[1])):
        rows.append(
            {
                "source": source,
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return {
        "asset_manifest_sha256": hashlib.sha256(encoded).hexdigest(),
        "asset_count": len(rows),
        "asset_bytes": sum(int(row["bytes"]) for row in rows),
    }


def _serve(config: dict[str, Any]) -> None:
    _install_network_guard()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

    import torch
    from pocket_tts import TTSModel
    from pocket_tts.models import tts_model as tts_model_module

    threads = max(1, int(config.get("threads", 2)))
    torch.set_num_threads(threads)
    torch.set_num_interop_threads(1)
    loaded_assets: list[tuple[str, Path]] = []
    original_download = tts_model_module.download_if_necessary

    def tracked_download(source: str) -> Path:
        path = Path(original_download(source))
        loaded_assets.append((source, path))
        return path

    tts_model_module.download_if_necessary = tracked_download
    try:
        model = TTSModel.load_model(
            language=str(config.get("language") or "english"),
            quantize=bool(config.get("quantize", True)),
        )
        voice = str(config.get("voice") or "anna")
        state = model.get_state_for_audio_prompt(voice)
    finally:
        tts_model_module.download_if_necessary = original_download
    backchannel_text = str(config.get("backchannel_text") or "yeah.").strip()
    if not backchannel_text:
        raise ValueError("Pocket TTS backchannel text cannot be empty")
    prime_started = time.monotonic()
    backchannel_parts = []
    for chunk in model.generate_audio_stream(state, backchannel_text):
        pcm = (
            (chunk.clamp(-1, 1) * 32767)
            .to(torch.int16)
            .cpu()
            .numpy()
            .tobytes()
        )
        if pcm:
            backchannel_parts.append(pcm)
    if not backchannel_parts:
        raise RuntimeError("Pocket TTS startup backchannel returned no audio")
    backchannel_pcm = b"".join(backchannel_parts)
    prime_ms = round((time.monotonic() - prime_started) * 1000, 3)
    assets = _asset_manifest(loaded_assets)
    _send(
        {
            "ok": True,
            "event": "ready",
            "meta": {
                "provider": "CPUExecutionProvider",
                "sample_rate": int(model.sample_rate),
                "voice": voice,
                "threads": threads,
                "quantized": bool(config.get("quantize", True)),
                "pid": os.getpid(),
                "primed": True,
                "prime_ms": prime_ms,
                "backchannel_text": backchannel_text,
                "backchannel_pcm_b64": base64.b64encode(backchannel_pcm).decode(
                    "ascii"
                ),
                "backchannel_sample_rate": int(model.sample_rate),
                "network_isolation": os.environ.get(
                    "SERENA_MODEL_NETWORK_ISOLATION", "none"
                ),
                **assets,
            },
        }
    )
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get("op") != "synthesise":
                raise ValueError("unknown Pocket TTS operation")
            generation = int(request.get("generation", 0))
            text = str(request.get("text") or "").strip()
            if not text:
                raise ValueError("Pocket TTS text cannot be empty")
            for chunk in model.generate_audio_stream(state, text):
                pcm = (
                    (chunk.clamp(-1, 1) * 32767)
                    .to(torch.int16)
                    .cpu()
                    .numpy()
                    .tobytes()
                )
                _send(
                    {
                        "ok": True,
                        "event": "chunk",
                        "generation": generation,
                        "sample_rate": int(model.sample_rate),
                        "pcm_b64": base64.b64encode(pcm).decode("ascii"),
                    }
                )
            _send(
                {
                    "ok": True,
                    "event": "done",
                    "generation": generation,
                }
            )
        except Exception as exc:
            _send({"ok": False, "event": "error", "error": str(exc)})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        _serve(json.loads(args.config))
    except Exception as exc:
        _send({"ok": False, "event": "ready", "error": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
