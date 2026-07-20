"""One local faster-whisper worker for Serena calls."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path

from .process_worker import CancellableModelProcess
from .protocol import MIC_SAMPLE_RATE

DEFAULT_VOCABULARY_PATH = Path(__file__).resolve().parents[1] / "vocabulary.txt"


def load_whisper_hotwords(path: str | Path | None = None) -> str:
    vocabulary_path = Path(
        path
        or os.environ.get("SERENA_CALL_VOCABULARY")
        or DEFAULT_VOCABULARY_PATH
    ).expanduser()
    terms: list[str] = []
    try:
        lines = vocabulary_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        term = line.split("#", 1)[0].strip()
        if term and term not in terms:
            terms.append(term)
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
        self.device = select_whisper_device()
        self._worker = CancellableModelProcess(
            "stt",
            {
                "model": self.model_ref,
                "device": self.device.device,
                "compute_type": self.device.compute_type,
                "sample_rate": MIC_SAMPLE_RATE,
                "hotwords": self.hotwords,
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
