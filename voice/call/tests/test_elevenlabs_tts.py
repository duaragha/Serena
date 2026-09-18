"""Her ElevenLabs voice, and the local engine that has to catch it.

This voice is metered: 0.5 credits per character, measured, against a 10,000
credit month. It will run out mid-sentence one day, and the only acceptable
answer to that is Pocket finishing the sentence, never silence.
"""

from __future__ import annotations

import asyncio

import pytest

from voice.call.tts import ElevenLabsTTSBackend, PCMChunk, create_tts_backend


class _Local:
    name = "pocket-tts-remote"
    supports_true_stream = True

    def __init__(self) -> None:
        self.warmed = False
        self.spoken: list[str] = []
        self.cancelled: list[int | None] = []

    async def warm(self) -> None:
        self.warmed = True

    async def stream(self, sentence, *, generation):
        self.spoken.append(sentence)
        yield PCMChunk(pcm=b"\x00\x01" * 8, sample_rate=24_000)

    async def cancel(self, generation=None) -> None:
        self.cancelled.append(generation)

    def retire_generation(self, generation: int) -> None:
        return None


def _backend(monkeypatch, chunks=None, error=None, **kwargs):
    local = _Local()
    eleven = ElevenLabsTTSBackend(
        fallback=local, api_key="xi-test", voice_id="voice-1", **kwargs)

    def fake_read(sentence, queue, stop):
        for chunk in chunks or []:
            queue.put_nowait(chunk)
        if error is not None:
            queue.put_nowait(error)
        queue.put_nowait(None)

    monkeypatch.setattr(eleven, "_read_chunks", fake_read)
    return eleven, local


def _drain(backend, sentence="hey, raghav.", generation=1):
    async def run():
        return [chunk async for chunk in backend.stream(sentence, generation=generation)]

    return asyncio.run(run())


def test_her_voice_streams_pcm_at_the_rate_the_call_already_speaks(monkeypatch):
    eleven, local = _backend(monkeypatch, chunks=[b"\x01\x02" * 64, b"\x03\x04" * 64])
    chunks = _drain(eleven)
    # Socket reads are re-cut into protocol-sized frames, so what matters is
    # the rate and that every byte arrives, not how the reads were split.
    assert {c.sample_rate for c in chunks} == {24_000}
    assert b"".join(c.pcm for c in chunks) == b"\x01\x02" * 64 + b"\x03\x04" * 64
    assert local.spoken == []
    assert eleven.last_backend == "elevenlabs"


def test_a_refused_request_finishes_the_sentence_locally(monkeypatch):
    """Quota, a bad key or a dropped socket all arrive here as an exception."""

    eleven, local = _backend(monkeypatch, error=RuntimeError("quota_exceeded"))
    chunks = _drain(eleven, "that job is blocked.")
    assert chunks, "the sentence must still be spoken"
    assert local.spoken == ["that job is blocked."]
    assert eleven.last_backend == "pocket-tts-remote"


def test_silence_from_the_service_is_not_accepted_as_an_answer(monkeypatch):
    eleven, local = _backend(monkeypatch, chunks=[])
    chunks = _drain(eleven)
    assert chunks and local.spoken == ["hey, raghav."]


def test_without_a_voice_id_it_never_calls_the_service(monkeypatch):
    local = _Local()
    eleven = ElevenLabsTTSBackend(fallback=local, api_key="xi-test", voice_id="")

    def explode(sentence, queue, stop):
        raise AssertionError("called the service with no voice id")

    monkeypatch.setattr(eleven, "_read_chunks", explode)
    assert eleven.configured() is False
    assert _drain(eleven)
    assert local.spoken == ["hey, raghav."]


def test_barge_in_stops_the_voice_and_the_fallback(monkeypatch):
    eleven, local = _backend(monkeypatch, chunks=[b"\x01\x02" * 64])
    asyncio.run(eleven.cancel(7))
    assert _drain(eleven, generation=7) == []
    assert local.cancelled == [7]
    # A different turn is unaffected.
    assert _drain(eleven, generation=8)


def test_an_empty_sentence_costs_nothing(monkeypatch):
    eleven, local = _backend(monkeypatch, chunks=[b"\x01\x02" * 8])
    assert _drain(eleven, "   ") == []
    assert local.spoken == []


def test_the_backend_is_never_selected_implicitly(monkeypatch):
    """A key for transcription is not consent to spend credits on speech."""

    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test")
    monkeypatch.delenv("SERENA_CALL_ELEVEN_VOICE_ID", raising=False)
    monkeypatch.setenv("SERENA_CALL_TTS_BACKEND", "kokoro")
    assert not isinstance(create_tts_backend(), ElevenLabsTTSBackend)

    monkeypatch.setenv("SERENA_CALL_TTS_BACKEND", "elevenlabs")
    with pytest.raises(RuntimeError, match="SERENA_CALL_ELEVEN_VOICE_ID"):
        create_tts_backend()


def test_warming_reaches_the_local_engine(monkeypatch):
    eleven, local = _backend(monkeypatch)
    asyncio.run(eleven.warm())
    assert local.warmed is True
    assert eleven.metadata["voice"] == "voice-1"
    assert eleven.metadata["model"] == "eleven_flash_v2_5"


def test_frames_never_exceed_the_fifty_millisecond_protocol_limit(monkeypatch):
    """The host rejected every 4 kB chunk: "payload exceeds 50 ms frame limit"."""

    from voice.call.tts import ELEVEN_FRAME_BYTES, ELEVEN_SAMPLE_RATE

    # One 4 kB socket read, the size the service actually returns.
    eleven, local = _backend(monkeypatch, chunks=[b"\x01\x02" * 2048])
    chunks = _drain(eleven)

    assert chunks, "audio must still be produced"
    for chunk in chunks:
        milliseconds = len(chunk.pcm) / 2 / ELEVEN_SAMPLE_RATE * 1000
        assert milliseconds <= 50, f"{milliseconds:.0f} ms frame would be refused"
    # Nothing is lost in the re-cutting.
    assert b"".join(c.pcm for c in chunks) == b"\x01\x02" * 2048
    assert ELEVEN_FRAME_BYTES == 1920


def test_a_tail_shorter_than_a_frame_is_still_spoken(monkeypatch):
    eleven, _ = _backend(monkeypatch, chunks=[b"\x05\x06" * 100])
    chunks = _drain(eleven)
    assert b"".join(c.pcm for c in chunks) == b"\x05\x06" * 100


def test_the_desk_and_the_phone_read_one_shared_voice(tmp_path, monkeypatch):
    """She answered the phone as Grace and the desk as Kokoro for five days.

    Neither host set anything wrong; the laptop's unit simply set nothing, and
    the built-in default is Kokoro. The synced file is what closes that gap.
    """

    from voice.call.stack_config import stack_config, voice_setting

    config = tmp_path / "voice-stack.json"
    config.write_text(
        '{"tts": {"backend": "elevenlabs", "voice_id": "grace-1"},'
        ' "stt": {"backend": "scribe"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("SERENA_VOICE_STACK_CONFIG", str(config))
    monkeypatch.delenv("SERENA_CALL_TTS_BACKEND", raising=False)
    monkeypatch.delenv("SERENA_CALL_ELEVEN_VOICE_ID", raising=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test")

    assert stack_config()["tts"]["voice_id"] == "grace-1"
    backend = create_tts_backend()
    assert isinstance(backend, ElevenLabsTTSBackend)
    assert backend.voice_id == "grace-1"
    assert voice_setting(
        "stt", "backend", env="SERENA_CALL_STT_BACKEND", default="auto") == "scribe"


def test_one_machine_can_still_be_pinned_for_a_test(tmp_path, monkeypatch):
    config = tmp_path / "voice-stack.json"
    config.write_text('{"tts": {"backend": "elevenlabs", "voice_id": "grace-1"}}',
                      encoding="utf-8")
    monkeypatch.setenv("SERENA_VOICE_STACK_CONFIG", str(config))
    monkeypatch.setenv("SERENA_CALL_TTS_BACKEND", "kokoro")

    assert not isinstance(create_tts_backend(), ElevenLabsTTSBackend)


def test_an_unreadable_stack_file_never_costs_her_a_voice(tmp_path, monkeypatch):
    """Built-in defaults take over; a bad edit must not make her mute."""

    from voice.call.stack_config import stack_config

    broken = tmp_path / "voice-stack.json"
    broken.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv("SERENA_VOICE_STACK_CONFIG", str(broken))
    monkeypatch.delenv("SERENA_CALL_TTS_BACKEND", raising=False)

    assert stack_config() == {}
    assert create_tts_backend().name == "kokoro-onnx"


def test_the_checked_in_stack_is_the_voice_both_machines_should_run(monkeypatch):
    """Guards the file itself: it is the only thing keeping the two in step."""

    from voice.call.stack_config import DEFAULT_CONFIG_PATH, stack_config

    monkeypatch.delenv("SERENA_VOICE_STACK_CONFIG", raising=False)
    assert DEFAULT_CONFIG_PATH.is_file()
    shipped = stack_config()
    assert shipped["tts"]["backend"] == "elevenlabs"
    assert shipped["tts"]["voice_id"]
    assert shipped["stt"]["backend"] == "auto"
