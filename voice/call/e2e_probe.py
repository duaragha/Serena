"""Drive one synthetic PTT turn through the real Tailnet call websocket."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import statistics
import time
import uuid
import wave
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .protocol import AudioFrame, AudioHeader, AudioKind, parse_audio_frame
from .tailnet import classify_tailscale_ping as _classify_tailscale_ping


def _pcm_frames(path: str | Path) -> list[bytes]:
    with wave.open(str(Path(path).expanduser()), "rb") as audio:
        if (
            audio.getnchannels() != 1
            or audio.getsampwidth() != 2
            or audio.getframerate() != 16_000
            or audio.getcomptype() != "NONE"
        ):
            raise ValueError("probe WAV must be mono PCM16 at 16000 Hz")
        pcm = audio.readframes(audio.getnframes())
    frame_bytes = 3_200 * 2
    frames = [pcm[offset : offset + frame_bytes] for offset in range(0, len(pcm), frame_bytes)]
    if not frames:
        raise ValueError("probe WAV is empty")
    if len(frames[-1]) < frame_bytes:
        frames[-1] += b"\x00" * (frame_bytes - len(frames[-1]))
    return frames


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


async def _probe_tailscale_path(
    url: str, *, attempts: int = 3, timeout_seconds: float = 5.0
) -> dict[str, Any]:
    executable = shutil.which("tailscale")
    host = urlsplit(url).hostname
    if not executable or not host:
        return {
            "path": "unknown",
            "source": "unavailable",
            "evidence": "tailscale CLI or websocket host is unavailable",
        }
    attempts = max(1, min(int(attempts), 10))
    timeout_seconds = max(1.0, min(float(timeout_seconds), 15.0))
    proc = await asyncio.create_subprocess_exec(
        executable,
        "ping",
        "--c",
        str(attempts),
        "--until-direct=false",
        "--timeout",
        f"{timeout_seconds:g}s",
        host,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(
            proc.communicate(), timeout=attempts * timeout_seconds + 3.0
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "path": "unknown",
            "source": "tailscale_probe",
            "evidence": "tailscale ping timed out",
        }
    text = output.decode("utf-8", errors="replace")
    path, evidence = _classify_tailscale_ping(text)
    return {
        "path": path,
        "source": "tailscale_probe" if path != "unknown" else "unknown",
        "evidence": evidence,
        "returncode": proc.returncode,
    }


async def _run_once(args: argparse.Namespace) -> dict[str, Any]:
    import websockets

    token = os.environ.get(args.token_env, "").strip()
    if not token:
        raise RuntimeError(f"{args.token_env} is required")
    frames = _pcm_frames(args.wav)
    call_id = f"probe-{uuid.uuid4().hex[:12]}"
    generation = 1
    connected_ns = time.monotonic_ns()
    timestamps: dict[str, int] = {}
    ready_payload: dict[str, Any] = {}
    stt_text = ""
    brain_text: list[str] = []
    output_frames = 0
    output_samples = 0
    output_rate = 0
    current_segment_kind = "content"
    receiver_error: BaseException | None = None
    ready = asyncio.Event()
    generation_ready = asyncio.Event()
    audio_ended = asyncio.Event()
    call_ended = asyncio.Event()
    pong_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}

    path_measurement = await _probe_tailscale_path(
        args.url,
        attempts=args.tailscale_pings,
        timeout_seconds=args.tailscale_timeout,
    )
    measured_path = str(path_measurement["path"])
    if args.expect_path and measured_path != args.expect_path:
        raise RuntimeError(
            f"tailscale path is {measured_path}, expected {args.expect_path}: "
            f"{path_measurement.get('evidence', '')}"
        )

    async with websockets.connect(
        args.url,
        extra_headers={"Authorization": f"Bearer {token}"},
        max_size=4 * 1024 * 1024,
        ping_interval=None,
        close_timeout=3,
    ) as socket:

        async def receive() -> None:
            nonlocal ready_payload, stt_text, output_frames
            nonlocal output_samples, output_rate, receiver_error
            nonlocal current_segment_kind
            try:
                async for message in socket:
                    now = time.monotonic_ns()
                    if isinstance(message, bytes):
                        frame = parse_audio_frame(message, inbound=False)
                        timestamps.setdefault("first_pcm", now)
                        if current_segment_kind == "content":
                            timestamps.setdefault("first_content_pcm", now)
                        output_frames += 1
                        output_samples += len(frame.pcm) // 2
                        output_rate = frame.header.sample_rate
                        continue
                    payload = json.loads(message)
                    kind = str(payload.get("type") or "")
                    timestamps.setdefault(kind, now)
                    if kind == "error":
                        error = RuntimeError(
                            f"call error {payload.get('code')}: {payload.get('message')}"
                        )
                        for waiter in pong_waiters.values():
                            if not waiter.done():
                                waiter.set_exception(error)
                        raise error
                    if kind == "call.ready":
                        ready_payload = payload
                        ready.set()
                    elif kind == "generation" and payload.get("generation") == generation:
                        generation_ready.set()
                    elif kind == "stt.result":
                        stt_text = str(payload.get("text") or "")
                    elif kind == "brain.delta":
                        brain_text.append(str(payload.get("delta") or ""))
                    elif kind == "pong":
                        nonce = str(payload.get("nonce") or "")
                        waiter = pong_waiters.get(nonce)
                        if waiter is not None and not waiter.done():
                            waiter.set_result(payload)
                    elif kind == "audio.segment":
                        current_segment_kind = str(
                            payload.get("kind") or "content"
                        )
                    elif kind == "audio.end":
                        audio_ended.set()
                    elif kind == "call.ended":
                        call_ended.set()
            except BaseException as exc:
                receiver_error = exc
                for waiter in pong_waiters.values():
                    if not waiter.done():
                        waiter.set_exception(exc)
                ready.set()
                generation_ready.set()
                audio_ended.set()
                call_ended.set()

        receiver = asyncio.create_task(receive(), name="call-e2e-probe-receiver")
        await socket.send(
            json.dumps(
                {
                    "type": "call.start",
                    "call_id": call_id,
                    "continuity": False,
                }
            )
        )
        await asyncio.wait_for(ready.wait(), timeout=args.timeout)
        if receiver_error is not None:
            raise receiver_error
        if not ready_payload.get("ready"):
            raise RuntimeError(f"call runtime is not ready: {ready_payload}")

        rtt_values: list[float] = []
        for _ in range(max(1, args.rtt_samples)):
            nonce = uuid.uuid4().hex[:12]
            waiter = asyncio.get_running_loop().create_future()
            pong_waiters[nonce] = waiter
            started = time.monotonic_ns()
            await socket.send(
                json.dumps(
                    {
                        "type": "ping",
                        "nonce": nonce,
                        "sent_at_us": started // 1_000,
                    }
                )
            )
            pong = await asyncio.wait_for(waiter, timeout=5)
            pong_waiters.pop(nonce, None)
            raw_rtt_ms = (time.monotonic_ns() - started) / 1_000_000
            server_ms = max(0, int(pong.get("server_processing_us", 0))) / 1_000
            sample_rtt_ms = max(0.0, raw_rtt_ms - server_ms)
            rtt_values.append(sample_rtt_ms)
            sample_id = str(pong.get("sample_id") or "")
            if not sample_id:
                raise RuntimeError("call pong did not include a server sample_id")
            await socket.send(
                json.dumps(
                    {
                        "type": "rtt.report",
                        "rtt_ms": sample_rtt_ms,
                        "path": pong.get("path", "unknown"),
                        "path_source": pong.get("path_source", "unknown"),
                        "sample_id": sample_id,
                    }
                )
            )
        rtt_ms = statistics.median(rtt_values)

        await socket.send(
            json.dumps({"type": "ptt.begin", "generation": generation})
        )
        await asyncio.wait_for(generation_ready.wait(), timeout=args.timeout)
        if receiver_error is not None:
            raise receiver_error
        capture_started_ns = time.monotonic_ns()
        for sequence, pcm in enumerate(frames):
            captured_ns = time.monotonic_ns()
            packet = AudioFrame(
                AudioHeader(
                    kind=AudioKind.MIC_PCM16,
                    flags=0,
                    sequence=sequence,
                    sample_rate=16_000,
                    timestamp_us=captured_ns // 1_000,
                ),
                pcm,
            ).pack()
            await socket.send(packet)
            if args.realtime:
                await asyncio.sleep(0.2)
        eou_ns = time.monotonic_ns()
        await socket.send(
            json.dumps(
                {
                    "type": "ptt.end",
                    "generation": generation,
                    "eou_monotonic_us": eou_ns // 1_000,
                }
            )
        )
        await asyncio.wait_for(audio_ended.wait(), timeout=args.timeout)
        if receiver_error is not None:
            raise receiver_error
        await socket.send(json.dumps({"type": "hangup"}))
        await asyncio.wait_for(call_ended.wait(), timeout=10)
        if receiver_error is not None:
            raise receiver_error
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(receiver), timeout=1.0)
        if receiver_error is not None:
            raise receiver_error
        await socket.close()
        await asyncio.gather(receiver, return_exceptions=True)
        if receiver_error is not None:
            raise receiver_error

    def since_eou(name: str) -> float | None:
        value = timestamps.get(name)
        return round((value - eou_ns) / 1_000_000, 3) if value else None

    return {
        "ok": True,
        "synthetic_websocket_path": True,
        "physical_playback_measured": False,
        "acceptance_claim": False,
        "url": args.url,
        "path": measured_path,
        "path_measurement": path_measurement,
        "call_ready_ms": round(
            (timestamps["call.ready"] - connected_ns) / 1_000_000, 3
        ),
        "rtt_ms": {
            "samples": [round(value, 3) for value in rtt_values],
            "p50": round(rtt_ms, 3),
            "max": round(max(rtt_values), 3),
        },
        "input": {
            "frames": len(frames),
            "audio_ms": len(frames) * 200,
            "wall_ms": round((eou_ns - capture_started_ns) / 1_000_000, 3),
        },
        "stt_text": stt_text,
        "brain_text": "".join(brain_text).strip(),
        "eou_to_ms": {
            "stt_result": since_eou("stt.result"),
            "first_brain_delta": since_eou("brain.delta"),
            "audio_start": since_eou("audio.start"),
            "first_pcm_received": since_eou("first_pcm"),
            "first_content_pcm_received": since_eou("first_content_pcm"),
            "audio_end": since_eou("audio.end"),
        },
        "output": {
            "frames": output_frames,
            "samples": output_samples,
            "sample_rate": output_rate,
        },
        "models": ready_payload.get("models"),
        "model_details": ready_payload.get("model_details"),
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.iterations <= 1:
        return await _run_once(args)
    runs: list[dict[str, Any]] = []
    for _ in range(args.iterations):
        try:
            runs.append(await _run_once(args))
        except Exception as exc:
            runs.append(
                {
                    "ok": False,
                    "error": str(exc),
                    "acceptance_claim": False,
                }
            )
    latencies = [
        float(run["eou_to_ms"]["first_pcm_received"])
        for run in runs
        if run.get("ok")
        and isinstance(run.get("eou_to_ms"), dict)
        and run["eou_to_ms"].get("first_pcm_received") is not None
    ]
    content_latencies = [
        float(run["eou_to_ms"]["first_content_pcm_received"])
        for run in runs
        if run.get("ok")
        and isinstance(run.get("eou_to_ms"), dict)
        and run["eou_to_ms"].get("first_content_pcm_received") is not None
    ]
    summary: dict[str, float | int | None] = {"count": len(latencies)}
    if latencies:
        summary.update(
            {
                "p50": round(statistics.median(latencies), 3),
                "p90": round(_percentile(latencies, 0.9), 3),
                "p95": round(_percentile(latencies, 0.95), 3),
                "max": round(max(latencies), 3),
            }
        )
    else:
        summary.update({"p50": None, "p90": None, "p95": None, "max": None})
    content_summary: dict[str, float | int | None] = {
        "count": len(content_latencies)
    }
    if content_latencies:
        content_summary.update(
            {
                "p50": round(statistics.median(content_latencies), 3),
                "p90": round(_percentile(content_latencies, 0.9), 3),
                "p95": round(_percentile(content_latencies, 0.95), 3),
                "max": round(max(content_latencies), 3),
            }
        )
    else:
        content_summary.update(
            {"p50": None, "p90": None, "p95": None, "max": None}
        )
    return {
        "ok": all(run.get("ok") is True for run in runs),
        "synthetic_websocket_path": True,
        "physical_playback_measured": False,
        "acceptance_claim": False,
        "iterations": len(runs),
        "successful": sum(run.get("ok") is True for run in runs),
        "eou_to_first_pcm_received_ms": summary,
        "eou_to_first_content_pcm_received_ms": content_summary,
        "runs": runs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--wav", required=True)
    parser.add_argument("--token-env", default="SERENA_CALL_TOKEN")
    parser.add_argument("--expect-path", choices=("direct", "relay"))
    parser.add_argument("--tailscale-pings", type=int, default=3)
    parser.add_argument("--tailscale-timeout", type=float, default=5.0)
    parser.add_argument("--rtt-samples", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="omit per-turn records from multi-run output",
    )
    parser.add_argument("--realtime", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:
        result = {
            "ok": False,
            "synthetic_websocket_path": True,
            "physical_playback_measured": False,
            "acceptance_claim": False,
            "error": str(exc),
        }
    if args.summary_only and isinstance(result.get("runs"), list):
        result = {key: value for key, value in result.items() if key != "runs"}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
