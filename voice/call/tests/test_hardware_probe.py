from __future__ import annotations

import argparse
import asyncio
import json
import wave

import pytest

from voice.call import e2e_probe
from voice.call.e2e_probe import _classify_tailscale_ping, _pcm_frames
from voice.call.hardware_probe import _read_stt_wav


def test_read_stt_wav_accepts_call_format(tmp_path):
    target = tmp_path / "speech.wav"
    pcm = b"\x01\x00" * 320
    with wave.open(str(target), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(pcm)

    assert _read_stt_wav(str(target)) == pcm


def test_read_stt_wav_rejects_wrong_sample_rate(tmp_path):
    target = tmp_path / "speech.wav"
    with wave.open(str(target), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24_000)
        audio.writeframes(b"\x00\x00" * 320)

    with pytest.raises(ValueError, match="16000 Hz"):
        _read_stt_wav(str(target))


def test_e2e_probe_pads_final_pcm_to_one_transport_frame(tmp_path):
    target = tmp_path / "speech.wav"
    pcm = b"\x01\x00" * 3_300
    with wave.open(str(target), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(pcm)

    frames = _pcm_frames(target)

    assert len(frames) == 2
    assert all(len(frame) == 6_400 for frame in frames)
    assert frames[1].startswith(b"\x01\x00" * 100)
    assert frames[1][200:] == b"\x00" * 6_200


@pytest.mark.parametrize(
    "output, expected",
    [
        (
            "pong from pc (100.116.233.56) via 99.234.238.73:12566 in 26ms\n",
            "direct",
        ),
        (
            "pong from pc (100.116.233.56) via DERP(tor) in 43ms\n",
            "relay",
        ),
        ("no matching peer\n", "unknown"),
    ],
)
def test_e2e_probe_classifies_measured_tailscale_route(output, expected):
    path, evidence = _classify_tailscale_ping(output)

    assert path == expected
    assert evidence


def test_e2e_probe_fails_when_server_rejects_hangup(monkeypatch):
    async def handler(socket):
        async for raw in socket:
            if not isinstance(raw, str):
                continue
            message = json.loads(raw)
            kind = message["type"]
            if kind == "call.start":
                assert message["continuity"] is False
                await socket.send(
                    json.dumps({"type": "call.ready", "ready": True, "models": {}})
                )
            elif kind == "ping":
                await socket.send(
                    json.dumps(
                        {
                            "type": "pong",
                            "nonce": message["nonce"],
                            "sample_id": "sample-1",
                            "server_processing_us": 0,
                        }
                    )
                )
            elif kind == "ptt.begin":
                await socket.send(json.dumps({"type": "generation", "generation": 1}))
            elif kind == "ptt.end":
                await socket.send(json.dumps({"type": "audio.end"}))
            elif kind == "hangup":
                await socket.send(
                    json.dumps(
                        {
                            "type": "error",
                            "code": "hangup_failed",
                            "message": "server rejected hangup",
                        }
                    )
                )
                return

    async def no_path(*args, **kwargs):
        return {"path": "unknown", "source": "test", "evidence": "test"}

    async def scenario() -> None:
        import websockets

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            args = argparse.Namespace(
                url=f"ws://127.0.0.1:{port}",
                wav="unused.wav",
                token_env="SERENA_CALL_TOKEN",
                expect_path=None,
                tailscale_pings=1,
                tailscale_timeout=1.0,
                rtt_samples=1,
                timeout=1.0,
                realtime=False,
            )
            with pytest.raises(RuntimeError, match="hangup_failed"):
                await e2e_probe._run_once(args)

    monkeypatch.setenv("SERENA_CALL_TOKEN", "test")
    monkeypatch.setattr(e2e_probe, "_pcm_frames", lambda _path: [b"\0" * 6_400])
    monkeypatch.setattr(e2e_probe, "_probe_tailscale_path", no_path)
    asyncio.run(scenario())


def test_e2e_probe_fails_on_error_just_after_call_ended(monkeypatch):
    async def handler(socket):
        async for raw in socket:
            if not isinstance(raw, str):
                continue
            message = json.loads(raw)
            kind = message["type"]
            if kind == "call.start":
                await socket.send(
                    json.dumps({"type": "call.ready", "ready": True, "models": {}})
                )
            elif kind == "ping":
                await socket.send(
                    json.dumps(
                        {
                            "type": "pong",
                            "nonce": message["nonce"],
                            "sample_id": "sample-1",
                            "server_processing_us": 0,
                        }
                    )
                )
            elif kind == "ptt.begin":
                await socket.send(json.dumps({"type": "generation", "generation": 1}))
            elif kind == "ptt.end":
                await socket.send(json.dumps({"type": "audio.end"}))
            elif kind == "hangup":
                await socket.send(json.dumps({"type": "call.ended"}))
                await asyncio.sleep(0.05)
                await socket.send(
                    json.dumps(
                        {
                            "type": "error",
                            "code": "late_failure",
                            "message": "late server failure",
                        }
                    )
                )
                return

    async def no_path(*args, **kwargs):
        return {"path": "unknown", "source": "test", "evidence": "test"}

    async def scenario() -> None:
        import websockets

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            args = argparse.Namespace(
                url=f"ws://127.0.0.1:{port}",
                wav="unused.wav",
                token_env="SERENA_CALL_TOKEN",
                expect_path=None,
                tailscale_pings=1,
                tailscale_timeout=1.0,
                rtt_samples=1,
                timeout=1.0,
                realtime=False,
            )
            with pytest.raises(RuntimeError, match="late_failure"):
                await e2e_probe._run_once(args)

    monkeypatch.setenv("SERENA_CALL_TOKEN", "test")
    monkeypatch.setattr(e2e_probe, "_pcm_frames", lambda _path: [b"\0" * 6_400])
    monkeypatch.setattr(e2e_probe, "_probe_tailscale_path", no_path)
    asyncio.run(scenario())
