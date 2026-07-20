"""Fail-closed openWakeWord scoring primitives for Serena's desk client.

This module deliberately contains no microphone or persistence code. It keeps the
model boundary small enough to test without audio hardware and never substitutes a
different wake phrase when the requested Serena model is absent.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 1_280
FRAME_MILLISECONDS = 80


class WakeWordConfigurationError(RuntimeError):
    """Raised when the configured wake-word model cannot be used safely."""


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return a streaming SHA-256 digest for a model or verifier file."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class WakeWordModelSpec:
    """Configuration for one explicit openWakeWord model."""

    model_path: Path
    score_label: str | None = None
    verifier_path: Path | None = None
    verifier_threshold: float = 0.1
    vad_threshold: float = 0.0
    enable_speex_noise_suppression: bool = False

    @property
    def model_name(self) -> str:
        return self.model_path.stem

    @property
    def inference_framework(self) -> str:
        suffix = self.model_path.suffix.lower()
        if suffix == ".onnx":
            return "onnx"
        raise WakeWordConfigurationError(
            f"wake-word model must be an ONNX file: {self.model_path}"
        )

    def validate(self) -> None:
        if not self.model_path.is_file():
            raise WakeWordConfigurationError(
                "hey serena model is missing; refusing to substitute another wake phrase: "
                f"{self.model_path}"
            )
        if self.verifier_path is not None and not self.verifier_path.is_file():
            raise WakeWordConfigurationError(
                f"configured wake-word verifier is missing: {self.verifier_path}"
            )
        if not 0.0 <= self.verifier_threshold <= 1.0:
            raise WakeWordConfigurationError("verifier threshold must be between 0 and 1")
        if not 0.0 <= self.vad_threshold <= 1.0:
            raise WakeWordConfigurationError("VAD threshold must be between 0 and 1")
        _ = self.inference_framework


class OpenWakeWordScorer:
    """Return raw frame scores from one explicit openWakeWord model.

    ``model_factory`` exists for deterministic tests. Production callers leave it
    unset, which lazily imports openWakeWord only after the model files validate.
    """

    def __init__(
        self,
        spec: WakeWordModelSpec,
        *,
        model_factory: Callable[..., Any] | None = None,
    ) -> None:
        spec.validate()
        if model_factory is None:
            try:
                from openwakeword.model import Model as model_factory
            except ImportError as exc:
                raise WakeWordConfigurationError(
                    "openwakeword is not installed in this interpreter"
                ) from exc

        kwargs: dict[str, Any] = {
            "wakeword_models": [str(spec.model_path)],
            "inference_framework": spec.inference_framework,
            "vad_threshold": spec.vad_threshold,
            "enable_speex_noise_suppression": spec.enable_speex_noise_suppression,
        }
        if spec.verifier_path is not None:
            kwargs["custom_verifier_models"] = {
                spec.model_name: str(spec.verifier_path)
            }
            kwargs["custom_verifier_threshold"] = spec.verifier_threshold

        try:
            self._model = model_factory(**kwargs)
        except Exception as exc:
            raise WakeWordConfigurationError(
                f"could not load hey serena model {spec.model_path}: {exc}"
            ) from exc
        self.spec = spec
        self._resolved_label: str | None = spec.score_label

    @property
    def score_label(self) -> str | None:
        return self._resolved_label

    def score_frame(self, frame: np.ndarray) -> float:
        """Score one native 80 ms, 16 kHz, mono PCM16 frame."""

        if not isinstance(frame, np.ndarray):
            raise TypeError("wake-word frame must be a numpy array")
        if frame.dtype != np.int16:
            raise ValueError(f"wake-word frame must be int16, got {frame.dtype}")
        if frame.ndim != 1 or frame.shape[0] != FRAME_SAMPLES:
            raise ValueError(
                f"wake-word frame must contain exactly {FRAME_SAMPLES} mono samples"
            )

        predictions = self._model.predict(frame)
        if isinstance(predictions, tuple):
            predictions = predictions[0]
        if not isinstance(predictions, dict) or not predictions:
            raise WakeWordConfigurationError("openWakeWord returned no prediction scores")

        label = self._resolve_label(predictions)
        try:
            score = float(predictions[label])
        except (TypeError, ValueError) as exc:
            raise WakeWordConfigurationError(
                f"openWakeWord returned a non-numeric score for {label!r}"
            ) from exc
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise WakeWordConfigurationError(
                f"openWakeWord returned an invalid score for {label!r}: {score}"
            )
        return score

    def _resolve_label(self, predictions: dict[str, Any]) -> str:
        if self._resolved_label is not None:
            if self._resolved_label not in predictions:
                raise WakeWordConfigurationError(
                    f"configured score label {self._resolved_label!r} is absent; "
                    f"model returned {sorted(predictions)}"
                )
            return self._resolved_label
        if self.spec.model_name in predictions:
            self._resolved_label = self.spec.model_name
        elif len(predictions) == 1:
            self._resolved_label = next(iter(predictions))
        else:
            raise WakeWordConfigurationError(
                "wake-word model exposes multiple labels; set --score-label explicitly: "
                f"{sorted(predictions)}"
            )
        return self._resolved_label

    def reset(self) -> None:
        self._model.reset()


@dataclass(slots=True)
class WakeGate:
    """Apply threshold, patience, and cooldown without mutating model state."""

    threshold: float
    patience_frames: int = 1
    cooldown_seconds: float = 3.0
    _consecutive: int = 0
    _last_trigger_ns: int | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError("wake threshold must be between 0 and 1")
        if self.patience_frames < 1:
            raise ValueError("patience_frames must be at least 1")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds cannot be negative")

    def observe(self, score: float, *, monotonic_ns: int | None = None) -> bool:
        now_ns = time.monotonic_ns() if monotonic_ns is None else monotonic_ns
        if score >= self.threshold:
            self._consecutive += 1
        else:
            self._consecutive = 0
            return False

        if self._consecutive < self.patience_frames:
            return False
        self._consecutive = 0
        if self._last_trigger_ns is not None:
            elapsed = (now_ns - self._last_trigger_ns) / 1_000_000_000
            if elapsed < self.cooldown_seconds:
                return False
        self._last_trigger_ns = now_ns
        return True

    def reset(self) -> None:
        self._consecutive = 0
        self._last_trigger_ns = None


def rms_dbfs(frame: np.ndarray) -> float:
    """Return PCM16 RMS level in dBFS without retaining audio."""

    if frame.size == 0:
        return -120.0
    normalized = frame.astype(np.float64) / 32768.0
    rms = float(np.sqrt(np.mean(np.square(normalized))))
    if rms <= 1e-12:
        return -120.0
    return max(-120.0, 20.0 * math.log10(rms))
