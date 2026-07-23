from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

import pytest

from voice.call.protocol import MIC_FRAME_BYTES, AudioFrame, AudioHeader, AudioKind
from voice.call.wakeword import WakeGate, WakeWordConfigurationError, sha256_file
from voice.call.wakeword_calibration import build_acceptance_manifest
from voice.desk.client import (
    DeskClient,
    DeskConfig,
    DeskConnectionLost,
    DeskMetrics,
    derive_greeting_url,
    load_manifest_wake_config,
    main,
)
from voice.desk.greetings import GreetingAudio
from voice.desk.io import PlaybackStart
from voice.desk.transport import TransportEvent


class _Scorer:
    score_label = "hey_serena"

    def __init__(self, scores: list[float]) -> None:
        self.scores = iter(scores)
        self.resets = 0

    def score_frame(self, _frame) -> float:
        return next(self.scores)

    def reset(self) -> None:
        self.resets += 1


class _Microphone:
    def __init__(self) -> None:
        self.frames: queue.Queue[bytes] = queue.Queue()
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def drain(self) -> None:
        while True:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                return

    def close(self) -> None:
        self.closed = True


class _Overlay:
    def __init__(self) -> None:
        self.states: list[str] = []
        self.events: list[dict] = []

    def open(self) -> None:
        pass

    def set_state(self, state: str) -> None:
        self.states.append(state)

    def send_event(self, message: dict) -> None:
        self.events.append(message)

    def close(self) -> None:
        pass


class _Playback:
    def __init__(self) -> None:
        self.played: list[str] = []
        self.rates: list[int] = []
        self.writes: list[bytes] = []
        self.finished = 0

    def play(self, greeting: GreetingAudio) -> PlaybackStart:
        self.played.append(greeting.greeting_id)
        return PlaybackStart(time.monotonic_ns(), False)

    def start(self, sample_rate: int) -> None:
        self.rates.append(sample_rate)

    def write(self, pcm: bytes) -> PlaybackStart | None:
        self.writes.append(pcm)
        if len(self.writes) == 1:
            return PlaybackStart(time.monotonic_ns(), False)
        return None

    def finish(self) -> None:
        self.finished += 1

    def close(self) -> None:
        self.finish()


class _Fetcher:
    def __init__(self) -> None:
        self.calls = 0

    def fetch(self) -> tuple[GreetingAudio, str]:
        self.calls += 1
        return GreetingAudio("greeting", 24_000, b"\x01\x00", "hi", time.time()), "server-cache"


class _Metrics:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    def record(self, event: str, **fields) -> None:
        self.rows.append((event, fields))


class _LocalFallback:
    def __init__(self) -> None:
        self.runs = 0

    def run(self, _stop: threading.Event) -> int:
        self.runs += 1
        return 1


class _SoundDevice:
    class default:
        device = (3, 4)

    @staticmethod
    def query_devices(device, kind):
        assert device == 3
        assert kind == "input"
        return {"name": "Desk Mic", "hostapi": 1, "max_input_channels": 2}

    @staticmethod
    def check_input_settings(**settings) -> None:
        assert settings == {
            "device": 3,
            "channels": 1,
            "dtype": "int16",
            "samplerate": 16_000,
        }


class _Transport:
    def __init__(self, microphone: _Microphone | None = None, *, ready: bool = True) -> None:
        self.events: queue.Queue[TransportEvent] = queue.Queue()
        self.call_id = "desk-test"
        self.generation = 0
        self.failure = "" if ready else "offline"
        self._ready = ready
        self.microphone = microphone
        self.connects = 0
        self.closed = 0
        self.sent: list[bytes] = []
        self.playback_acks: list[tuple[int, int]] = []
        self.gaps: list[tuple[int, int]] = []
        self.cancels = 0

    def connect(self) -> None:
        self.connects += 1

    def wait_ready(self, _timeout: float) -> bool:
        return self._ready

    def begin_listening(self) -> int:
        self.generation += 1
        if self.microphone is not None and self.generation == 1:
            for _ in range(5):
                self.microphone.frames.put(b"\x01\x00" * 1_280)
        return self.generation

    def send_mic_frame(self, pcm: bytes) -> None:
        self.sent.append(pcm)
        if len(self.sent) == 2:
            self.events.put(
                TransportEvent(
                    "control", {"type": "endpoint.detected", "generation": 1}
                )
            )
            self.events.put(
                TransportEvent(
                    "control",
                    {"type": "audio.start", "generation": 1, "sample_rate": 24_000},
                )
            )
            self.events.put(
                TransportEvent(
                    "audio",
                    AudioFrame(
                        AudioHeader(AudioKind.TTS_PCM16, 0, 0, 24_000, 1),
                        b"\x02\x00" * 480,
                    ),
                )
            )
            self.events.put(
                TransportEvent(
                    "control",
                    {"type": "audio.end", "generation": 1, "last_sequence": 0},
                )
            )

    def playback_started(self, sequence: int, timestamp_ns: int) -> None:
        self.playback_acks.append((sequence, timestamp_ns))

    def report_output_gap(self, expected: int, received: int) -> None:
        self.gaps.append((expected, received))

    def cancel(self) -> None:
        self.cancels += 1

    def close(self) -> None:
        self.closed += 1

    def hangup(self) -> None:
        self.closed += 1


def _client(
    scores: list[float],
    microphone: _Microphone,
    transport_factory,
) -> tuple[DeskClient, _Playback, _Overlay, _Fetcher, _Metrics]:
    playback = _Playback()
    overlay = _Overlay()
    fetcher = _Fetcher()
    metrics = _Metrics()
    client = DeskClient(
        _Scorer(scores),
        WakeGate(0.5, patience_frames=2, cooldown_seconds=0),
        microphone,
        playback,
        overlay,
        fetcher,
        transport_factory,
        config=DeskConfig(
            listen_idle_seconds=1,
            listen_max_seconds=2,
            response_timeout=0.2,
            reconnect_attempts=0,
        ),
        metrics=metrics,
    )
    return client, playback, overlay, fetcher, metrics


def test_background_frames_do_not_create_network_or_fetch_greeting() -> None:
    microphone = _Microphone()
    factory_calls = 0

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        return _Transport()

    client, playback, _overlay, fetcher, _metrics = _client(
        [0.1, 0.2, 0.49], microphone, factory
    )
    for index in range(3):
        woke, _score = client.process_idle_frame(
            b"\x00\x00" * 1_280, monotonic_ns=index * 80_000_000
        )
        assert woke is False

    assert factory_calls == 0
    assert fetcher.calls == 0
    assert playback.played == []


def test_network_and_greeting_begin_only_after_threshold_gate() -> None:
    microphone = _Microphone()
    transport = _Transport(ready=False)
    client, playback, overlay, fetcher, metrics = _client(
        [0.7, 0.8], microphone, lambda: transport
    )
    first, _ = client.process_idle_frame(b"\x00\x00" * 1_280, monotonic_ns=0)
    second, _ = client.process_idle_frame(
        b"\x00\x00" * 1_280, monotonic_ns=80_000_000
    )
    assert first is False and second is True

    client._activate(time.monotonic_ns(), threading.Event())

    assert transport.connects == 1
    assert fetcher.calls == 1
    assert playback.played == ["greeting"]
    assert overlay.states[:2] == ["listening", "speaking"]
    assert any(event == "greeting.first_write" for event, _ in metrics.rows)


def test_awake_handoff_starts_one_session_without_scoring_another_wake() -> None:
    microphone = _Microphone()
    client, _playback, _overlay, _fetcher, metrics = _client(
        [], microphone, lambda: _Transport(ready=False)
    )
    activations: list[int] = []
    client._activate = lambda wake_ns, _stop: activations.append(wake_ns)

    client.run(start_awake=True)

    assert len(activations) == 1
    assert microphone.started is True
    assert microphone.closed is True
    assert any(event == "wake.handoff_received" for event, _ in metrics.rows)


def test_conversation_sends_exact_post_wake_frames_and_tracks_states() -> None:
    microphone = _Microphone()
    transport = _Transport(microphone)
    client, playback, overlay, _fetcher, _metrics = _client(
        [], microphone, lambda: transport
    )

    client._run_conversation(
        transport, threading.Event(), max_completed_turns=1
    )

    assert len(transport.sent) == 2
    assert all(len(frame) == MIC_FRAME_BYTES for frame in transport.sent)
    assert playback.rates == [24_000]
    assert playback.writes == [b"\x02\x00" * 480]
    assert transport.playback_acks[0][0] == 0
    assert overlay.states == ["listening", "thinking", "speaking"]


def test_output_gap_is_reported_and_playback_aborts() -> None:
    microphone = _Microphone()
    transport = _Transport()
    client, _playback, _overlay, _fetcher, _metrics = _client(
        [], microphone, lambda: transport
    )
    client.playback.start(24_000)
    event = TransportEvent(
        "audio",
        AudioFrame(
            AudioHeader(AudioKind.TTS_PCM16, 0, 2, 24_000, 1),
            b"\x01\x00" * 10,
        ),
    )

    with pytest.raises(RuntimeError, match="sequence gap"):
        client._handle_event(
            transport,
            event,
            phase="speaking",
            expected_output_sequence=0,
            output_rate=24_000,
            first_output_written=False,
        )
    assert transport.gaps == [(0, 2)]


def test_late_brain_delta_does_not_downgrade_active_playback_to_thinking() -> None:
    microphone = _Microphone()
    transport = _Transport()
    client, _playback, _overlay, _fetcher, _metrics = _client(
        [], microphone, lambda: transport
    )

    outcome = client._handle_event(
        transport,
        TransportEvent(
            "control",
            {"type": "brain.delta", "generation": 0, "delta": "more words"},
        ),
        phase="speaking",
        expected_output_sequence=3,
        output_rate=24_000,
        first_output_written=True,
    )

    assert outcome["phase"] == "speaking"
    assert outcome["phase_changed"] is False
    assert outcome["output_rate"] == 24_000


def test_server_code_panel_control_reaches_overlay() -> None:
    microphone = _Microphone()
    transport = _Transport()
    client, _playback, overlay, _fetcher, _metrics = _client(
        [], microphone, lambda: transport
    )

    client._handle_event(
        transport,
        TransportEvent(
            "control",
            {"type": "code.panel", "generation": 0, "action": "open"},
        ),
        phase="thinking",
        expected_output_sequence=0,
        output_rate=None,
        first_output_written=False,
    )

    assert overlay.events == [
        {"type": "code_start", "project": "coding", "status": "ready"}
    ]


def test_missing_model_fails_before_microphone_construction(
    tmp_path: Path, monkeypatch
) -> None:
    token = tmp_path / "token"
    token.write_text("secret", encoding="utf-8")

    def microphone_should_not_construct(*_args, **_kwargs):
        raise AssertionError("microphone opened before model validation")

    monkeypatch.setattr(
        "voice.desk.client.SoundDeviceMicrophone", microphone_should_not_construct
    )
    with pytest.raises(WakeWordConfigurationError, match="refusing to substitute"):
        main(["--token-file", str(token), "--model", str(tmp_path / "missing.onnx")])


def test_greeting_url_tracks_websocket_security_and_host() -> None:
    assert derive_greeting_url("ws://brain:8766/ws/desk") == (
        "http://brain:8766/desk/greeting"
    )
    assert derive_greeting_url("wss://brain.example/ws/desk?x=1") == (
        "https://brain.example/desk/greeting"
    )


def test_metrics_close_never_blocks_on_a_full_stalled_queue(
    tmp_path: Path, monkeypatch
) -> None:
    writer_entered = threading.Event()
    release_writer = threading.Event()

    def stalled_append(_path, _payload) -> None:
        writer_entered.set()
        release_writer.wait(2)

    monkeypatch.setattr("voice.desk.client.append_json_line", stalled_append)
    metrics = DeskMetrics(tmp_path / "metrics.jsonl")
    metrics.record("first")
    assert writer_entered.wait(1)
    for index in range(600):
        metrics.record("queued", index=index)

    started = time.monotonic()
    metrics.close()
    elapsed = time.monotonic() - started
    release_writer.set()

    assert elapsed < 1.5
    assert metrics.dropped > 0


def test_mid_call_disconnect_reconnects_with_a_cue() -> None:
    microphone = _Microphone()
    first = _Transport(microphone)
    second = _Transport(microphone)
    transports = iter([second])
    client, playback, _overlay, _fetcher, metrics = _client(
        [], microphone, lambda: next(transports)
    )
    client.config = DeskConfig(
        listen_idle_seconds=1,
        listen_max_seconds=2,
        reconnect_attempts=1,
        reconnect_backoff_seconds=0.001,
    )
    conversations: list[_Transport] = []

    def conversation(transport, _stop) -> None:
        conversations.append(transport)
        if transport is first:
            raise DeskConnectionLost("network roam")

    client._run_conversation = conversation
    connect_thread = client._start_connect(first)

    assert client._run_remote_with_reconnect(
        first, connect_thread, threading.Event()
    ) is True
    assert conversations == [first, second]
    assert second.connects == 1
    assert "tone-fallback" in playback.played
    assert any(event == "desk.reconnected" for event, _ in metrics.rows)


def test_visible_session_dismissal_hangs_up_without_reconnecting() -> None:
    microphone = _Microphone()
    transport = _Transport(microphone)
    replacement_calls = 0

    def replacement() -> _Transport:
        nonlocal replacement_calls
        replacement_calls += 1
        return _Transport(microphone)

    session_stop = threading.Event()
    client, _playback, _overlay, _fetcher, _metrics = _client(
        [], microphone, replacement
    )
    client.session_stop = session_stop

    def dismiss_during_conversation(_transport, _service_stop) -> None:
        session_stop.set()

    client._run_conversation = dismiss_during_conversation
    connect_thread = client._start_connect(transport)

    assert client._run_remote_with_reconnect(
        transport, connect_thread, threading.Event()
    ) is True
    assert transport.closed == 1
    assert replacement_calls == 0


def test_outbound_send_failure_uses_the_same_reconnect_path() -> None:
    microphone = _Microphone()
    first = _Transport(microphone)
    second = _Transport(microphone)
    client, _playback, _overlay, _fetcher, metrics = _client(
        [], microphone, lambda: second
    )
    client.config = DeskConfig(
        listen_idle_seconds=1,
        listen_max_seconds=2,
        reconnect_attempts=1,
        reconnect_backoff_seconds=0.001,
    )
    conversations: list[_Transport] = []

    def conversation(transport, _stop) -> None:
        conversations.append(transport)
        if transport is first:
            transport.failure = "send failed: broken pipe"
            raise OSError("broken pipe")

    client._run_conversation = conversation
    connect_thread = client._start_connect(first)

    assert client._run_remote_with_reconnect(
        first, connect_thread, threading.Event()
    ) is True
    assert conversations == [first, second]
    assert any(
        event == "desk.disconnected" and fields["error"] == first.failure
        for event, fields in metrics.rows
    )
    assert any(event == "desk.reconnected" for event, _ in metrics.rows)


def test_exhausted_remote_retries_enter_local_cold_fallback() -> None:
    microphone = _Microphone()
    transports: list[_Transport] = []

    def factory() -> _Transport:
        transport = _Transport(ready=False)
        transports.append(transport)
        return transport

    fallback = _LocalFallback()
    client, playback, _overlay, _fetcher, _metrics = _client(
        [], microphone, factory
    )
    client.config = DeskConfig(
        listen_idle_seconds=1,
        listen_max_seconds=2,
        reconnect_attempts=1,
        reconnect_backoff_seconds=0.001,
    )
    client.local_fallback = fallback

    client._activate(time.monotonic_ns(), threading.Event())

    assert len(transports) == 2
    assert all(transport.connects == 1 for transport in transports)
    assert playback.played == ["greeting", "tone-fallback"]
    assert fallback.runs == 1


def _acceptance_manifest(tmp_path: Path) -> tuple[Path, Path]:
    model = tmp_path / "hey_serena.onnx"
    model.write_bytes(b"frozen model")
    manifest = build_acceptance_manifest(
        model_path=model,
        model_sha256=sha256_file(model),
        score_label="hey_serena",
        threshold=0.62,
        patience_frames=2,
        cooldown_seconds=3.5,
        device="3:Desk Mic:1:2",
        device_selector="3",
        package_version="0.6.0",
    )
    path = tmp_path / "wakeword-acceptance.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, model


def test_frozen_manifest_controls_the_exact_production_wake_config(
    tmp_path: Path,
) -> None:
    path, model = _acceptance_manifest(tmp_path)

    frozen = load_manifest_wake_config(
        path,
        sounddevice_module=_SoundDevice,
        package_version="0.6.0",
    )

    assert frozen.spec.model_path == model.resolve()
    assert frozen.spec.score_label == "hey_serena"
    assert frozen.gate.threshold == 0.62
    assert frozen.gate.patience_frames == 2
    assert frozen.gate.cooldown_seconds == 3.5
    assert frozen.input_device == 3


def test_frozen_manifest_rejects_changed_model_version_or_microphone(
    tmp_path: Path,
) -> None:
    path, model = _acceptance_manifest(tmp_path)
    model.write_bytes(b"changed")
    with pytest.raises(WakeWordConfigurationError, match="model hash"):
        load_manifest_wake_config(
            path,
            sounddevice_module=_SoundDevice,
            package_version="0.6.0",
        )

    path, _model = _acceptance_manifest(tmp_path)
    with pytest.raises(WakeWordConfigurationError, match="version"):
        load_manifest_wake_config(
            path,
            sounddevice_module=_SoundDevice,
            package_version="0.7.0",
        )

    class ChangedMicrophone(_SoundDevice):
        @staticmethod
        def query_devices(device, kind):
            info = _SoundDevice.query_devices(device, kind)
            return {**info, "name": "Other Mic"}

    with pytest.raises(WakeWordConfigurationError, match="microphone identity"):
        load_manifest_wake_config(
            path,
            sounddevice_module=ChangedMicrophone,
            package_version="0.6.0",
        )


class _BargeTransport(_Transport):
    """Scripts a turn that reaches 'speaking' and stays there (no audio.end),
    then loads loud mic frames so the barge-in path is the only exit."""

    LOUD_FRAME = (8_000).to_bytes(2, "little", signed=True) * 1_280

    def send_mic_frame(self, pcm: bytes) -> None:
        self.sent.append(pcm)
        if len(self.sent) == 2:
            self.events.put(
                TransportEvent(
                    "control", {"type": "endpoint.detected", "generation": 1}
                )
            )
            self.events.put(
                TransportEvent(
                    "control",
                    {"type": "audio.start", "generation": 1, "sample_rate": 24_000},
                )
            )
            for sequence in range(12):
                self.events.put(
                    TransportEvent(
                        "audio",
                        AudioFrame(
                            AudioHeader(
                                AudioKind.TTS_PCM16,
                                0,
                                sequence,
                                24_000,
                                sequence + 1,
                            ),
                            b"\x02\x00" * 480,
                        ),
                    )
                )
            # No audio.end: Serena keeps "speaking" until interrupted.
            assert self.microphone is not None
            for _ in range(6):
                self.microphone.frames.put(self.LOUD_FRAME)


def test_sustained_speech_while_speaking_barges_in(monkeypatch) -> None:
    monkeypatch.delenv("SERENA_DESK_BARGE_IN", raising=False)
    microphone = _Microphone()
    transport = _BargeTransport(microphone)
    client, playback, overlay, _fetcher, metrics = _client(
        [], microphone, lambda: transport
    )

    client._run_conversation(transport, threading.Event(), max_completed_turns=1)

    # Her voice was cut and the turn was cancelled server-side.
    assert playback.finished >= 1
    assert transport.cancels >= 1
    # A fresh listening generation opened after the barge.
    assert transport.generation >= 2
    # The barged speech itself was forwarded, not lost: beyond the 2 scripted
    # listening frames, the packed trigger frames went out on the wire.
    assert len(transport.sent) >= 4
    assert all(len(frame) == MIC_FRAME_BYTES for frame in transport.sent)
    # The loop services one microphone frame between queued playback frames,
    # so interruption is not starved behind buffered TTS.
    assert len(playback.writes) < 12
    # The overlay came back to listening after speaking.
    assert "speaking" in overlay.states
    assert overlay.states[-1] == "listening"
    assert any(event == "desk.barge_in" for event, _ in metrics.rows)


def test_soft_noise_while_speaking_does_not_barge(monkeypatch) -> None:
    monkeypatch.delenv("SERENA_DESK_BARGE_IN", raising=False)

    class _SoftNoiseTransport(_BargeTransport):
        LOUD_FRAME = (20).to_bytes(2, "little", signed=True) * 1_280

    microphone = _Microphone()
    transport = _SoftNoiseTransport(microphone)
    client, playback, _overlay, _fetcher, metrics = _client(
        [], microphone, lambda: transport
    )

    client._run_conversation(transport, threading.Event(), max_completed_turns=1)

    assert not any(event == "desk.barge_in" for event, _ in metrics.rows)
    # Only the 2 scripted listening frames went out; quiet noise stayed local.
    assert len(transport.sent) == 2
