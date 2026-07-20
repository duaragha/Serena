from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from voice.desk.greetings import GreetingAudio
from voice.desk.io import GreetingFetcher, SoundDevicePlayback, pcm_visual_level


class _Overlay:
    def __init__(self) -> None:
        self.amplitudes: list[float] = []

    def set_amplitude(self, value: float) -> None:
        self.amplitudes.append(value)


class _RawOutputStream:
    instances: list[_RawOutputStream] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.writes: list[bytes] = []
        self.started = False
        self.stopped = False
        self.closed = False
        self.instances.append(self)

    def start(self) -> None:
        self.started = True

    def write(self, pcm: bytes) -> bool:
        self.writes.append(bytes(pcm))
        return False

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


def test_playback_publishes_level_from_every_real_pcm_chunk(monkeypatch) -> None:
    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(RawOutputStream=_RawOutputStream),
    )
    overlay = _Overlay()
    playback = SoundDevicePlayback(overlay)
    quiet = b"\x00\x00" * 480
    loud = (16_000).to_bytes(2, "little", signed=True) * 480

    playback.start(24_000)
    first = playback.write(quiet)
    assert first is not None
    assert playback.write(loud) is None
    playback.finish()

    assert overlay.amplitudes[0] == 0.0
    assert overlay.amplitudes[1] == 0.0
    assert overlay.amplitudes[2] == pcm_visual_level(loud)
    assert overlay.amplitudes[-1] == 0.0
    stream = _RawOutputStream.instances[-1]
    assert stream.writes == [quiet, loud]
    assert stream.started and stream.stopped and stream.closed


def test_greeting_fetcher_uses_persisted_assistant_audio_when_server_fails(
    tmp_path: Path,
) -> None:
    fallback_path = tmp_path / "last.json"
    greeting = GreetingAudio("last-good", 24_000, b"\x01\x00", "hi", 1.0)
    from core.brain_lifetime import write_json_atomic

    write_json_atomic(fallback_path, {"version": 1, "greeting": greeting.to_json()})
    fetcher = GreetingFetcher(
        "http://127.0.0.1:1/desk/greeting",
        "secret",
        fallback_path=fallback_path,
        timeout=0.2,
    )

    restored, source = fetcher.fetch()

    assert source == "local-fallback"
    assert restored.greeting_id == "last-good"
    assert restored.pcm == greeting.pcm


def test_greeting_fetcher_has_local_tone_as_last_resort(tmp_path: Path) -> None:
    fetcher = GreetingFetcher(
        "http://127.0.0.1:1/desk/greeting",
        "secret",
        fallback_path=tmp_path / "missing.json",
        timeout=0.2,
    )

    greeting, source = fetcher.fetch()

    assert source == "tone-fallback"
    assert greeting.greeting_id == "tone-fallback"
    assert greeting.pcm


def test_failed_pcm_write_never_publishes_the_chunk_level(monkeypatch) -> None:
    class _FailedOutput(_RawOutputStream):
        def write(self, pcm: bytes) -> bool:
            self.writes.append(bytes(pcm))
            raise OSError("device lost")

    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(RawOutputStream=_FailedOutput),
    )
    overlay = _Overlay()
    playback = SoundDevicePlayback(overlay)
    playback.start(24_000)
    before = list(overlay.amplitudes)

    try:
        playback.write(b"\x00\x40" * 20)
    except OSError:
        pass
    else:
        raise AssertionError("failed audio device write did not propagate")

    assert overlay.amplitudes == before
    playback.finish()
    assert overlay.amplitudes[-1] == 0.0


def test_overlay_publisher_reconnects_after_send_failure(monkeypatch, tmp_path: Path) -> None:
    from voice.desk.io import OverlayPublisher

    connected = threading.Event()
    sockets = []

    class _PublisherSocket:
        def __init__(self, fail: bool) -> None:
            self.fail = fail
            self.sent: list[str] = []
            self.closed = False

        def settimeout(self, _value) -> None:
            pass

        def send(self, payload: str) -> None:
            if self.fail:
                self.fail = False
                raise OSError("bridge restarted")
            self.sent.append(payload)

        def close(self) -> None:
            self.closed = True

    def create_connection(*_args, **_kwargs):
        socket = _PublisherSocket(fail=not sockets)
        sockets.append(socket)
        connected.set()
        return socket

    monkeypatch.setitem(
        sys.modules,
        "websocket",
        SimpleNamespace(create_connection=create_connection),
    )
    publisher = OverlayPublisher(state_path=tmp_path / "state")
    publisher.open()
    assert connected.wait(1)
    publisher.set_amplitude(0.4)
    deadline = time.monotonic() + 2
    while len(sockets) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(sockets) >= 2

    publisher.set_amplitude(0.7)
    publisher.close()

    assert sockets[0].closed is True
    assert any('"value":0.7' in payload for payload in sockets[-1].sent)
