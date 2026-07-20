from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from voice.desk.fallback import FALLBACK_CWD, LocalColdResponder, _speech_text
from voice.desk.greetings import GreetingAudio
from voice.desk.io import PlaybackStart


class _Microphone:
    def __init__(self) -> None:
        self.frames: queue.Queue[bytes] = queue.Queue()

    def drain(self) -> None:
        pass


class _Playback:
    def __init__(self) -> None:
        self.played: list[str] = []

    def play(self, audio: GreetingAudio) -> PlaybackStart:
        self.played.append(audio.greeting_id)
        return PlaybackStart(time.monotonic_ns(), False)


class _Overlay:
    def __init__(self) -> None:
        self.states: list[str] = []

    def set_state(self, state: str) -> None:
        self.states.append(state)


class _Metrics:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    def record(self, event: str, **fields) -> None:
        self.rows.append((event, fields))


def _audio(text: str = "reply") -> GreetingAudio:
    return GreetingAudio("local", 24_000, b"\x01\x00" * 480, text, time.time())


def _responder(**overrides) -> LocalColdResponder:
    return LocalColdResponder(
        overrides.pop("microphone", _Microphone()),
        overrides.pop("playback", _Playback()),
        overrides.pop("overlay", _Overlay()),
        overrides.pop("metrics", _Metrics()),
        transcriber=overrides.pop("transcriber", lambda _pcm: "hello"),
        brain=overrides.pop("brain", lambda _text: "hi back"),
        synthesizer=overrides.pop("synthesizer", _audio),
        **overrides,
    )


def test_capture_utterance_uses_preroll_and_trailing_silence() -> None:
    microphone = _Microphone()
    silence = b"\x00\x00" * 1_280
    speech = (1_200).to_bytes(2, "little", signed=True) * 1_280
    for frame in [silence, silence, speech, speech, silence, silence]:
        microphone.frames.put(frame)
    responder = _responder(
        microphone=microphone,
        idle_seconds=1,
        max_utterance_seconds=2,
        trailing_silence_ms=160,
    )

    captured = responder._capture_utterance(threading.Event())

    assert captured.startswith(silence + silence)
    assert captured.endswith(silence + silence)
    assert len(captured) == 6 * len(silence)


def test_local_fallback_runs_a_spoken_turn_without_persisting_content(
    tmp_path: Path,
) -> None:
    microphone = _Microphone()
    playback = _Playback()
    overlay = _Overlay()
    metrics = _Metrics()
    responder = _responder(
        microphone=microphone,
        playback=playback,
        overlay=overlay,
        metrics=metrics,
    )
    utterances = iter([b"private pcm", b""])
    responder._capture_utterance = lambda _stop: next(utterances)

    assert responder.run(threading.Event()) == 1
    assert playback.played == ["local"]
    assert overlay.states == ["listening", "thinking", "speaking", "listening", "idle"]
    assert any(event == "desk.local_fallback_reply" for event, _ in metrics.rows)
    assert not any("text" in fields for _event, fields in metrics.rows)
    assert list(tmp_path.iterdir()) == []


def test_missing_local_assets_fail_closed(tmp_path: Path) -> None:
    responder = LocalColdResponder(
        _Microphone(),
        _Playback(),
        _Overlay(),
        _Metrics(),
        whisper_model=tmp_path / "whisper",
        kokoro_model=tmp_path / "kokoro.onnx",
        kokoro_voices=tmp_path / "voices.bin",
        brain=lambda _text: "reply",
    )

    ready, reason = responder.available()

    assert ready is False
    assert "Whisper model is missing" in reason


def test_cold_brain_disables_tools_sessions_and_api_key(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "claude"
    executable.write_text("stub", encoding="utf-8")
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="**short answer**", stderr="")

    monkeypatch.setenv("SERENA_DESK_FALLBACK_CLAUDE", str(executable))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("core.config.read_agent_context", lambda: "persona")
    monkeypatch.setattr("core.brain_state.compact_active", lambda: "state")
    responder = _responder(brain=None)
    responder._brain_override = None

    assert responder._answer("private question") == "short answer"
    args = captured["args"]
    assert args[args.index("--tools") + 1] == ""
    assert "--no-session-persistence" in args
    assert "--safe-mode" in args
    assert captured["kwargs"]["env"].get("ANTHROPIC_API_KEY") is None
    assert captured["kwargs"]["stdin"] is not None


def test_speech_text_removes_non_spoken_markdown() -> None:
    raw = "# title\n- **hello** [there](https://example.test)\n```json\n{}\n```"

    assert _speech_text(raw) == "title hello there"


def test_cold_lane_working_directory_stays_inside_service_cache_root() -> None:
    expected_root = Path.home() / ".cache" / "serena"

    assert FALLBACK_CWD.parent == expected_root
