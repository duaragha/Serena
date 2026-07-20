"""Install and verify a portable ``hey serena`` openWakeWord export.

Training happens in the official openWakeWord Colab workflow. This module owns
only the trusted desk-host boundary: copy an exported ONNX model into an
immutable release, load it through the production scorer, seal its provenance,
and atomically promote model and provenance together.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import importlib.metadata
import json
import os
import secrets
import shutil
import stat
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from voice.call.wakeword import (
    FRAME_SAMPLES,
    OpenWakeWordScorer,
    WakeWordModelSpec,
    sha256_file,
)

MODEL_NAME = "hey_serena"
TARGET_PHRASE = "hey serena"
MODEL_FILENAME = f"{MODEL_NAME}.onnx"
PROVENANCE_FILENAME = f"{MODEL_NAME}.provenance.json"
DEFAULT_MODELS_DIR = Path("~/.config/serena/models").expanduser()
DEFAULT_RUNTIME_MODEL = DEFAULT_MODELS_DIR / MODEL_FILENAME
DEFAULT_RUNTIME_PROVENANCE = DEFAULT_MODELS_DIR / PROVENANCE_FILENAME
DEFAULT_PROVENANCE_ATTESTATION_KEY = (
    DEFAULT_MODELS_DIR / "provenance-attestation.key"
)
MAX_MODEL_BYTES = 100 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024

RuntimeValidator = Callable[[Path], dict[str, Any]]


def _canonical_payload(value: dict[str, Any]) -> bytes:
    payload = dict(value)
    payload.pop("installation_hmac_sha256", None)
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sign_installation_provenance(value: dict[str, Any], key: bytes) -> str:
    return hmac.new(key, _canonical_payload(value), hashlib.sha256).hexdigest()


def _private_directory(path: Path) -> Path:
    path = path.expanduser().absolute()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError(f"runtime directory cannot be a symlink: {path}")
    metadata = path.stat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise ValueError(f"runtime directory is not owned by this user: {path}")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o077:
        path.chmod(0o700)
    return path


def _read_key(path: Path) -> bytes:
    path = path.expanduser().absolute()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise ValueError(f"provenance attestation key is missing: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise ValueError(
                "provenance attestation key must be singly linked, owned by this "
                "user, and mode 0600"
            )
        key = os.read(descriptor, 33)
        if len(key) != 32:
            raise ValueError("provenance attestation key must contain exactly 32 bytes")
        return key
    finally:
        os.close(descriptor)


def _load_or_create_key(path: Path) -> tuple[Path, bytes]:
    path = path.expanduser().absolute()
    _private_directory(path.parent)
    if path.exists() or path.is_symlink():
        return path, _read_key(path)
    descriptor = os.open(
        path,
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    key = secrets.token_bytes(32)
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(key)
        output.flush()
        os.fsync(output.fileno())
    return path, key


def verify_installation_attestation(value: dict[str, Any], key_path: Path) -> None:
    signature = value.get("installation_hmac_sha256")
    if not isinstance(signature, str) or len(signature) != 64:
        raise ValueError("model provenance has no installation attestation")
    key = _read_key(key_path)
    if value.get("installation_attestation_key_id") != hashlib.sha256(key).hexdigest():
        raise ValueError("model provenance names another installation key")
    expected = _sign_installation_provenance(value, key)
    if not hmac.compare_digest(signature, expected):
        raise ValueError("model provenance installation attestation does not match")


def _copy_regular_file(source: Path, target: Path) -> str:
    source = source.expanduser().absolute()
    if source.suffix.lower() != ".onnx":
        raise ValueError("wake-word export must be an ONNX file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise ValueError(f"cannot open wake-word export safely: {exc}") from exc
    digest = hashlib.sha256()
    written = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("wake-word export must be a singly linked regular file")
        if before.st_size <= 0 or before.st_size > MAX_MODEL_BYTES:
            raise ValueError("wake-word export has an invalid size")
        output_descriptor = os.open(
            target,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        with os.fdopen(output_descriptor, "wb") as output:
            while chunk := os.read(descriptor, 1024 * 1024):
                written += len(chunk)
                if written > MAX_MODEL_BYTES:
                    raise ValueError("wake-word export exceeds the size limit")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("wake-word export changed while it was being installed")
        if written != before.st_size:
            raise ValueError("wake-word export was not copied completely")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _load_training_metadata(path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if path is None:
        return None, None
    path = path.expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise ValueError("training metadata must be a regular JSON file")
    if path.stat().st_size > MAX_METADATA_BYTES:
        raise ValueError("training metadata exceeds the size limit")
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("training metadata is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("training metadata must be a JSON object")
    return value, hashlib.sha256(raw).hexdigest()


def _openwakeword_version() -> str:
    try:
        return importlib.metadata.version("openwakeword")
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def validate_runtime_model(model: Path) -> dict[str, Any]:
    scorer = OpenWakeWordScorer(
        WakeWordModelSpec(model_path=model, score_label=MODEL_NAME)
    )
    silence = np.zeros(FRAME_SAMPLES, dtype=np.int16)
    scores = [scorer.score_frame(silence) for _ in range(5)]
    scorer.reset()
    return {
        "score_label": scorer.score_label,
        "silence_frames": len(scores),
        "silence_score_max": max(scores),
        "openwakeword_version": _openwakeword_version(),
        "validated_at": time.time(),
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, sort_keys=True, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def _replace_symlink(path: Path, target: str) -> None:
    if path.exists() and not path.is_symlink():
        raise ValueError(f"refusing to replace unmanaged runtime path: {path}")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    temporary.symlink_to(target)
    os.replace(temporary, path)


def _require_managed_slot(path: Path) -> None:
    if path.exists() and not path.is_symlink():
        raise ValueError(f"refusing to replace unmanaged runtime path: {path}")


def install_exported_model(
    source: Path,
    *,
    models_dir: Path = DEFAULT_MODELS_DIR,
    attestation_key: Path | None = None,
    source_kind: str = "openwakeword-colab",
    training_metadata: Path | None = None,
    validator: RuntimeValidator = validate_runtime_model,
) -> dict[str, Any]:
    if source_kind != "openwakeword-colab":
        raise ValueError("unsupported wake-word export source")
    models_dir = _private_directory(models_dir)
    releases = _private_directory(models_dir / ".wakeword-releases")
    key_path, key = _load_or_create_key(
        attestation_key or models_dir / DEFAULT_PROVENANCE_ATTESTATION_KEY.name
    )
    metadata, metadata_sha256 = _load_training_metadata(training_metadata)
    model_alias = models_dir / MODEL_FILENAME
    provenance_alias = models_dir / PROVENANCE_FILENAME
    current = models_dir / ".wakeword-current"
    for managed_path in (model_alias, provenance_alias, current):
        _require_managed_slot(managed_path)
    staging = releases / f".staging-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    staged_model = staging / MODEL_FILENAME
    staged_provenance = staging / PROVENANCE_FILENAME
    try:
        model_sha256 = _copy_regular_file(source, staged_model)
        runtime_validation = validator(staged_model)
        if runtime_validation.get("score_label") != MODEL_NAME:
            raise ValueError("runtime validation did not expose the hey_serena score label")
        release_name = f"{time.time_ns()}-{model_sha256[:16]}"
        release = releases / release_name
        installed_model = release / MODEL_FILENAME
        provenance: dict[str, Any] = {
            "schema": 1,
            "target_phrase": TARGET_PHRASE,
            "model_name": MODEL_NAME,
            "model_sha256": model_sha256,
            "installed_model": str(installed_model),
            "validated_for_runtime": True,
            "runtime_validation": runtime_validation,
            "source": source_kind,
            "source_model_sha256": model_sha256,
            "source_filename": source.name,
            "imported_at": time.time(),
            "installation_attestation_key_id": hashlib.sha256(key).hexdigest(),
        }
        if metadata is not None:
            provenance["training_metadata"] = metadata
            provenance["training_metadata_sha256"] = metadata_sha256
        provenance["installation_hmac_sha256"] = _sign_installation_provenance(
            provenance, key
        )
        _write_json(staged_provenance, provenance)
        staged_model.chmod(0o400)
        staged_provenance.chmod(0o400)
        staging.chmod(0o500)
        staging.rename(release)

        verify_installed_model(
            model=release / MODEL_FILENAME,
            provenance=release / PROVENANCE_FILENAME,
            attestation_key=key_path,
        )
        _replace_symlink(model_alias, f"{current.name}/{MODEL_FILENAME}")
        _replace_symlink(provenance_alias, f"{current.name}/{PROVENANCE_FILENAME}")
        _replace_symlink(current, f".wakeword-releases/{release_name}")
        verify_installed_model(
            model=model_alias,
            provenance=provenance_alias,
            attestation_key=key_path,
        )
        return {
            "ok": True,
            "model": str(model_alias),
            "provenance": str(provenance_alias),
            "release": str(release),
            "model_sha256": model_sha256,
            "source": source_kind,
            "score_label": runtime_validation["score_label"],
        }
    except Exception:
        if staging.exists():
            with contextlib.suppress(OSError):
                staging.chmod(0o700)
            shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_installed_model(
    *,
    model: Path = DEFAULT_RUNTIME_MODEL,
    provenance: Path = DEFAULT_RUNTIME_PROVENANCE,
    attestation_key: Path = DEFAULT_PROVENANCE_ATTESTATION_KEY,
) -> dict[str, Any]:
    model = model.expanduser().absolute()
    provenance = provenance.expanduser().absolute()
    if not model.is_file() or not provenance.is_file():
        raise ValueError("installed wake-word model or provenance is missing")
    try:
        value = json.loads(provenance.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("installed wake-word provenance is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("installed wake-word provenance must be a JSON object")
    verify_installation_attestation(value, attestation_key)
    expected = {
        "target_phrase": TARGET_PHRASE,
        "model_name": MODEL_NAME,
        "model_sha256": sha256_file(model),
        "installed_model": str(model.resolve()),
        "validated_for_runtime": True,
    }
    for key, wanted in expected.items():
        if value.get(key) != wanted:
            raise ValueError(f"installed wake-word provenance has invalid {key}")
    runtime_validation = value.get("runtime_validation")
    if not isinstance(runtime_validation, dict) or runtime_validation.get(
        "score_label"
    ) != MODEL_NAME:
        raise ValueError("installed model has no successful hey_serena validation")
    return {
        "ok": True,
        "model": str(model),
        "provenance": str(provenance),
        "model_sha256": expected["model_sha256"],
        "source": value.get("source"),
        "score_label": MODEL_NAME,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    install = subparsers.add_parser("install", help="install an exported ONNX model")
    install.add_argument("--model", required=True, type=Path)
    install.add_argument("--models-dir", type=Path, default=DEFAULT_MODELS_DIR)
    install.add_argument("--attestation-key", type=Path)
    install.add_argument(
        "--source",
        choices=("openwakeword-colab",),
        default="openwakeword-colab",
    )
    install.add_argument("--training-metadata", type=Path)
    verify = subparsers.add_parser("verify", help="verify the installed model seal")
    verify.add_argument("--model", type=Path, default=DEFAULT_RUNTIME_MODEL)
    verify.add_argument("--provenance", type=Path, default=DEFAULT_RUNTIME_PROVENANCE)
    verify.add_argument(
        "--attestation-key", type=Path, default=DEFAULT_PROVENANCE_ATTESTATION_KEY
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "install":
            result = install_exported_model(
                args.model,
                models_dir=args.models_dir,
                attestation_key=args.attestation_key,
                source_kind=args.source,
                training_metadata=args.training_metadata,
            )
        else:
            result = verify_installed_model(
                model=args.model,
                provenance=args.provenance,
                attestation_key=args.attestation_key,
            )
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
