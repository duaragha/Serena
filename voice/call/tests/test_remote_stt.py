"""Hosted recognition, and the local model that must still catch it.

A call where the network is down has to keep working, so every test here that
breaks the hosted path asserts the local worker answered instead.
"""

from __future__ import annotations

import asyncio
import wave

import pytest

from voice.call import stt as stt_module
from voice.call.stt import (
    GroqWhisperWorker,
    create_stt_backend,
    load_groq_key,
    pcm16_to_wav,
)
from voice.call.protocol import MIC_SAMPLE_RATE


class _Local:
    name = "faster-whisper"
    execution = "local"

    def __init__(self, text: str = "local text") -> None:
        self.text = text
        self.warmed = False
        self.calls: list[int] = []
        self.cancelled: list[int] = []
        self.closed = False

    async def warm(self) -> None:
        self.warmed = True

    async def transcribe(self, pcm16, sample_rate=MIC_SAMPLE_RATE, *, generation=0):
        self.calls.append(generation)
        return self.text

    async def cancel(self, generation: int) -> bool:
        self.cancelled.append(generation)
        return True

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def no_ambient_key(monkeypatch, tmp_path):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("SERENA_CALL_STT_BACKEND", raising=False)
    monkeypatch.setenv("SERENA_CALL_GROQ_ENV", str(tmp_path / "absent.env"))
    monkeypatch.setenv("SERENA_CALL_PRIVATE_VOCABULARY", str(tmp_path / "absent.txt"))


def _speech(seconds: float = 0.2) -> bytes:
    return b"\x01\x02" * int(MIC_SAMPLE_RATE * seconds)


def test_pcm_is_wrapped_as_a_real_wav_file() -> None:
    import io

    wav = pcm16_to_wav(_speech(0.1))
    with wave.open(io.BytesIO(wav), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == MIC_SAMPLE_RATE
        assert handle.getnframes() == int(MIC_SAMPLE_RATE * 0.1)


def test_the_key_comes_from_its_own_file_when_the_env_is_empty(monkeypatch, tmp_path):
    env = tmp_path / "groq.env"
    env.write_text("# key\nGROQ_API_KEY='gsk-from-file'\n", encoding="utf-8")
    monkeypatch.setenv("SERENA_CALL_GROQ_ENV", str(env))
    assert load_groq_key() == "gsk-from-file"
    monkeypatch.setenv("GROQ_API_KEY", "gsk-from-env")
    assert load_groq_key() == "gsk-from-env"


def test_a_hosted_transcript_wins_and_carries_the_vocabulary(monkeypatch):
    local = _Local()
    worker = GroqWhisperWorker(fallback=local, api_key="gsk-test")
    posted: list[bytes] = []

    def _post(wav: bytes) -> str:
        posted.append(wav)
        return "  what do you know about Kamakshi  "

    monkeypatch.setattr(worker, "_post", _post)
    text = asyncio.run(worker.transcribe(_speech()))
    assert text == "what do you know about Kamakshi"
    assert local.calls == []
    assert posted and posted[0][:4] == b"RIFF"
    assert "Serena" in worker.prompt


def test_a_dead_network_falls_back_to_the_local_model(monkeypatch):
    local = _Local("kumachi, but at least she answered")
    worker = GroqWhisperWorker(fallback=local, api_key="gsk-test")

    def _boom(wav: bytes) -> str:
        raise OSError("no route to host")

    monkeypatch.setattr(worker, "_post", _boom)
    assert asyncio.run(worker.transcribe(_speech(), generation=3)) == local.text
    assert local.calls == [3]


def test_an_empty_hosted_answer_still_gets_a_local_try(monkeypatch):
    local = _Local()
    worker = GroqWhisperWorker(fallback=local, api_key="gsk-test")
    monkeypatch.setattr(worker, "_post", lambda wav: "   ")
    assert asyncio.run(worker.transcribe(_speech())) == "local text"


def test_without_a_key_it_never_touches_the_network(monkeypatch):
    local = _Local()
    worker = GroqWhisperWorker(fallback=local, api_key="")

    def _never(wav: bytes) -> str:
        raise AssertionError("posted without a key")

    monkeypatch.setattr(worker, "_post", _never)
    assert asyncio.run(worker.transcribe(_speech())) == "local text"


def test_a_transcript_for_a_cancelled_turn_is_dropped(monkeypatch):
    local = _Local()
    worker = GroqWhisperWorker(fallback=local, api_key="gsk-test")
    monkeypatch.setattr(worker, "_post", lambda wav: "he already moved on")
    asyncio.run(worker.cancel(7))
    assert asyncio.run(worker.transcribe(_speech(), generation=7)) == ""
    assert local.calls == []


def test_warming_and_closing_reach_the_local_model():
    local = _Local()
    worker = GroqWhisperWorker(fallback=local, api_key="gsk-test")
    asyncio.run(worker.warm())
    assert local.warmed and worker.warmed is True  # it reports the local model's
    worker.close()
    assert local.closed


def test_the_backend_is_chosen_by_the_key_and_the_switch(monkeypatch):
    monkeypatch.setattr(stt_module, "FasterWhisperWorker", _Local)
    assert isinstance(create_stt_backend(), _Local)
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    assert isinstance(create_stt_backend(), GroqWhisperWorker)
    # He can always pin the local model, key or no key.
    monkeypatch.setenv("SERENA_CALL_STT_BACKEND", "local")
    assert isinstance(create_stt_backend(), _Local)
    # And asking for hosted without a key is an error, not a silent downgrade.
    monkeypatch.setenv("SERENA_CALL_STT_BACKEND", "groq")
    monkeypatch.delenv("GROQ_API_KEY")
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        create_stt_backend()


def test_private_names_extend_the_vocabulary_without_entering_the_repo(
    monkeypatch, tmp_path
):
    private = tmp_path / "vocabulary.txt"
    private.write_text("Kamakshi\nJenna\n", encoding="utf-8")
    monkeypatch.setenv("SERENA_CALL_PRIVATE_VOCABULARY", str(private))
    hotwords = stt_module.load_whisper_hotwords()
    assert "Kamakshi" in hotwords and "Serena" in hotwords
    repo_vocabulary = stt_module.DEFAULT_VOCABULARY_PATH.read_text(encoding="utf-8")
    assert "Kamakshi" not in repo_vocabulary
