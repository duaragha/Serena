from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from voice.call.wakeword import (
    FRAME_SAMPLES,
    OpenWakeWordScorer,
    WakeGate,
    WakeWordConfigurationError,
    WakeWordModelSpec,
    rms_dbfs,
    sha256_file,
)
from voice.call.wakeword_harness import _validated_model_provenance
from voice.call.wakeword_model import (
    _load_or_create_key,
    _sign_installation_provenance,
)


class FakeModel:
    def __init__(self, predictions: list[dict[str, float]], **kwargs: object) -> None:
        self.predictions = iter(predictions)
        self.kwargs = kwargs
        self.reset_count = 0

    def predict(self, _frame: np.ndarray) -> dict[str, float]:
        return next(self.predictions)

    def reset(self) -> None:
        self.reset_count += 1


def test_missing_serena_model_fails_closed_before_factory(tmp_path: Path) -> None:
    called = False

    def factory(**_kwargs: object) -> FakeModel:
        nonlocal called
        called = True
        return FakeModel([])

    with pytest.raises(WakeWordConfigurationError, match="refusing to substitute"):
        OpenWakeWordScorer(
            WakeWordModelSpec(tmp_path / "hey_serena.onnx"),
            model_factory=factory,
        )
    assert called is False


def test_scorer_wires_verifier_to_exact_model_stem(tmp_path: Path) -> None:
    model_path = tmp_path / "hey_serena.onnx"
    verifier_path = tmp_path / "serena.pkl"
    model_path.write_bytes(b"model")
    verifier_path.write_bytes(b"verifier")
    fake = FakeModel([{"hey_serena": 0.72}])

    scorer = OpenWakeWordScorer(
        WakeWordModelSpec(model_path, verifier_path=verifier_path),
        model_factory=lambda **kwargs: _capture(fake, kwargs),
    )
    score = scorer.score_frame(np.zeros(FRAME_SAMPLES, dtype=np.int16))

    assert score == 0.72
    assert scorer.score_label == "hey_serena"
    assert fake.kwargs["custom_verifier_models"] == {
        "hey_serena": str(verifier_path)
    }
    assert fake.kwargs["wakeword_models"] == [str(model_path)]
    assert fake.kwargs["inference_framework"] == "onnx"


def _capture(fake: FakeModel, kwargs: dict[str, object]) -> FakeModel:
    fake.kwargs = kwargs
    return fake


def test_scorer_requires_explicit_label_for_multi_output_model(tmp_path: Path) -> None:
    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"x")
    scorer = OpenWakeWordScorer(
        WakeWordModelSpec(model_path),
        model_factory=lambda **_kwargs: FakeModel([{"one": 0.1, "two": 0.8}]),
    )
    with pytest.raises(WakeWordConfigurationError, match="multiple labels"):
        scorer.score_frame(np.zeros(FRAME_SAMPLES, dtype=np.int16))


def test_scorer_rejects_wrong_pcm_shape_and_dtype(tmp_path: Path) -> None:
    model_path = tmp_path / "hey_serena.onnx"
    model_path.write_bytes(b"x")
    scorer = OpenWakeWordScorer(
        WakeWordModelSpec(model_path),
        model_factory=lambda **_kwargs: FakeModel([{"hey_serena": 0.1}]),
    )
    with pytest.raises(ValueError, match="int16"):
        scorer.score_frame(np.zeros(FRAME_SAMPLES, dtype=np.float32))
    with pytest.raises(ValueError, match="exactly"):
        scorer.score_frame(np.zeros(10, dtype=np.int16))


def test_gate_applies_patience_and_cooldown_without_resetting_model() -> None:
    gate = WakeGate(0.5, patience_frames=2, cooldown_seconds=3.0)
    assert gate.observe(0.7, monotonic_ns=1_000_000_000) is False
    assert gate.observe(0.8, monotonic_ns=1_080_000_000) is True
    assert gate.observe(0.8, monotonic_ns=1_160_000_000) is False
    assert gate.observe(0.8, monotonic_ns=1_240_000_000) is False
    assert gate.observe(0.8, monotonic_ns=4_200_000_000) is False
    assert gate.observe(0.8, monotonic_ns=4_280_000_000) is True


def test_rms_and_hash_helpers_do_not_persist_audio(tmp_path: Path) -> None:
    path = tmp_path / "model.onnx"
    path.write_bytes(b"abc")
    assert sha256_file(path) == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    assert rms_dbfs(np.zeros(FRAME_SAMPLES, dtype=np.int16)) == -120.0
    assert -7.0 < rms_dbfs(np.full(FRAME_SAMPLES, 16_384, dtype=np.int16)) < -5.0


def test_acceptance_freeze_requires_runtime_validated_model_provenance(
    tmp_path: Path,
) -> None:
    model = tmp_path / "hey_serena.onnx"
    model.write_bytes(b"model")
    model_hash = sha256_file(model)
    provenance = tmp_path / "hey_serena.provenance.json"
    key_path, key = _load_or_create_key(tmp_path / "provenance.key")
    value = {
        "target_phrase": "hey serena",
        "model_name": "hey_serena",
        "model_sha256": model_hash,
        "installed_model": str(model.resolve()),
        "validated_for_runtime": True,
        "runtime_validation": {"score_label": "hey_serena"},
        "installation_attestation_key_id": hashlib.sha256(key).hexdigest(),
    }
    value["installation_hmac_sha256"] = _sign_installation_provenance(value, key)
    provenance.write_text(json.dumps(value), encoding="utf-8")

    assert _validated_model_provenance(
        provenance,
        model=model,
        model_hash=model_hash,
        attestation_key=key_path,
    ) == sha256_file(provenance)

    value["validated_for_runtime"] = False
    value["installation_hmac_sha256"] = _sign_installation_provenance(value, key)
    provenance.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(WakeWordConfigurationError, match="validated_for_runtime"):
        _validated_model_provenance(
            provenance,
            model=model,
            model_hash=model_hash,
            attestation_key=key_path,
        )


def test_acceptance_freeze_rejects_provenance_for_another_model(tmp_path: Path) -> None:
    model = tmp_path / "hey_serena.onnx"
    model.write_bytes(b"model")
    key_path, key = _load_or_create_key(tmp_path / "provenance.key")
    provenance = tmp_path / "hey_serena.provenance.json"
    value = {
        "target_phrase": "hey serena",
        "model_name": "hey_serena",
        "model_sha256": "0" * 64,
        "installed_model": str(model.resolve()),
        "validated_for_runtime": True,
        "runtime_validation": {"score_label": "hey_serena"},
        "installation_attestation_key_id": hashlib.sha256(key).hexdigest(),
    }
    value["installation_hmac_sha256"] = _sign_installation_provenance(value, key)
    provenance.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(WakeWordConfigurationError, match="model_sha256"):
        _validated_model_provenance(
            provenance,
            model=model,
            model_hash=sha256_file(model),
            attestation_key=key_path,
        )


def test_acceptance_freeze_rejects_self_asserted_unsigned_provenance(
    tmp_path: Path,
) -> None:
    model = tmp_path / "hey_serena.onnx"
    model.write_bytes(b"arbitrary")
    key_path, _key = _load_or_create_key(tmp_path / "provenance.key")
    provenance = tmp_path / "forged.json"
    provenance.write_text(
        json.dumps(
            {
                "target_phrase": "hey serena",
                "model_name": "hey_serena",
                "model_sha256": sha256_file(model),
                "installed_model": str(model.resolve()),
                "validated_for_runtime": True,
                "runtime_validation": {"score_label": "hey_serena"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(WakeWordConfigurationError, match="attestation"):
        _validated_model_provenance(
            provenance,
            model=model,
            model_hash=sha256_file(model),
            attestation_key=key_path,
        )
