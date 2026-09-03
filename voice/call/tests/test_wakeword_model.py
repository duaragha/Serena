from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from voice.call.wakeword_model import (
    install_exported_model,
    verify_installed_model,
)


def _validator(_model: Path) -> dict[str, object]:
    return {
        "score_label": "hey_serena",
        "silence_frames": 5,
        "silence_score_max": 0.01,
        "openwakeword_version": "test",
        "validated_at": 1.0,
    }


def _install(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "colab-export.onnx"
    source.write_bytes(b"portable-openwakeword-model")
    models = tmp_path / "models"
    result = install_exported_model(source, models_dir=models, validator=_validator)
    assert result["ok"] is True
    return (
        models / "hey_serena.onnx",
        models / "hey_serena.provenance.json",
        models / "provenance-attestation.key",
    )


def test_install_promotes_one_private_release_and_verifies(tmp_path: Path) -> None:
    model, provenance, key = _install(tmp_path)

    assert model.is_symlink()
    assert provenance.is_symlink()
    assert os.readlink(model) == ".wakeword-current/hey_serena.onnx"
    assert os.readlink(provenance) == (
        ".wakeword-current/hey_serena.provenance.json"
    )
    assert model.resolve().parent == provenance.resolve().parent
    assert model.read_bytes() == b"portable-openwakeword-model"
    assert (key.stat().st_mode & 0o777) == 0o600
    assert (model.resolve().stat().st_mode & 0o777) == 0o400
    assert (model.resolve().parent.stat().st_mode & 0o777) == 0o500

    result = verify_installed_model(
        model=model,
        provenance=provenance,
        attestation_key=key,
    )
    assert result["ok"] is True
    assert result["source"] == "openwakeword-colab"


def test_install_preserves_optional_training_metadata(tmp_path: Path) -> None:
    source = tmp_path / "model.onnx"
    source.write_bytes(b"model")
    metadata = tmp_path / "training.json"
    metadata.write_text(json.dumps({"colab": "official", "phrase": "hey serena"}))
    models = tmp_path / "models"

    install_exported_model(
        source,
        models_dir=models,
        training_metadata=metadata,
        validator=_validator,
    )

    provenance = json.loads(
        (models / "hey_serena.provenance.json").read_text(encoding="utf-8")
    )
    assert provenance["training_metadata"]["colab"] == "official"
    assert len(provenance["training_metadata_sha256"]) == 64


def test_tampered_provenance_fails_attestation(tmp_path: Path) -> None:
    model, provenance, key = _install(tmp_path)
    release_provenance = provenance.resolve()
    release_provenance.chmod(0o600)
    value = json.loads(release_provenance.read_text(encoding="utf-8"))
    value["source"] = "forged"
    release_provenance.write_text(json.dumps(value), encoding="utf-8")
    release_provenance.chmod(0o400)

    with pytest.raises(ValueError, match="attestation does not match"):
        verify_installed_model(
            model=model,
            provenance=provenance,
            attestation_key=key,
        )


def test_symlink_source_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real.onnx"
    real.write_bytes(b"model")
    source = tmp_path / "download.onnx"
    source.symlink_to(real)

    with pytest.raises(ValueError, match="cannot open wake-word export safely"):
        install_exported_model(
            source,
            models_dir=tmp_path / "models",
            validator=_validator,
        )


def test_unmanaged_runtime_alias_is_not_replaced(tmp_path: Path) -> None:
    source = tmp_path / "model.onnx"
    source.write_bytes(b"model")
    models = tmp_path / "models"
    models.mkdir()
    (models / "hey_serena.onnx").write_bytes(b"unmanaged")

    with pytest.raises(ValueError, match="unmanaged runtime path"):
        install_exported_model(source, models_dir=models, validator=_validator)
