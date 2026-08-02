from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

from voice.desk.greetings import GreetingAudio
from voice.desk.io import (
    FALLBACK_SCHEMA_VERSION,
    WAKE_FRAME_BYTES,
    GreetingFetcher,
    PipeWireMicrophone,
    PlaybackStart,
    SoundDevicePlayback,
    consume_wake_greeting_handoff,
    pcm_visual_level,
    record_wake_greeting_handoff,
)


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


class _ChunkedCaptureOutput:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = deque(chunks)
        self.closed = False

    def read(self, size: int) -> bytes:
        if not self.chunks:
            return b""
        chunk = self.chunks.popleft()
        if len(chunk) <= size:
            return chunk
        self.chunks.appendleft(chunk[size:])
        return chunk[:size]

    def close(self) -> None:
        self.closed = True


class _ExitedCaptureProcess:
    def __init__(self, chunks: list[bytes], returncode: int = 7) -> None:
        self.stdout = _ChunkedCaptureOutput(chunks)
        self.returncode = returncode
        self.command: list[str] = []
        self.kwargs = {}

    def poll(self) -> int:
        return self.returncode

    def kill(self) -> None:
        raise AssertionError("an exited capture must not be killed")


class _BlockingCaptureOutput:
    def __init__(self) -> None:
        self.released = threading.Event()
        self.closed = False

    def read(self, _size: int) -> bytes:
        self.released.wait(1)
        return b""

    def close(self) -> None:
        self.closed = True
        self.released.set()


class _HungCaptureProcess:
    def __init__(self) -> None:
        self.stdout = _BlockingCaptureOutput()
        self.terminated = False
        self.killed = False

    def poll(self):
        return -9 if self.killed else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float):
        if not self.killed:
            raise subprocess.TimeoutExpired("pw-cat", timeout)
        return -9

    def kill(self) -> None:
        self.killed = True
        self.stdout.released.set()


def test_pipewire_capture_assembles_frames_and_bounds_its_queue() -> None:
    first = b"\x01\x00" * (WAKE_FRAME_BYTES // 2)
    second = b"\x02\x00" * (WAKE_FRAME_BYTES // 2)
    third = b"\x03\x00" * (WAKE_FRAME_BYTES // 2)
    process = _ExitedCaptureProcess(
        [
            first[:311],
            first[311:] + second[:97],
            second[97:] + third,
        ]
    )

    def factory(command, **kwargs):
        process.command = list(command)
        process.kwargs = dict(kwargs)
        return process

    microphone = PipeWireMicrophone(
        target="dmic_dc_filtered",
        max_frames=2,
        process_factory=factory,
    )
    microphone.start()
    deadline = time.monotonic() + 1
    while microphone.failure_reason is None and time.monotonic() < deadline:
        time.sleep(0.005)

    assert microphone.frames.get_nowait() == second
    assert microphone.frames.get_nowait() == third
    assert microphone.dropped_frames == 1
    assert microphone.failure_reason == "PipeWire capture exited with status 7"
    assert process.command[:2] == ["/usr/bin/pw-cat", "--record"]
    assert process.command[process.command.index("--target") + 1] == "dmic_dc_filtered"
    assert "--raw" not in process.command
    assert "--sample-count" not in process.command
    assert process.command[-1] == "-"
    assert process.kwargs["stderr"] is subprocess.DEVNULL
    microphone.close()
    assert process.stdout.closed is True


def test_pipewire_capture_close_escalates_to_kill_without_hanging() -> None:
    process = _HungCaptureProcess()
    microphone = PipeWireMicrophone(
        process_factory=lambda *_args, **_kwargs: process,
        terminate_timeout=0.01,
        kill_timeout=0.05,
    )
    microphone.start()

    started = time.monotonic()
    microphone.close()
    elapsed = time.monotonic() - started

    assert process.terminated is True
    assert process.killed is True
    assert process.stdout.closed is True
    assert elapsed < 0.5


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


def test_wake_greeting_handoff_is_runtime_only_and_single_use(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "handoff.json"
    greeting = GreetingAudio(
        "hot-greeting",
        24_000,
        b"\x01\x00" * 2_400,
        "private",
        time.time(),
    )
    first_write = PlaybackStart(time.monotonic_ns(), False)

    recorded = record_wake_greeting_handoff(
        greeting,
        first_write,
        "server-cache",
        path=marker,
    )
    consumed = consume_wake_greeting_handoff(path=marker)

    assert consumed == recorded
    assert consumed.playback_end_monotonic_ns > first_write.monotonic_ns
    assert consume_wake_greeting_handoff(path=marker) is None


def test_greeting_fetcher_uses_persisted_assistant_audio_when_server_fails(
    tmp_path: Path,
) -> None:
    fallback_path = tmp_path / "last.json"
    greeting = GreetingAudio("last-good", 24_000, b"\x01\x00", "hi", 1.0)
    from core.brain_lifetime import write_json_atomic

    write_json_atomic(
        fallback_path,
        {"version": FALLBACK_SCHEMA_VERSION, "greeting": greeting.to_json()},
    )
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


def test_greeting_fetcher_rejects_retired_voice_cache(tmp_path: Path) -> None:
    fallback_path = tmp_path / "last.json"
    greeting = GreetingAudio("retired", 24_000, b"\x01\x00", "hi", 1.0)
    from core.brain_lifetime import write_json_atomic

    write_json_atomic(fallback_path, {"version": 1, "greeting": greeting.to_json()})
    fetcher = GreetingFetcher(
        "http://127.0.0.1:1/desk/greeting",
        "secret",
        fallback_path=fallback_path,
        timeout=0.2,
    )

    restored, source = fetcher.fetch()

    assert source == "tone-fallback"
    assert restored.greeting_id == "tone-fallback"


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


def test_a_restarted_client_does_not_stomp_a_turn_in_progress(tmp_path: Path) -> None:
    """The wake rearm knocked the dot field out from under a typed reply.

    On 2026-08-01 the supervisor put the microphone back on the wake word at
    21:10:38 by starting a fresh client, four seconds after her typed reply
    appeared and three seconds before her voice reached the speakers. The new
    process announced itself with "idle" and the overlay went dark while she
    was still mid-answer.
    """
    from voice.desk.io import OverlayPublisher

    state = tmp_path / "state"
    state.write_text("speaking\n", encoding="utf-8")
    publisher = OverlayPublisher(state_path=state)

    assert publisher.adopt_state() == "speaking"
    assert state.read_text(encoding="utf-8").strip() == "speaking"
    # It adopted the state rather than merely skipping one write, so its own
    # next transition is still published.
    publisher.set_state("listening")
    assert state.read_text(encoding="utf-8").strip() == "listening"


def test_a_state_left_behind_by_a_dead_process_is_cleared(tmp_path: Path) -> None:
    """Inheriting forever would leave the dot stuck on a turn nobody is having."""
    from voice.desk.io import OverlayPublisher

    state = tmp_path / "state"
    state.write_text("speaking\n", encoding="utf-8")
    os.utime(state, (time.time() - 600, time.time() - 600))
    publisher = OverlayPublisher(state_path=state)

    assert publisher.adopt_state() == "idle"
    assert state.read_text(encoding="utf-8").strip() == "idle"


def test_a_departing_client_does_not_clear_someone_elses_turn(tmp_path: Path) -> None:
    """A spoken conversation ending is not the machine going quiet.

    The awake client writes idle in its shutdown path. If Raghav typed to her
    while that conversation was winding down, that write landed on top of the
    typed turn's "speaking" and blanked the dot field mid-sentence.
    """
    from voice.desk.io import OverlayPublisher

    state = tmp_path / "state"
    publisher = OverlayPublisher(state_path=state)
    publisher.set_state("listening")
    # The typed turn takes the overlay over while this client is shutting down.
    state.write_text("speaking\n", encoding="utf-8")

    publisher.release_state()

    assert state.read_text(encoding="utf-8").strip() == "speaking"


def test_a_departing_client_still_clears_its_own_turn(tmp_path: Path) -> None:
    """Releasing must not mean never going idle, or the dot never settles."""
    from voice.desk.io import OverlayPublisher

    state = tmp_path / "state"
    publisher = OverlayPublisher(state_path=state)
    publisher.set_state("listening")

    publisher.release_state()

    assert state.read_text(encoding="utf-8").strip() == "idle"
