"""One local faster-whisper worker for Serena calls."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path

from .process_worker import CancellableModelProcess
from .protocol import MIC_SAMPLE_RATE

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


def load_whisper_hotwords(path: str | Path | None = None) -> str:
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
    return " ".join(terms)[:2_000]


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


def create_stt_backend() -> FasterWhisperWorker | GroqWhisperWorker:
    """Hosted recognition when a key is present, the local model otherwise."""

    backend = os.environ.get("SERENA_CALL_STT_BACKEND", "auto").strip().lower()
    if backend in {"local", "faster-whisper"}:
        return FasterWhisperWorker()
    if backend in {"auto", "", "remote", "groq"}:
        if load_groq_key():
            return GroqWhisperWorker()
        if backend in {"remote", "groq"}:
            raise RuntimeError(
                "SERENA_CALL_STT_BACKEND asked for hosted recognition but no "
                "GROQ_API_KEY is set (env or ~/.config/serena/groq.env)")
        return FasterWhisperWorker()
    raise RuntimeError(f"unsupported Serena call STT backend {backend!r}")
