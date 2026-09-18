"""One local faster-whisper worker for Serena calls."""

from __future__ import annotations

import base64
import contextlib
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .process_worker import CancellableModelProcess
from .protocol import MIC_SAMPLE_RATE
from .stack_config import voice_setting

log_stt = logging.getLogger("serena.call.stt")

DEFAULT_VOCABULARY_PATH = Path(__file__).with_name("vocabulary.txt")
# Personal names live here, off the public repo and off Syncthing.
PRIVATE_VOCABULARY_PATH = Path("~/.config/serena/vocabulary.txt")
DEFAULT_BEAM_SIZE = 5


def load_whisper_beam_size() -> int:
    """Return a bounded decoder beam instead of the old greedy search."""

    raw = os.environ.get("SERENA_CALL_WHISPER_BEAM_SIZE", "").strip()
    if not raw:
        return DEFAULT_BEAM_SIZE
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_BEAM_SIZE
    return min(max(value, 1), 10)


def _vocabulary_terms(path: Path, terms: list[str]) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        term = line.split("#", 1)[0].strip()
        if term and term not in terms:
            terms.append(term)


def load_vocabulary_terms(path: str | Path | None = None) -> list[str]:
    """Names the recognizer should expect, from the repo and from this machine.

    The names of people he actually talks about are exactly the words a small
    model gets wrong, and exactly the words that must not be committed to a
    public repository. So the repo file holds the project vocabulary and
    PRIVATE_VOCABULARY_PATH holds the rest, unsynced and uncommitted.
    """

    vocabulary_path = Path(
        path
        or os.environ.get("SERENA_CALL_VOCABULARY")
        or DEFAULT_VOCABULARY_PATH
    ).expanduser()
    private_path = Path(
        os.environ.get("SERENA_CALL_PRIVATE_VOCABULARY") or PRIVATE_VOCABULARY_PATH
    ).expanduser()
    terms: list[str] = []
    _vocabulary_terms(vocabulary_path, terms)
    _vocabulary_terms(private_path, terms)
    for term in os.environ.get("SERENA_CALL_WHISPER_HOTWORDS", "").split(","):
        term = term.strip()
        if term and term not in terms:
            terms.append(term)
    return terms


def load_whisper_hotwords(path: str | Path | None = None) -> str:
    """The same terms as one string, which is what faster-whisper takes."""

    return " ".join(load_vocabulary_terms(path))[:2_000]


def load_keyterms(path: str | Path | None = None) -> list[str]:
    """The same terms as a list, which is what Scribe's keyterms takes.

    A multi-word name survives here and does not in the whisper string, which
    is the whole reason this returns a list.
    """

    return load_vocabulary_terms(path)


@dataclass(frozen=True, slots=True)
class WhisperDevice:
    device: str
    compute_type: str
    reason: str


def select_whisper_device() -> WhisperDevice:
    """Use CUDA only when CTranslate2 confirms a usable CUDA device."""
    try:
        import ctranslate2

        count = int(ctranslate2.get_cuda_device_count())
        if count > 0:
            supported = set(ctranslate2.get_supported_compute_types("cuda"))
            for compute_type in ("float16", "int8_float16", "int8"):
                if compute_type in supported:
                    return WhisperDevice(
                        "cuda", compute_type, "CTranslate2 reported CUDA support"
                    )
    except (ImportError, RuntimeError, OSError, ValueError):
        pass
    return WhisperDevice(
        "cpu",
        "int8",
        "CTranslate2 did not report CUDA; CPU int8 is required on AMD/DirectML",
    )


class FasterWhisperWorker:
    """Warm faster-whisper process that can be terminated on cancellation."""

    name = "faster-whisper"
    execution = "local"
    model_source = "local_path"

    def __init__(self, model: str | Path | None = None) -> None:
        bundled = (
            Path(__file__).resolve().parents[1]
            / "models"
            / "faster-whisper-small.en"
        )
        configured = os.environ.get("SERENA_CALL_WHISPER_MODEL")
        self.model_ref = str(model or configured or bundled)
        self.hotwords = load_whisper_hotwords()
        self.beam_size = load_whisper_beam_size()
        self.device = select_whisper_device()
        self._worker = CancellableModelProcess(
            "stt",
            {
                "model": self.model_ref,
                "device": self.device.device,
                "compute_type": self.device.compute_type,
                "sample_rate": MIC_SAMPLE_RATE,
                "hotwords": self.hotwords,
                "beam_size": self.beam_size,
            },
            network_disabled=True,
        )

    @property
    def warmed(self) -> bool:
        return self._worker.warmed

    async def warm(self) -> None:
        path = Path(self.model_ref).expanduser()
        if not path.exists():
            raise RuntimeError(
                f"local faster-whisper model is missing at {path}; "
                "set SERENA_CALL_WHISPER_MODEL to a staged local model"
            )
        await self._worker.warm()

    async def transcribe(
        self,
        pcm16: bytes,
        sample_rate: int = MIC_SAMPLE_RATE,
        *,
        generation: int = 0,
    ) -> str:
        if sample_rate != MIC_SAMPLE_RATE:
            raise ValueError(f"STT input must be {MIC_SAMPLE_RATE} Hz")
        if len(pcm16) % 2:
            raise ValueError("STT PCM16 input has a partial sample")
        response = await self._worker.request(
            {
                "op": "transcribe",
                "pcm_b64": base64.b64encode(bytes(pcm16)).decode("ascii"),
            },
            generation=generation,
        )
        return str(response.get("text") or "").strip()

    async def cancel(self, generation: int) -> bool:
        return await self._worker.cancel(generation)

    def close(self) -> None:
        self._worker.close()


GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"
REMOTE_STT_TIMEOUT_SECONDS = 8.0


def load_groq_key() -> str:
    """The key from the environment, or from its own 0600 file."""

    key = os.environ.get("GROQ_API_KEY", "").strip()
    if key:
        return key
    path = Path(
        os.environ.get("SERENA_CALL_GROQ_ENV")
        or Path.home() / ".config" / "serena" / "groq.env"
    ).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in raw.splitlines():
        name, _, value = line.strip().partition("=")
        if name.strip() == "GROQ_API_KEY":
            return value.strip().strip("'\"")
    return ""


def pcm16_to_wav(pcm16: bytes, sample_rate: int = MIC_SAMPLE_RATE) -> bytes:
    """Wrap raw mono PCM16 in a WAV header, which is what the API accepts."""

    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm16)
    return buffer.getvalue()


class GroqWhisperWorker:
    """whisper-large-v3-turbo over the network, with the local model beneath.

    tiny.en is what fits on his CPU inside the answer-latency budget, and it
    mishears every proper noun that matters: a name it has never seen comes
    back as a different word, and she then reasons confidently about the wrong
    one. A hosted large-v3 pass is both more accurate and faster than the local
    model, so the call gets better on both axes at once.

    The local worker stays warm underneath and answers whenever the network,
    the key or the service is not there. Hosted recognition is therefore a
    preference, never a dependency: with the key removed, calls still work.
    """

    name = "groq-whisper-large-v3-turbo"
    execution = "remote"
    model_source = "hosted"

    def __init__(
        self,
        *,
        fallback: FasterWhisperWorker | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self.fallback = fallback if fallback is not None else FasterWhisperWorker()
        self.api_key = api_key if api_key is not None else load_groq_key()
        self.model = model or os.environ.get("SERENA_CALL_REMOTE_STT_MODEL") or GROQ_MODEL
        # The same terms the local model biases on; the API takes them as a
        # prompt, which is how "Kamakshi" stops coming back as "kumachi".
        self.prompt = load_whisper_hotwords()
        self.timeout = REMOTE_STT_TIMEOUT_SECONDS
        self.last_backend = ""
        self._cancelled: set[int] = set()

    @property
    def warmed(self) -> bool:
        return self.fallback.warmed

    async def warm(self) -> None:
        await self.fallback.warm()

    def _post(self, wav: bytes) -> str:
        import json
        import urllib.request

        boundary = "----serena-call-stt"
        fields = [("model", self.model), ("response_format", "json"),
                  ("temperature", "0"), ("language", "en")]
        if self.prompt:
            fields.append(("prompt", self.prompt))
        parts: list[bytes] = []
        for name, value in fields:
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n".encode("utf-8"))
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"turn.wav\"\r\nContent-Type: audio/wav\r\n\r\n".encode("utf-8"))
        parts.append(wav)
        parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        request = urllib.request.Request(
            GROQ_TRANSCRIBE_URL,
            data=b"".join(parts),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return str(payload.get("text") or "").strip()

    async def transcribe(
        self,
        pcm16: bytes,
        sample_rate: int = MIC_SAMPLE_RATE,
        *,
        generation: int = 0,
    ) -> str:
        import asyncio
        import logging

        if sample_rate != MIC_SAMPLE_RATE:
            raise ValueError(f"STT input must be {MIC_SAMPLE_RATE} Hz")
        if len(pcm16) % 2:
            raise ValueError("STT PCM16 input has a partial sample")
        if self.api_key:
            try:
                text = await asyncio.to_thread(
                    self._post, pcm16_to_wav(bytes(pcm16), sample_rate))
            except Exception as error:  # network, auth, rate limit, bad payload
                logging.getLogger("serena.call").warning(
                    "remote STT unavailable (%s); using the local model", error)
            else:
                if generation in self._cancelled:
                    # He started talking again while this was in flight.
                    return ""
                text = (text or "").strip()
                if text:
                    self.last_backend = self.name
                    return text
        self.last_backend = self.fallback.name
        return await self.fallback.transcribe(
            pcm16, sample_rate, generation=generation)

    async def cancel(self, generation: int) -> bool:
        self._cancelled.add(generation)
        if len(self._cancelled) > 64:
            self._cancelled = set(sorted(self._cancelled)[-32:])
        return await self.fallback.cancel(generation)

    def close(self) -> None:
        self.fallback.close()


def create_stt_backend() -> Any:
    """The best recognizer the keys on this machine allow, backed by the rest.

    One chain, most capable first: Scribe realtime streams during the turn,
    Groq decodes the clip after it, and the local model needs nothing but the
    machine. Each layer holds the next as its fallback, so losing a key or a
    network only costs accuracy, never the call.
    """

    backend = voice_setting(
        "stt", "backend", env="SERENA_CALL_STT_BACKEND", default="auto").lower()
    if backend in {"local", "faster-whisper"}:
        return FasterWhisperWorker()

    def _groq_or_local(required: bool) -> Any:
        if load_groq_key():
            return GroqWhisperWorker()
        if required:
            raise RuntimeError(
                "SERENA_CALL_STT_BACKEND asked for hosted recognition but no "
                "GROQ_API_KEY is set (env or ~/.config/serena/groq.env)")
        return FasterWhisperWorker()

    if backend in {"groq", "remote"}:
        return _groq_or_local(True)
    if backend in {"scribe", "elevenlabs", "realtime"}:
        if not load_elevenlabs_key():
            raise RuntimeError(
                "SERENA_CALL_STT_BACKEND asked for Scribe realtime but no "
                "ELEVENLABS_API_KEY is set (env or ~/.config/serena/elevenlabs.env)")
        return ScribeRealtimeWorker(fallback=_groq_or_local(False))
    if backend in {"auto", ""}:
        if load_elevenlabs_key():
            return ScribeRealtimeWorker(fallback=_groq_or_local(False))
        return _groq_or_local(False)
    raise RuntimeError(f"unsupported Serena call STT backend {backend!r}")


SCRIBE_REALTIME_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
SCRIBE_MODEL = "scribe_v2_realtime"
# The socket is opened while he is still talking, so the only wait at the end
# of a turn is the commit round trip. A second is generous for a 150 ms model;
# past that the local model is faster than continuing to hope.
SCRIBE_COMMIT_TIMEOUT_SECONDS = 1.2
SCRIBE_CONNECT_TIMEOUT_SECONDS = 4.0


def load_elevenlabs_key() -> str:
    """The key from the environment, or from its own 0600 file."""

    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if key:
        return key
    path = Path(
        os.environ.get("SERENA_CALL_ELEVENLABS_ENV")
        or Path.home() / ".config" / "serena" / "elevenlabs.env"
    ).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in raw.splitlines():
        name, _, value = line.strip().partition("=")
        if name.strip() in {"ELEVENLABS_API_KEY", "XI_API_KEY"}:
            return value.strip().strip("'\"")
    return ""


class _ScribeSession:
    """One utterance's websocket, driven by two small threads.

    Audio is pushed from the mic loop, which must never block, so `feed` only
    ever touches a queue. A sender thread drains it and a reader thread keeps
    the newest partial and committed transcripts. Every failure collapses to
    "no text", which the caller reads as "use the local model".
    """

    def __init__(self, url: str, headers: list[str], sample_rate: int) -> None:
        import queue
        import threading

        self.url = url
        self.headers = headers
        self.sample_rate = sample_rate
        self.outbox: queue.Queue[bytes | None] = queue.Queue(maxsize=200)
        self.partial = ""
        self.committed = ""
        self.failed = False
        self._socket: Any = None
        self._lock = threading.Lock()
        self._committed_event = threading.Event()
        self._closed = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> bool:
        import threading

        try:
            import websocket
        except ImportError:
            self.failed = True
            return False
        try:
            self._socket = websocket.create_connection(
                self.url, header=self.headers,
                timeout=SCRIBE_CONNECT_TIMEOUT_SECONDS)
        except Exception:
            self.failed = True
            return False
        for target, name in ((self._send_loop, "scribe-send"),
                             (self._read_loop, "scribe-read")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        return True

    def feed(self, pcm: bytes) -> None:
        import queue

        if self.failed or self._closed.is_set():
            return
        try:
            self.outbox.put_nowait(bytes(pcm))
        except queue.Full:
            # Falling behind the mic is a lost cause for this turn; the local
            # model still has the whole utterance buffered parent-side.
            self.failed = True

    def _chunk(self, pcm: bytes, commit: bool) -> str:
        import json

        return json.dumps({
            "message_type": "input_audio_chunk",
            "audio_base_64": base64.b64encode(pcm).decode("ascii"),
            "sample_rate": self.sample_rate,
            "commit": commit,
        })

    def _send_loop(self) -> None:
        import queue

        while not self._closed.is_set():
            try:
                pcm = self.outbox.get(timeout=0.2)
            except queue.Empty:
                continue
            commit = pcm is None
            try:
                with self._lock:
                    if self._socket is None:
                        return
                    self._socket.send(self._chunk(pcm or b"", commit))
            except Exception:
                self.failed = True
                return
            if commit:
                return

    def _read_loop(self) -> None:
        import json

        while not self._closed.is_set():
            try:
                raw = self._socket.recv()
            except Exception:
                if not self._committed_event.is_set():
                    self.failed = True
                self._committed_event.set()
                return
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            kind = str(message.get("message_type") or "")
            text = str(message.get("text") or "")
            if kind == "partial_transcript":
                self.partial = text
            elif kind == "committed_transcript":
                # A turn can commit in pieces, so they accumulate.
                self.committed = f"{self.committed} {text}".strip()
                self._committed_event.set()

    def commit(self, timeout: float) -> str:
        """Ask for the rest of the transcript and wait briefly for it."""

        if self.failed:
            return ""
        self._committed_event.clear()
        with contextlib.suppress(Exception):
            self.outbox.put_nowait(None)
        self._committed_event.wait(timeout)
        return (self.committed or self.partial).strip()

    def close(self) -> None:
        self._closed.set()
        self._committed_event.set()
        with self._lock:
            socket, self._socket = self._socket, None
        if socket is not None:
            with contextlib.suppress(Exception):
                socket.close()


class ScribeRealtimeWorker:
    """ElevenLabs Scribe v2 Realtime: transcribed while he is still talking.

    The cascade used to wait for silence, then cut a clip, then upload it, then
    decode it, and only then think. Streaming removes the middle three: the
    socket is open during the turn and the transcript is essentially ready when
    he stops. `keyterms` biases the decoder towards the names he actually says,
    which is the failure the local model could not fix with a prompt hint.

    Everything degrades in one direction. No key, no socket, a slow commit or
    an empty transcript all fall through to `fallback`, which is a hosted or
    local Whisper holding the same utterance parent-side.
    """

    name = "elevenlabs-scribe-v2-realtime"
    execution = "remote"
    model_source = "hosted"
    supports_streaming = True

    def __init__(
        self,
        *,
        fallback: Any = None,
        api_key: str | None = None,
        model: str | None = None,
        keyterms: Sequence[str] | None = None,
    ) -> None:
        self.fallback = fallback if fallback is not None else FasterWhisperWorker()
        self.api_key = api_key if api_key is not None else load_elevenlabs_key()
        self.model = model or os.environ.get("SERENA_CALL_SCRIBE_MODEL") or SCRIBE_MODEL
        self.keyterms = list(keyterms) if keyterms is not None else load_keyterms()
        self.last_backend = ""
        self._sessions: dict[int, _ScribeSession] = {}
        self._streamed: dict[int, str] = {}

    @property
    def warmed(self) -> bool:
        return self.fallback.warmed

    async def warm(self) -> None:
        await self.fallback.warm()

    def _url(self) -> str:
        import urllib.parse

        query = [
            ("model_id", self.model),
            ("audio_format", f"pcm_{MIC_SAMPLE_RATE}"),
            ("language_code", "en"),
            # Our own VAD already owns turn taking, including barge-in, so the
            # segment ends when it says so and not on a second opinion.
            ("commit_strategy", "manual"),
        ]
        query.extend(("keyterms", term) for term in self.keyterms[:100])
        return f"{SCRIBE_REALTIME_URL}?{urllib.parse.urlencode(query)}"

    def _session(self, generation: int) -> _ScribeSession | None:
        session = self._sessions.get(generation)
        if session is not None:
            return None if session.failed else session
        if not self.api_key:
            return None
        session = _ScribeSession(
            self._url(), [f"xi-api-key: {self.api_key}"], MIC_SAMPLE_RATE)
        self._sessions[generation] = session
        if not session.start():
            log_stt.warning("Scribe realtime did not connect; local model owns this turn")
            return None
        return session

    def feed(self, pcm: bytes, generation: int = 0) -> None:
        """Called from the mic loop for every frame. Never blocks, never raises."""

        with contextlib.suppress(Exception):
            session = self._session(generation)
            if session is not None:
                session.feed(pcm)

    def partial_text(self, generation: int = 0) -> str:
        """What Scribe has heard so far this turn, at no cost and no commit.

        This is what makes the speculative decode unnecessary: the orchestrator
        used to spend a whole extra transcription to guess whether he had
        finished his sentence, and streaming already knows.
        """

        session = self._sessions.get(generation)
        if session is None or session.failed:
            return ""
        return (session.committed or session.partial).strip()

    async def finish_stream(self, generation: int = 0) -> str:
        """Commit the open segment and return what Scribe heard, or ""."""

        import asyncio

        session = self._sessions.get(generation)
        if session is None or session.failed:
            return ""
        text = await asyncio.to_thread(session.commit, SCRIBE_COMMIT_TIMEOUT_SECONDS)
        self.reset(generation)
        if text:
            self._streamed[generation] = text
            self.last_backend = self.name
        return text

    def reset(self, generation: int = 0) -> None:
        session = self._sessions.pop(generation, None)
        if session is not None:
            session.close()

    async def transcribe(
        self,
        pcm16: bytes,
        sample_rate: int = MIC_SAMPLE_RATE,
        *,
        generation: int = 0,
    ) -> str:
        streamed = self._streamed.pop(generation, "")
        if streamed:
            return streamed
        text = await self.finish_stream(generation)
        if text:
            self._streamed.pop(generation, None)
            return text
        self.last_backend = self.fallback.name
        return await self.fallback.transcribe(pcm16, sample_rate, generation=generation)

    async def cancel(self, generation: int) -> bool:
        self.reset(generation)
        self._streamed.pop(generation, None)
        return await self.fallback.cancel(generation)

    def close(self) -> None:
        for generation in list(self._sessions):
            self.reset(generation)
        self.fallback.close()
