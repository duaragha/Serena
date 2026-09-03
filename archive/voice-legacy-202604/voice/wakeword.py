"""Wake word detection module.

Uses openwakeword to detect a wake phrase in a continuous audio stream.
Currently uses the built-in "hey_jarvis" model as a placeholder until
a custom "Hey Serena" model is trained (Phase 5).
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from openwakeword.model import Model as OWWModel

from serena.config import WakeWordConfig

logger = logging.getLogger(__name__)

# Default placeholder model -- ships with openwakeword
_DEFAULT_MODEL = "hey_jarvis"


class WakeWordDetector:
    """Detects a wake word in streaming audio chunks.

    Wraps openwakeword to process 16kHz int16 audio chunks and report
    when the wake phrase confidence exceeds the configured threshold.

    Usage:
        detector = WakeWordDetector(config)
        # In audio callback or loop:
        if detector.process(chunk):
            print("Wake word detected!")
    """

    def __init__(self, config: WakeWordConfig | None = None) -> None:
        config = config or WakeWordConfig()
        self._threshold = config.threshold

        model_path = config.model_path
        custom_model = Path(model_path).exists()

        if custom_model:
            # Load a custom-trained model from disk
            self._model = OWWModel(
                wakeword_models=[model_path],
                inference_framework="onnx",
            )
            self._model_name = Path(model_path).stem
            logger.info("Loaded custom wake word model: %s", model_path)
        else:
            # Fall back to a built-in pretrained model
            self._model = OWWModel(
                wakeword_models=[_DEFAULT_MODEL],
                inference_framework="onnx",
            )
            self._model_name = _DEFAULT_MODEL
            logger.info(
                "Custom model not found at %s -- using built-in '%s' as placeholder",
                model_path,
                _DEFAULT_MODEL,
            )

        logger.info("Wake word threshold: %.2f", self._threshold)

    @property
    def model_name(self) -> str:
        """The name of the active wake word model."""
        return self._model_name

    @property
    def threshold(self) -> float:
        return self._threshold

    @threshold.setter
    def threshold(self, value: float) -> None:
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"Threshold must be between 0 and 1, got {value}")
        self._threshold = value

    def process(self, chunk: np.ndarray) -> bool:
        """Process an audio chunk and return True if the wake word is detected.

        Args:
            chunk: Audio data as a numpy int16 array. Ideally 1280 samples
                   (80ms at 16kHz), which is openwakeword's native frame size.

        Returns:
            True if wake word confidence exceeds the threshold.
        """
        scores = self._model.predict(chunk)
        score = scores.get(self._model_name, 0.0)

        if score >= self._threshold:
            logger.info(
                "Wake word '%s' detected (score=%.3f, threshold=%.2f)",
                self._model_name,
                score,
                self._threshold,
            )
            self.reset()
            return True

        return False

    def reset(self) -> None:
        """Reset internal buffers. Call after detection to avoid re-triggers."""
        self._model.reset()
