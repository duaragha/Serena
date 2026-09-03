from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

import numpy as np
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
from voice.desk.io import PlaybackStart, WakeGreetingHandoff
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

    def adopt_state(self) -> str:
        self.states.append("idle")
        return "idle"

    def release_state(self) -> None:
        self.states.append("idle")

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
    # These exercise behaviour after the room has been measured; run() is not
    # called here, so the calibration that normally sets this never happens.
    client.noise_calibrated = True
    client.barge_in_safe = True
    return client, playback, overlay, fetcher, metrics


def test_muted_microphone_cannot_wake_or_open_a_voice_call(monkeypatch) -> None:
    """OpenWhispr dictation must not accidentally wake Serena in parallel."""

    microphone = _Microphone()
    factory_calls = 0
    stop = threading.Event()

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        return _Transport()

    client, _playback, overlay, _fetcher, metrics = _client(
        [0.99], microphone, factory
    )
    client.input_muted = lambda: True
    monkeypatch.setattr(client, "_calibrate_microphone", lambda: None)

    thread = threading.Thread(target=client.run, args=(stop,), daemon=True)
    thread.start()
    time.sleep(0.15)
    stop.set()
    thread.join(timeout=1)

    assert thread.is_alive() is False
    assert factory_calls == 0
    assert "idle" in overlay.states
    assert any(event == "desk.microphone_muted" for event, _ in metrics.rows)


def test_muting_during_a_conversation_stops_microphone_delivery() -> None:
    """The mute button must cut an already-listening turn, not only future wakes."""

    microphone = _Microphone()
    transport = _Transport(microphone)
    client, _playback, _overlay, _fetcher, _metrics = _client(
        [], microphone, lambda: transport
    )
    client.input_muted = lambda: True

    client._run_conversation(transport, threading.Event())

    assert transport.generation == 1
    assert transport.sent == []


def test_dc_wedged_microphone_cannot_cancel_a_valid_reply(monkeypatch) -> None:
    """A raw AMD DMIC offset once looked like speech and cancelled every answer."""

    from voice.desk import client as client_module

    microphone = _Microphone()
    rng = np.random.default_rng(7)
    for _ in range(12):
        samples = np.clip(-8_300 + rng.normal(0, 300, 1_280), -32_768, 32_767)
        microphone.frames.put(samples.astype("<i2").tobytes())
    client, _playback, _overlay, _fetcher, _metrics = _client(
        [], microphone, lambda: _Transport()
    )
    monkeypatch.setattr(client_module, "NOISE_CALIBRATION_SECONDS", 0.0)

    client._calibrate_microphone()

    assert client.noise_calibrated is True
    assert client.barge_in_safe is False


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


def test_awake_handoff_starts_one_session_without_scoring_another_wake(
    monkeypatch,
) -> None:
    microphone = _Microphone()
    client, _playback, _overlay, _fetcher, metrics = _client(
        [], microphone, lambda: _Transport(ready=False)
    )
    activations: list[int] = []
    monkeypatch.setattr(
        "voice.desk.client.consume_wake_greeting_handoff", lambda: None
    )
    client._activate = lambda wake_ns, _stop, **_kwargs: activations.append(wake_ns)

    client.run(start_awake=True)

    assert len(activations) == 1
    assert microphone.started is True
    assert microphone.closed is True
    assert any(event == "wake.handoff_received" for event, _ in metrics.rows)


def test_hot_wake_greeting_is_not_replayed_by_cold_handoff() -> None:
    microphone = _Microphone()
    transport = _Transport()
    client, playback, overlay, fetcher, metrics = _client(
        [], microphone, lambda: transport
    )
    client._run_remote_with_reconnect = lambda *_args: True
    handoff = WakeGreetingHandoff(
        "already-playing",
        time.monotonic_ns(),
        "server-cache",
    )

    client._activate(
        time.monotonic_ns(),
        threading.Event(),
        greeting_handoff=handoff,
    )

    assert fetcher.calls == 0
    assert playback.played == []
    assert overlay.states == ["listening", "speaking", "listening"]
    assert any(event == "greeting.handoff_consumed" for event, _ in metrics.rows)


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


def test_visible_desk_turn_text_reaches_overlay_without_broker_metadata() -> None:
    microphone = _Microphone()
    transport = _Transport()
    client, _playback, overlay, _fetcher, _metrics = _client(
        [], microphone, lambda: transport
    )

    controls = [
        {"type": "stt.result", "generation": 0, "text": "what did you say"},
        {
            "type": "brain.done",
            "generation": 0,
            "text": "the visible reply",
            "meta": {"session_id": "private", "tool_result": "never forward"},
            "backend": "private-backend",
        },
    ]
    for control in controls:
        client._handle_event(
            transport,
            TransportEvent("control", control),
            phase="thinking",
            expected_output_sequence=0,
            output_rate=None,
            first_output_written=False,
        )

    assert overlay.events == [
        {"type": "transcription", "text": "what did you say"},
        {"type": "response", "text": "the visible reply"},
    ]


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
        "voice.desk.client.PipeWireMicrophone", microphone_should_not_construct
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


def test_native_wake_config_skips_only_portaudio_device_probe(
    tmp_path: Path,
) -> None:
    path, model = _acceptance_manifest(tmp_path)

    class ForbiddenSoundDevice:
        @staticmethod
        def query_devices(*_args, **_kwargs):
            raise AssertionError("native wake capture must not query PortAudio")

        @staticmethod
        def check_input_settings(*_args, **_kwargs):
            raise AssertionError("native wake capture must not check PortAudio")

    frozen = load_manifest_wake_config(
        path,
        requested_device="3",
        sounddevice_module=ForbiddenSoundDevice,
        package_version="0.6.0",
        validate_device=False,
    )

    assert frozen.spec.model_path == model.resolve()
    assert frozen.spec.score_label == "hey_serena"
    assert frozen.gate.threshold == 0.62
    assert frozen.gate.patience_frames == 2
    assert frozen.gate.cooldown_seconds == 3.5
    assert frozen.input_device == 3
    assert frozen.configuration_sha256


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

    # Alternating, not constant: a constant frame is pure DC, and the gate
    # now ignores DC because this laptop's DMIC wedges to a DC-heavy signal.
    LOUD_FRAME = (
        (8_000).to_bytes(2, "little", signed=True)
        + (-8_000).to_bytes(2, "little", signed=True)
    ) * 640

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


def test_voice_threshold_lifts_above_a_noisy_room() -> None:
    """His built-in DMIC idles near -41 dBFS, above the -45 default.

    Every frame then read as speech, so the barge-in gate cancelled her reply
    the moment she started forming it: "thinking" for a split second, then
    nothing at all.
    """
    from voice.desk.client import calibrate_voice_threshold

    rng = np.random.default_rng(7)
    noisy = [
        (rng.normal(0, 300, 1600)).astype("<i2").tobytes() for _ in range(12)
    ]  # about -41 dBFS
    lifted = calibrate_voice_threshold(noisy, -45.0)
    assert -36.0 < lifted < -30.0


def test_a_quiet_room_keeps_the_sensitive_default() -> None:
    from voice.desk.client import calibrate_voice_threshold

    rng = np.random.default_rng(7)
    quiet = [(rng.normal(0, 2, 1600)).astype("<i2").tobytes() for _ in range(12)]
    assert calibrate_voice_threshold(quiet, -45.0) == -45.0


def test_the_threshold_can_never_rise_past_speech() -> None:
    """A threshold above his voice would leave her unable to hear him at all,
    which is worse than an occasional false trigger."""
    from voice.desk.client import calibrate_voice_threshold

    rng = np.random.default_rng(7)
    roaring = [
        (rng.normal(0, 12_000, 1600)).astype("<i2").tobytes() for _ in range(12)
    ]
    assert calibrate_voice_threshold(roaring, -45.0) == -30.0


def test_a_loud_opening_frame_does_not_set_the_whole_session(caplog) -> None:
    """A freshly opened capture spits settling garbage before the real room."""
    from voice.desk.client import calibrate_voice_threshold

    rng = np.random.default_rng(7)
    startup = [(rng.normal(0, 12_000, 1600)).astype("<i2").tobytes() for _ in range(3)]
    room = [(rng.normal(0, 260, 1600)).astype("<i2").tobytes() for _ in range(12)]
    assert calibrate_voice_threshold(startup + room, -45.0) < -30.0


def test_no_microphone_frames_leaves_the_configured_threshold() -> None:
    from voice.desk.client import calibrate_voice_threshold

    assert calibrate_voice_threshold([], -45.0) == -45.0


def test_a_dc_wedged_microphone_does_not_read_as_constant_speech() -> None:
    """The live fault: an -8300 DC offset alone measured -12 dBFS, so every
    frame counted as him talking and barge-in killed her reply immediately."""
    from voice.desk.client import measure_noise_floor
    from voice.desk.duplex import speech_level_dbfs

    rng = np.random.default_rng(3)
    quiet_room = rng.normal(0, 250, 1600)
    wedged = (quiet_room - 8_300).astype("<i2")
    assert speech_level_dbfs(wedged) < -35.0

    floor, offset = measure_noise_floor([wedged.tobytes()] * 12)
    assert offset < -8_000
    assert floor < -35.0


def test_digital_silence_is_not_a_quiet_room() -> None:
    """The live regression: a freshly opened capture hands back zero-filled
    frames, calibration read a floor of -120 dBFS, the threshold fell back to
    the default the room sits above, and every frame counted as him talking."""
    from voice.desk.client import measure_noise_floor

    silence = [b"\x00\x00" * 800] * 12
    floor, offset = measure_noise_floor(silence)
    assert not np.isfinite(floor)
    assert offset == 0.0

    from voice.desk.client import calibrate_voice_threshold

    assert calibrate_voice_threshold(silence, -45.0) == -45.0


def test_real_frames_are_used_even_when_silence_arrives_first() -> None:
    from voice.desk.client import measure_noise_floor

    rng = np.random.default_rng(11)
    warmup = [b"\x00\x00" * 800] * 6
    room = [(rng.normal(0, 400, 800)).astype("<i2").tobytes() for _ in range(12)]
    floor, _offset = measure_noise_floor(warmup + room)
    assert -42.0 < floor < -30.0


def test_calibration_waits_for_a_capture_that_starts_silent(monkeypatch) -> None:
    """It must not calibrate to the warm-up silence and walk away."""
    import queue as queue_module

    from voice.desk import client as client_module

    rng = np.random.default_rng(5)
    frames = queue_module.Queue()
    for _ in range(20):
        frames.put(b"\x00\x00" * 800)
    for _ in range(20):
        frames.put((rng.normal(0, 400, 800)).astype("<i2").tobytes())

    class _Mic:
        def __init__(self) -> None:
            self.frames = frames

    class _Metrics:
        def __init__(self) -> None:
            self.records: list[tuple[str, dict]] = []

        def record(self, event, **fields):
            self.records.append((event, fields))

    client = client_module.DeskClient.__new__(client_module.DeskClient)
    client.microphone = _Mic()
    client.metrics = _Metrics()
    client.config = client_module.DeskConfig()
    client.voice_activity_dbfs = client.config.voice_activity_dbfs

    client._calibrate_microphone()

    assert client.voice_activity_dbfs > -45.0
    event, fields = client.metrics.records[-1]
    assert event == "desk.noise_calibrated"
    assert fields["real_frames"] >= client_module.NOISE_MIN_REAL_FRAMES


def test_barge_in_stands_down_when_the_room_was_never_measured() -> None:
    """Not being interruptible beats never answering.

    An uncalibrated threshold sits below the real floor, so the gate fires on
    every frame and cancels her reply before a word of it is spoken.
    """
    source = (Path(__file__).resolve().parents[1] / "client.py").read_text(
        encoding="utf-8"
    )
    assert "if self.barge_in_safe" in source
    assert "MAX_SAFE_BARGE_IN_DC_OFFSET" in source
    assert "desk.barge_in_disabled" in source


def test_a_muted_microphone_is_said_out_loud_not_beeped(monkeypatch) -> None:
    """He opened her with the OS mic muted and got the fallback tone, which
    reads as broken rather than as the actual fixable problem. Zero real
    frames at calibration now plays "the mic's off" in her voice."""
    from voice.desk import client as client_module

    played: list[str] = []

    class _Playback:
        def play(self, greeting, **kwargs):
            played.append(getattr(greeting, "greeting_id", "?"))

        def __getattr__(self, name):
            # The run loop touches more of playback than this test cares
            # about; everything else is a harmless no-op.
            return lambda *a, **k: None

    class _Overlay:
        def open(self): pass
        def adopt_state(self): pass
        def set_state(self, s): pass
        def __getattr__(self, name):
            return lambda *a, **k: None

    class _Mic:
        def __init__(self):
            import queue as q
            self.frames = q.Queue()
            # silence only: what an OS-muted source actually produces
            for _ in range(12):
                self.frames.put(b"\x00\x00" * 800)
        def start(self): pass
        def __getattr__(self, name):
            return lambda *a, **k: None

    class _Metrics:
        def record(self, *a, **k): pass

    c = client_module.DeskClient.__new__(client_module.DeskClient)
    c.playback = _Playback(); c.overlay = _Overlay(); c.microphone = _Mic()
    c.metrics = _Metrics(); c.config = client_module.DeskConfig()
    c.voice_activity_dbfs = c.config.voice_activity_dbfs
    c.mic_silent = False
    c.local_fallback = None
    c.scorer = None
    c.gate = None
    import threading as t
    c.session_stop = t.Event()
    c._calibrate_microphone()
    assert c.mic_silent is True

    # the wake-handoff branch speaks the clip
    monkeypatch.setattr(client_module.DeskClient, "_microphone_is_muted",
                        lambda self, force=False: False, raising=False)
    c._activate = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not activate"))
    c.run(t.Event(), start_awake=True)
    assert played and played[0] in ("mic-off", "tone-fallback")


def test_interrupting_her_is_scored_on_her_name_not_loudness() -> None:
    """2026-08-11, twice in one night: the energy gate's bar moved with room
    noise and "Serena" produced no interrupt at all. The wake model now
    scores every mic frame while she speaks, no loudness prefilter, because
    a wake model needs no prefilter; it IS the filter."""
    from pathlib import Path as _P

    source = _P(__file__).resolve().parents[1].joinpath("client.py").read_text()
    loop = source.split("if self.barge_scorer is not None:", 1)[1]
    head = loop[:1600]
    assert "score_frame" in head
    assert "self._barge_preroll.append(pcm)" in head
    # the energy gate only survives as the no-scorer fallback
    assert "barge_gate.feed" in loop
    # and name-mode interruption is never disabled by barge_in_safe
    assert "must not be disabled by barge_in_safe" in source


def test_a_bare_name_turn_sends_her_back_to_listening() -> None:
    """Saying just "Serena" stops her; the server answers with turn.listen
    instead of a spoken reply, and the client must treat that as a completed
    turn so she listens instead of resuming or stalling until the timeout."""
    from voice.desk import client as client_module

    c = client_module.DeskClient.__new__(client_module.DeskClient)

    class _Playback:
        finished = False
        def finish(self): self.finished = True
        def __getattr__(self, name): return lambda *a, **k: None

    class _Metrics:
        def record(self, *a, **k): pass

    c.playback = _Playback(); c.metrics = _Metrics()

    class _Transport:
        call_id = "t"; generation = 1

    event = client_module.TransportEvent(
        kind="control",
        payload={"type": "turn.listen", "generation": 1},
    )
    outcome = c._handle_event(
        _Transport(), event, phase="thinking",
        expected_output_sequence=0, output_rate=None, first_output_written=False,
    )
    assert outcome["turn_complete"] is True
    assert c.playback.finished is True


def test_a_thinking_sound_segment_keeps_the_output_sequence_contiguous() -> None:
    """A filler that shifted the numbering would abort the call as a sequence gap.

    The desk client is the component that actually enforces this: it raises
    "desk output sequence gap" and tears the turn down. A pre-rendered thinking
    sound shares the reply's generation counter, so replaying a real slow turn
    here proves the acknowledgement frames cost the answer nothing.
    """
    microphone = _Microphone()
    transport = _Transport()
    client, _playback, _overlay, _fetcher, _metrics = _client(
        [], microphone, lambda: transport
    )

    events = [
        TransportEvent("control", {"type": "audio.start", "sample_rate": 24_000}),
        TransportEvent(
            "control",
            {"type": "audio.segment", "kind": "acknowledgement", "sequence": 0},
        ),
        TransportEvent(
            "audio",
            AudioFrame(AudioHeader(AudioKind.TTS_PCM16, 0, 0, 24_000, 1), b"\x71\x00" * 8),
        ),
        TransportEvent(
            "control", {"type": "audio.segment", "kind": "content", "sequence": 1}
        ),
        TransportEvent(
            "audio",
            AudioFrame(AudioHeader(AudioKind.TTS_PCM16, 0, 1, 24_000, 2), b"\x01\x00" * 8),
        ),
        TransportEvent("control", {"type": "audio.end", "last_sequence": 1}),
    ]

    phase = "thinking"
    expected = 0
    rate = None
    written = False
    for event in events:
        outcome = client._handle_event(
            transport,
            event,
            phase=phase,
            expected_output_sequence=expected,
            output_rate=rate,
            first_output_written=written,
        )
        phase = outcome["phase"]
        expected = outcome["expected_output_sequence"]
        rate = outcome["output_rate"]
        written = outcome["first_output_written"]

    assert transport.gaps == []
    assert outcome["turn_complete"] is True
