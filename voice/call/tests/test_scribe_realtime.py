"""Streaming recognition, and the layers under it.

The point of streaming is that the transcript exists before he stops talking.
The point of the fallbacks is that a dropped socket costs accuracy and never a
turn, so every failure here asserts a local answer still arrived.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from voice.call import stt as stt_module
from voice.call.protocol import MIC_SAMPLE_RATE
from voice.call.stt import (
    ScribeRealtimeWorker,
    _ScribeSession,
    create_stt_backend,
    load_elevenlabs_key,
    load_keyterms,
)


class _Fallback:
    name = "faster-whisper"
    execution = "local"

    def __init__(self, text: str = "local text") -> None:
        self.text = text
        self.warmed = False
        self.calls: list[int] = []
        self.closed = False

    async def warm(self) -> None:
        self.warmed = True

    async def transcribe(self, pcm16, sample_rate=MIC_SAMPLE_RATE, *, generation=0):
        self.calls.append(generation)
        return self.text

    async def cancel(self, generation: int) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


class _Socket:
    """A scripted websocket: what the client sent, and what it hears back.

    A live socket blocks in recv when the far end has nothing to say, so this
    one does too. Raising there instead would fake a dropped connection and
    every test would silently be testing the fallback path.
    """

    def __init__(self, replies: list[dict] | None = None) -> None:
        import threading

        self.sent: list[dict] = []
        self._replies = list(replies or [])
        self.closed = False
        self._gone = threading.Event()

    def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def recv(self) -> str:
        if self._replies:
            return json.dumps(self._replies.pop(0))
        self._gone.wait(2.0)
        raise OSError("socket closed")

    def close(self) -> None:
        self.closed = True
        self._gone.set()


@pytest.fixture(autouse=True)
def no_ambient_keys(monkeypatch, tmp_path):
    for name in ("ELEVENLABS_API_KEY", "XI_API_KEY", "GROQ_API_KEY",
                 "SERENA_CALL_STT_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SERENA_CALL_ELEVENLABS_ENV", str(tmp_path / "absent.env"))
    monkeypatch.setenv("SERENA_CALL_GROQ_ENV", str(tmp_path / "absent.env"))
    monkeypatch.setenv("SERENA_CALL_PRIVATE_VOCABULARY", str(tmp_path / "absent.txt"))


def _worker(monkeypatch, replies=None, *, fallback=None):
    """A worker whose sessions talk to a scripted socket instead of the network."""

    socket = _Socket(replies)
    worker = ScribeRealtimeWorker(
        fallback=fallback or _Fallback(), api_key="xi-test", keyterms=["Kamakshi"])

    def _start(self) -> bool:
        import threading

        self._socket = socket
        for target, name in ((self._send_loop, "s"), (self._read_loop, "r")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        return True

    monkeypatch.setattr(_ScribeSession, "start", _start)
    return worker, socket


def test_the_key_comes_from_its_own_file(monkeypatch, tmp_path):
    env = tmp_path / "elevenlabs.env"
    env.write_text("XI_API_KEY=xi-from-file\n", encoding="utf-8")
    monkeypatch.setenv("SERENA_CALL_ELEVENLABS_ENV", str(env))
    assert load_elevenlabs_key() == "xi-from-file"
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-from-env")
    assert load_elevenlabs_key() == "xi-from-env"


def test_frames_stream_as_audio_chunks_without_committing(monkeypatch):
    worker, socket = _worker(monkeypatch)
    for _ in range(3):
        worker.feed(b"\x01\x02" * 160, generation=1)
    asyncio.run(asyncio.sleep(0.2))
    assert len(socket.sent) == 3
    assert all(item["message_type"] == "input_audio_chunk" for item in socket.sent)
    assert all(item["commit"] is False for item in socket.sent)
    assert all(item["sample_rate"] == MIC_SAMPLE_RATE for item in socket.sent)
    worker.close()


def test_the_committed_transcript_is_what_the_turn_uses(monkeypatch):
    worker, socket = _worker(monkeypatch, replies=[
        {"message_type": "partial_transcript", "text": "what do you know about"},
        {"message_type": "committed_transcript", "text": "what do you know about Kamakshi"},
    ])
    worker.feed(b"\x01\x02" * 160, generation=1)
    text = asyncio.run(worker.transcribe(b"\x01\x02" * 160, generation=1))
    assert text == "what do you know about Kamakshi"
    assert socket.sent[-1]["commit"] is True
    assert worker.last_backend == worker.name


def test_a_partial_is_readable_mid_turn_at_no_cost(monkeypatch):
    worker, _ = _worker(monkeypatch, replies=[
        {"message_type": "partial_transcript", "text": "hey serena"},
    ])
    worker.feed(b"\x01\x02" * 160, generation=4)
    asyncio.run(asyncio.sleep(0.2))
    assert worker.partial_text(4) == "hey serena"
    # Reading a partial must not end the segment.
    assert worker._sessions.get(4) is not None
    worker.close()


def test_a_socket_that_never_connects_falls_through(monkeypatch):
    fallback = _Fallback("local heard it")
    worker = ScribeRealtimeWorker(fallback=fallback, api_key="xi-test", keyterms=[])
    monkeypatch.setattr(_ScribeSession, "start", lambda self: False)
    worker.feed(b"\x01\x02" * 160, generation=2)
    assert asyncio.run(worker.transcribe(b"\x01\x02" * 160, generation=2)) == "local heard it"
    assert fallback.calls == [2]


def test_a_silent_commit_falls_through_to_the_local_model(monkeypatch):
    fallback = _Fallback("local heard it")
    worker, _ = _worker(monkeypatch, replies=[], fallback=fallback)
    monkeypatch.setattr(stt_module, "SCRIBE_COMMIT_TIMEOUT_SECONDS", 0.05)
    worker.feed(b"\x01\x02" * 160, generation=3)
    assert asyncio.run(worker.transcribe(b"\x01\x02" * 160, generation=3)) == "local heard it"
    assert fallback.calls == [3]


def test_without_a_key_it_never_opens_a_socket(monkeypatch):
    worker = ScribeRealtimeWorker(fallback=_Fallback(), api_key="", keyterms=[])
    monkeypatch.setattr(
        _ScribeSession, "start",
        lambda self: (_ for _ in ()).throw(AssertionError("connected with no key")))
    worker.feed(b"\x01\x02" * 160, generation=1)
    assert asyncio.run(worker.transcribe(b"\x01\x02" * 160, generation=1)) == "local text"


def test_barge_in_drops_the_open_segment(monkeypatch):
    worker, socket = _worker(monkeypatch, replies=[
        {"message_type": "partial_transcript", "text": "wait no"},
    ])
    worker.feed(b"\x01\x02" * 160, generation=5)
    asyncio.run(asyncio.sleep(0.1))
    asyncio.run(worker.cancel(5))
    assert socket.closed is True
    assert worker.partial_text(5) == ""


def test_the_url_carries_the_model_and_his_names(monkeypatch):
    worker, _ = _worker(monkeypatch)
    url = worker._url()
    assert url.startswith("wss://api.elevenlabs.io/v1/speech-to-text/realtime?")
    assert "model_id=scribe_v2_realtime" in url
    assert "keyterms=Kamakshi" in url
    assert f"audio_format=pcm_{MIC_SAMPLE_RATE}" in url
    # Our own VAD owns turn taking, so the service must not commit on its own.
    assert "commit_strategy=manual" in url
    worker.close()


def test_multi_word_names_survive_as_keyterms(monkeypatch, tmp_path):
    private = tmp_path / "vocabulary.txt"
    private.write_text("Pocket TTS\nKamakshi\n", encoding="utf-8")
    monkeypatch.setenv("SERENA_CALL_PRIVATE_VOCABULARY", str(private))
    terms = load_keyterms()
    assert "Pocket TTS" in terms and "Kamakshi" in terms
    # The whisper string cannot express that, which is why both exist.
    assert "Pocket TTS" in stt_module.load_whisper_hotwords()


def test_the_chain_is_scribe_then_groq_then_local(monkeypatch):
    monkeypatch.setattr(stt_module, "FasterWhisperWorker", _Fallback)
    assert isinstance(create_stt_backend(), _Fallback)

    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    assert isinstance(create_stt_backend(), stt_module.GroqWhisperWorker)

    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test")
    chain = create_stt_backend()
    assert isinstance(chain, ScribeRealtimeWorker)
    assert isinstance(chain.fallback, stt_module.GroqWhisperWorker)
    assert isinstance(chain.fallback.fallback, _Fallback)

    # Scribe with no Groq still lands on the local model.
    monkeypatch.delenv("GROQ_API_KEY")
    assert isinstance(create_stt_backend().fallback, _Fallback)

    # And an explicit ask with no key is an error, not a silent downgrade.
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    monkeypatch.setenv("SERENA_CALL_STT_BACKEND", "scribe")
    with pytest.raises(RuntimeError, match="ELEVENLABS_API_KEY"):
        create_stt_backend()


def test_health_names_the_recognizer_and_its_fallbacks(monkeypatch):
    """A hosted chain used to vanish from /health, which hid what was hearing him."""

    from voice.call.orchestrator import CallRuntime

    class _Middle:
        name = "groq-whisper-large-v3-turbo"
        execution = "remote"
        model_source = "hosted"

        def __init__(self, fallback):
            self.fallback = fallback

    local = _Fallback()
    chain = ScribeRealtimeWorker(fallback=_Middle(local), api_key="xi-test", keyterms=[])
    runtime = CallRuntime(stt=chain, brain=None, tts=None, endpoint_factory=None)
    stt = runtime.model_details["stt"]
    assert stt["backend"] == "elevenlabs-scribe-v2-realtime"
    assert stt["execution"] == "remote"
    assert stt["streaming"] is True
    assert stt["fallbacks"] == ["groq-whisper-large-v3-turbo", "faster-whisper"]
    # A local-only runtime still reports its device, as it always did.
    from voice.call.stt import WhisperDevice

    class _LocalOnly(_Fallback):
        execution = "local"
        model_source = "local_path"
        device = WhisperDevice("cpu", "int8", "test")

    plain = CallRuntime(stt=_LocalOnly(), brain=None, tts=None, endpoint_factory=None)
    assert plain.model_details["stt"]["device"] == "cpu"
    assert plain.model_details["stt"]["compute_type"] == "int8"
    assert plain.model_details["stt"]["streaming"] is False
