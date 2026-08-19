"""Latency-gated thinking sounds.

The failure these guard against is a real one: memory 414 records that Raghav
rejected the automatic "yeah" backchannel because it fired on every single
turn. The requested behaviour is a short pre-rendered sound, in her own voice,
only when the brain is actually slow, varied, and killable.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from voice.call.brain import BrainDoneMeta, BrainEvent
from voice.call.orchestrator import (
    CallRuntime,
    CallSession,
    _prerendered_chunks,
    _TTSRequest,
)
from voice.call.protocol import AudioKind, parse_audio_frame
from voice.call.thinking_sounds import ThinkingSoundPool, load_clips, render_clips
from voice.call.tts import DeterministicTTSStub


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str | bytes] = []
        self.lock = threading.Lock()

    def send(self, payload) -> None:
        with self.lock:
            self.sent.append(payload)


class FakeSTT:
    async def warm(self) -> None:
        return None

    async def transcribe(
        self, pcm16: bytes, sample_rate: int = 16_000, *, generation: int = 0
    ) -> str:
        return "unused"


class UnusedEndpoint:
    def warm(self) -> None:
        return None

    def reset(self) -> None:
        return None

    def process_pcm(self, pcm: bytes):
        return None


class PacedBrain:
    """A brain that answers after a set stall, like a cold or loaded turn."""

    def __init__(self, stall_seconds: float) -> None:
        self.stall_seconds = stall_seconds

    async def stream_turn(
        self, text, *, call_id, turn_id, request_id=None, journal=True
    ):
        request_id = request_id or "paced"
        yield BrainEvent("start", request_id, backend="paced-test")
        await asyncio.sleep(self.stall_seconds)
        yield BrainEvent("delta", request_id, delta="actual answer.", backend="paced-test")
        yield BrainEvent(
            "done",
            request_id,
            say="actual answer.",
            meta=BrainDoneMeta(elapsed=self.stall_seconds, turns=1),
            backend="paced-test",
        )


class EarlyPartialBrain:
    """A brain whose first delta arrives well before its first sentence."""

    async def stream_turn(
        self, text, *, call_id, turn_id, request_id=None, journal=True
    ):
        request_id = request_id or "early-partial"
        yield BrainEvent("start", request_id, backend="paced-test")
        yield BrainEvent("delta", request_id, delta="well", backend="paced-test")
        await asyncio.sleep(0.1)
        yield BrainEvent(
            "delta", request_id, delta=", actual answer.", backend="paced-test"
        )
        yield BrainEvent(
            "done",
            request_id,
            say="well, actual answer.",
            meta=BrainDoneMeta(elapsed=0.1, turns=1),
            backend="paced-test",
        )


def _install_clips(directory: Path, names: tuple[str, ...], *, samples: int = 480) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(names, start=1):
        # Deliberately unlike the TTS stub's own bytes, so a test cannot pass
        # by mistaking synthesised audio for the pre-rendered clip.
        payload = bytes([0x70 + index, 0]) * samples
        (directory / f"thinking-{name}-24k-mono-s16.raw").write_bytes(payload)


def _session(tmp_path: Path, brain, *, tts=None) -> tuple[CallSession, FakeSocket, object]:
    socket = FakeSocket()
    tts = tts or DeterministicTTSStub(samples_per_sentence=120)
    runtime = CallRuntime(
        stt=FakeSTT(),
        brain=brain,
        tts=tts,
        endpoint_factory=UnusedEndpoint,
        metrics_path=tmp_path / "metrics.jsonl",
    )
    session = CallSession(socket, runtime)
    session._call_started = True
    session.call_id = "thinking-call"
    session.current_generation = 1
    session.telemetry.speech_end(1, source="test")
    return session, socket, tts


def _prepare_env(monkeypatch, clip_dir: Path, *, delay_ms: str = "20") -> None:
    # The legacy knob must stay unset, otherwise it deliberately wins and the
    # pre-rendered path is bypassed.
    monkeypatch.delenv("SERENA_CALL_BACKCHANNEL_DELAY_MS", raising=False)
    monkeypatch.delenv("SERENA_VOICE_THINKING_SOUNDS", raising=False)
    monkeypatch.setenv("SERENA_VOICE_THINKING_DIR", str(clip_dir))
    monkeypatch.setenv("SERENA_VOICE_THINKING_DELAY_MS", delay_ms)


def _segments(socket: FakeSocket) -> list[tuple[str, int]]:
    controls = [json.loads(item) for item in socket.sent if isinstance(item, str)]
    return [
        (item["kind"], item["sequence"])
        for item in controls
        if item["type"] == "audio.segment"
    ]


def _audio_sequences(socket: FakeSocket) -> list[int]:
    sequences = []
    for item in socket.sent:
        if isinstance(item, bytes):
            frame = parse_audio_frame(item, inbound=False)
            if frame.header.kind is AudioKind.TTS_PCM16:
                sequences.append(frame.header.sequence)
    return sequences


def test_a_fast_reply_never_hears_a_thinking_sound(monkeypatch, tmp_path: Path) -> None:
    """Memory 414: the every-turn filler was rejected, so a quick brain stays silent."""

    clips = tmp_path / "audio"
    _install_clips(clips, ("hmm",))
    _prepare_env(monkeypatch, clips, delay_ms="400")

    async def scenario() -> None:
        session, socket, tts = _session(tmp_path, PacedBrain(0.0))
        await session._brain_to_tts(1, "hello")
        assert _segments(socket) == [("content", 0)]
        assert [text for _, text in tts.sentences] == ["actual answer."]

    asyncio.run(scenario())


def test_a_slow_brain_gets_one_pre_rendered_thinking_sound_before_the_answer(
    monkeypatch, tmp_path: Path
) -> None:
    """A cascade cannot answer instantly, so silence past the gate gets one clip."""

    clips = tmp_path / "audio"
    _install_clips(clips, ("hmm",))
    _prepare_env(monkeypatch, clips)

    async def scenario() -> None:
        session, socket, tts = _session(tmp_path, PacedBrain(0.25))
        await session._brain_to_tts(1, "hello")

        segments = _segments(socket)
        assert [kind for kind, _ in segments] == ["acknowledgement", "content"]
        # The filler is played from disk, never synthesised: spending TTS
        # latency to cover TTS latency is the bug this lever exists to avoid.
        assert [text for _, text in tts.sentences] == ["actual answer."]
        # Contiguous from zero, or the desk client tears the call down with a
        # "desk output sequence gap".
        sequences = _audio_sequences(socket)
        assert sequences == list(range(len(sequences)))
        assert len(sequences) > 1
        assert segments[1][1] > segments[0][1]
        # The first thing he hears is the installed clip's own bytes.
        first_pcm = next(
            parse_audio_frame(item, inbound=False).pcm
            for item in socket.sent
            if isinstance(item, bytes)
        )
        assert set(first_pcm[::2]) == {0x71}

    asyncio.run(scenario())


def test_first_brain_delta_stops_the_gate_before_a_sentence_exists(
    monkeypatch, tmp_path: Path
) -> None:
    """A fast partial delta must not trigger filler while punctuation catches up."""

    clips = tmp_path / "audio"
    _install_clips(clips, ("hmm",))
    _prepare_env(monkeypatch, clips)

    async def scenario() -> None:
        session, socket, tts = _session(tmp_path, EarlyPartialBrain())
        await session._brain_to_tts(1, "hello")
        assert _segments(socket) == [("content", 0)]
        assert [text for _, text in tts.sentences] == ["well, actual answer."]

    asyncio.run(scenario())


def test_first_delta_event_cancels_the_pending_clip_at_the_queue_boundary(
    monkeypatch, tmp_path: Path
) -> None:
    """The gate once waited for punctuation and filled after the brain replied."""

    clips = tmp_path / "audio"
    _install_clips(clips, ("hmm",))
    _prepare_env(monkeypatch, clips)

    async def scenario() -> None:
        session, _socket, _tts = _session(tmp_path, PacedBrain(0.1))
        queue: asyncio.Queue[_TTSRequest | None] = asyncio.Queue()
        content_queued = asyncio.Event()
        first_delta_received = asyncio.Event()
        task = asyncio.create_task(
            session._delayed_backchannel(
                1, queue, content_queued, first_delta_received
            )
        )
        await asyncio.sleep(0)
        first_delta_received.set()
        await task
        assert queue.empty()

    asyncio.run(scenario())


def test_deployed_legacy_zero_does_not_disable_thinking_clips(
    monkeypatch, tmp_path: Path
) -> None:
    """The mobile service's old zero-valued knob once disabled the new path."""

    clips = tmp_path / "audio"
    _install_clips(clips, ("hmm",))
    _prepare_env(monkeypatch, clips)
    monkeypatch.setenv("SERENA_CALL_BACKCHANNEL_DELAY_MS", "0")

    async def scenario() -> None:
        session, _socket, _tts = _session(tmp_path, PacedBrain(0.1))
        queue: asyncio.Queue[_TTSRequest | None] = asyncio.Queue()
        await session._delayed_backchannel(
            1,
            queue,
            asyncio.Event(),
            asyncio.Event(),
        )
        request = queue.get_nowait()
        assert request.kind == "acknowledgement"
        assert request.clip is not None

    asyncio.run(scenario())


def test_consecutive_slow_turns_do_not_replay_the_same_clip(
    monkeypatch, tmp_path: Path
) -> None:
    """A filler that repeats back to back stops sounding like a person."""

    clips = tmp_path / "audio"
    _install_clips(clips, ("hmm", "soft-breath"))
    _prepare_env(monkeypatch, clips)

    async def scenario() -> None:
        session, _socket, _tts = _session(tmp_path, PacedBrain(0.25))
        played: list[str] = []
        for generation in (1, 2):
            session.current_generation = generation
            session.telemetry.speech_end(generation, source="test")
            await session._brain_to_tts(generation, "hello")
            played.append(session._thinking_sounds._last_id)
        assert played[0] != played[1]

    asyncio.run(scenario())


def test_the_kill_switch_keeps_her_silent_while_the_brain_is_slow(
    monkeypatch, tmp_path: Path
) -> None:
    """He must be able to switch the sounds off without uninstalling the clips."""

    clips = tmp_path / "audio"
    _install_clips(clips, ("hmm",))
    _prepare_env(monkeypatch, clips)
    monkeypatch.setenv("SERENA_VOICE_THINKING_SOUNDS", "0")

    async def scenario() -> None:
        session, socket, _tts = _session(tmp_path, PacedBrain(0.25))
        await session._brain_to_tts(1, "hello")
        assert _segments(socket) == [("content", 0)]

    asyncio.run(scenario())


def test_a_cancelled_generation_never_plays_its_thinking_sound(
    monkeypatch, tmp_path: Path
) -> None:
    """Barge-in by name cancels the generation, and the filler must go with it."""

    clips = tmp_path / "audio"
    _install_clips(clips, ("hmm",))
    _prepare_env(monkeypatch, clips)

    async def scenario() -> None:
        session, _socket, _tts = _session(tmp_path, PacedBrain(0.25))
        queue: asyncio.Queue[_TTSRequest | None] = asyncio.Queue()
        content_queued = asyncio.Event()
        first_delta_received = asyncio.Event()
        task = asyncio.create_task(
            session._delayed_backchannel(
                1, queue, content_queued, first_delta_received
            )
        )
        await asyncio.sleep(0)
        session._cancelled_generations.add(1)
        await task
        assert queue.empty()

    asyncio.run(scenario())


def test_22050_hz_clip_keeps_every_pcm16_frame_whole(
    monkeypatch, tmp_path: Path
) -> None:
    """A byte-first 22.05 kHz frame calculation once made the answer fail."""

    clips = tmp_path / "audio"
    clips.mkdir()
    (clips / "thinking-hmm-22050hz-mono-s16.raw").write_bytes(
        b"\x71\x00" * 3_000
    )
    _prepare_env(monkeypatch, clips)

    async def scenario() -> None:
        clip = ThinkingSoundPool(directory=clips).take(sample_rate=22_050)
        assert clip is not None
        chunks = [chunk async for chunk in _prerendered_chunks(clip)]
        assert len(chunks) > 1
        assert all(len(chunk.pcm) % 2 == 0 for chunk in chunks)
        assert all(chunk.sample_rate == 22_050 for chunk in chunks)

    asyncio.run(scenario())


def test_a_clip_recorded_at_another_rate_never_reaches_the_reply(
    monkeypatch, tmp_path: Path
) -> None:
    """The worker refuses a rate change mid generation, which would kill the answer."""

    clips = tmp_path / "audio"
    _install_clips(clips, ("hmm",))
    _prepare_env(monkeypatch, clips)

    async def scenario() -> None:
        # A 16 kHz voice with only 24 kHz clips installed must stay silent
        # rather than poison the generation's sample rate.
        session, socket, _tts = _session(
            tmp_path,
            PacedBrain(0.25),
            tts=DeterministicTTSStub(sample_rate=16_000, samples_per_sentence=120),
        )
        await session._brain_to_tts(1, "hello")
        assert _segments(socket) == [("content", 0)]

    asyncio.run(scenario())


def test_unusable_clip_files_are_ignored_instead_of_breaking_the_turn(
    tmp_path: Path,
) -> None:
    """A truncated or oversized render must not become audio she plays at him."""

    clips = tmp_path / "audio"
    clips.mkdir()
    (clips / "thinking-good-24k-mono-s16.raw").write_bytes(b"\x01\x00" * 480)
    (clips / "thinking-odd-24k-mono-s16.raw").write_bytes(b"\x01\x00\x02")
    (clips / "thinking-empty-24k-mono-s16.raw").write_bytes(b"")
    (clips / "thinking-slow-9k-mono-s16.raw").write_bytes(b"\x01\x00" * 480)
    (clips / "thinking-long-24k-mono-s16.raw").write_bytes(b"\x01\x00" * 24_000 * 3)
    (clips / "unrelated.raw").write_bytes(b"\x01\x00" * 480)

    loaded = load_clips(clips)

    assert [clip.clip_id for clip in loaded] == ["thinking-good-24k-mono-s16"]
    assert loaded[0].sample_rate == 24_000


def test_rendered_clips_land_where_the_pool_will_actually_find_them(
    tmp_path: Path,
) -> None:
    """A render the loader then rejects would ship the feature permanently silent."""

    clips = tmp_path / "audio"

    written = asyncio.run(
        render_clips(
            {"hmm": "hmm.", "mm": "mm."},
            directory=clips,
            backend=DeterministicTTSStub(samples_per_sentence=240),
        )
    )

    assert [path.name for path in written] == [
        "thinking-hmm-24k-mono-s16.raw",
        "thinking-mm-24k-mono-s16.raw",
    ]
    loaded = load_clips(clips)
    assert sorted(clip.clip_id for clip in loaded) == [
        "thinking-hmm-24k-mono-s16",
        "thinking-mm-24k-mono-s16",
    ]
    assert all(clip.sample_rate == 24_000 for clip in loaded)


def test_pocket_warmup_provisions_missing_default_clips(
    monkeypatch, tmp_path: Path
) -> None:
    """The deployed audio directory once had only mic-off, leaving this inert."""

    class PocketStub(DeterministicTTSStub):
        name = "pocket-tts-test"

    clips = tmp_path / "audio"
    _prepare_env(monkeypatch, clips)
    runtime = CallRuntime(
        stt=FakeSTT(),
        brain=PacedBrain(0),
        tts=PocketStub(samples_per_sentence=240),
        endpoint_factory=UnusedEndpoint,
        metrics_path=tmp_path / "metrics.jsonl",
    )

    asyncio.run(runtime._run_warmup())

    assert runtime.status["tts"] == "ready"
    assert len(load_clips(clips)) == 4


def test_a_missing_clip_directory_leaves_her_quiet_rather_than_beeping(
    tmp_path: Path,
) -> None:
    """The mic-off tone taught us a bare beep reads as a fault, not as thinking."""

    pool = ThinkingSoundPool(
        directory=tmp_path / "not-installed",
        env={"SERENA_VOICE_THINKING_DELAY_MS": "1500"},
    )

    assert pool.clips == ()
    assert pool.take(sample_rate=24_000) is None
