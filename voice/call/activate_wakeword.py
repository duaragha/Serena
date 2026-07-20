"""Promote one hey-serena ONNX export and start the frozen desk runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from .wakeword_harness import DEFAULT_ACCEPTANCE_MANIFEST
from .wakeword_model import (
    DEFAULT_RUNTIME_MODEL,
    DEFAULT_RUNTIME_PROVENANCE,
    install_exported_model,
    verify_installed_model,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEMD_SOURCE = REPO_ROOT / "systemd"
SYSTEMD_TARGET = Path.home() / ".config" / "systemd" / "user"
OBSERVATION_DB = Path.home() / ".local" / "state" / "serena" / "wakeword-acceptance.sqlite3"
CHAT_TOKEN = Path.home() / ".config" / "serena" / "chat_token"
DEFAULT_THRESHOLD = 0.55
OPENWAKEWORD_FEATURE_MODELS = {
    "embedding_model.onnx": {
        "url": (
            "https://github.com/dscripka/openWakeWord/releases/download/"
            "v0.5.1/embedding_model.onnx"
        ),
        "sha256": "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f",
    },
    "melspectrogram.onnx": {
        "url": (
            "https://github.com/dscripka/openWakeWord/releases/download/"
            "v0.5.1/melspectrogram.onnx"
        ),
        "sha256": "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f",
    },
}
_MAX_FEATURE_MODEL_BYTES = 64 * 1024 * 1024
UNITS = (
    "serena-brain-bridge.service",
    "serena-dot-overlay.service",
    "serena-desk.service",
    "serena-wake-listener.service",
    "serena-work-supervisor.service",
    "serena-wakeword-acceptance.service",
    "serena-wakeword-acceptance-report.service",
    "serena-wakeword-acceptance-report.timer",
)
START_UNITS = (
    "serena-wake-listener.service",
    "serena-work-supervisor.service",
    "serena-wakeword-acceptance.service",
    "serena-wakeword-acceptance-report.timer",
)
NON_BOOT_UNITS = (
    "serena-brain-bridge.service",
    "serena-desk.service",
    "serena-dot-overlay.service",
)


def _openwakeword_models_dir() -> Path:
    try:
        import openwakeword
    except ImportError as exc:
        raise RuntimeError("openwakeword is not installed in the wake runtime") from exc
    package = getattr(openwakeword, "__file__", None)
    if not package:
        raise RuntimeError("openwakeword package location could not be resolved")
    return Path(package).resolve().parent / "resources" / "models"


def _stream_verified_model(
    response: BinaryIO,
    target: Path,
    expected_sha256: str,
) -> str:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(descriptor, "wb") as output:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > _MAX_FEATURE_MODEL_BYTES:
                    raise ValueError(f"{target.name} exceeds the download size limit")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        actual = digest.hexdigest()
        if total == 0 or actual != expected_sha256:
            raise ValueError(f"{target.name} failed its pinned SHA-256 check")
        temporary.chmod(0o444)
        os.replace(temporary, target)
        return actual
    finally:
        temporary.unlink(missing_ok=True)


def ensure_openwakeword_feature_models(
    models_dir: Path | None = None,
    *,
    opener: Callable[..., BinaryIO] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Install the two pinned ONNX feature models needed by custom models."""

    target_dir = Path(models_dir or _openwakeword_models_dir()).expanduser().absolute()
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    installed: dict[str, dict[str, str]] = {}
    for name, metadata in OPENWAKEWORD_FEATURE_MODELS.items():
        target = target_dir / name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError(f"refusing unmanaged openwakeword resource: {target}")
        expected = metadata["sha256"]
        if target.is_file():
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual == expected:
                installed[name] = {"status": "cached", "sha256": actual}
                continue
        try:
            with opener(metadata["url"], timeout=60) as response:
                actual = _stream_verified_model(response, target, expected)
        except (OSError, urllib.error.URLError) as exc:
            raise RuntimeError(f"could not download {name}: {exc}") from exc
        installed[name] = {"status": "installed", "sha256": actual}
    return {"ok": True, "directory": str(target_dir), "models": installed}


def _copy_unit(source: Path, target: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"systemd unit is missing from the repository: {source}")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as input_file, os.fdopen(descriptor, "wb") as output:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def install_units(
    source_dir: Path = SYSTEMD_SOURCE,
    target_dir: Path = SYSTEMD_TARGET,
) -> list[str]:
    installed: list[str] = []
    for name in UNITS:
        _copy_unit(source_dir / name, target_dir / name)
        installed.append(name)
    return installed


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, capture_output=True)


def _service_states(names: tuple[str, ...]) -> dict[str, str]:
    states: dict[str, str] = {}
    for name in names:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", name],
            check=False,
            text=True,
            capture_output=True,
        )
        states[name] = result.stdout.strip() or "unknown"
    return states


def activate(
    model: Path,
    *,
    training_metadata: Path | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    patience_frames: int = 1,
    cooldown_seconds: float = 3.0,
    device: str | None = None,
    start_services: bool = True,
    allow_existing_observation: bool = False,
) -> dict[str, Any]:
    """Install, validate, freeze, and optionally start one production model."""

    if not 0.0 < threshold <= 1.0:
        raise ValueError("threshold must be greater than zero and no more than one")
    if patience_frames < 1:
        raise ValueError("patience frames must be positive")
    if cooldown_seconds < 0:
        raise ValueError("cooldown seconds cannot be negative")
    source = Path(model).expanduser().absolute()
    metadata = Path(training_metadata).expanduser().absolute() if training_metadata else None
    if OBSERVATION_DB.exists() and not allow_existing_observation:
        raise ValueError(
            "a passive observation database already exists; preserve it and rerun with "
            "--allow-existing-observation only if this is the same frozen configuration"
        )

    feature_models = ensure_openwakeword_feature_models()

    installed = install_exported_model(
        source,
        source_kind="openwakeword-colab",
        training_metadata=metadata,
    )
    verified = verify_installed_model()

    doctor = [sys.executable, "-m", "voice.call.wakeword_harness", "doctor"]
    if device is not None:
        doctor.extend(("--device", device))
    _run(doctor)

    freeze = [
        sys.executable,
        "-m",
        "voice.call.wakeword_harness",
        "freeze",
        "--threshold",
        str(threshold),
        "--patience-frames",
        str(patience_frames),
        "--cooldown-seconds",
        str(cooldown_seconds),
        "--model-provenance",
        str(DEFAULT_RUNTIME_PROVENANCE),
    ]
    if device is not None:
        freeze.extend(("--device", device))
    _run(freeze)
    if not DEFAULT_ACCEPTANCE_MANIFEST.is_file():
        raise RuntimeError("wake-word freeze did not create the acceptance manifest")

    units = install_units()
    services: dict[str, str] = {}
    if start_services:
        if not CHAT_TOKEN.is_file() or stat.S_IMODE(CHAT_TOKEN.stat().st_mode) & 0o077:
            raise ValueError("the desk chat token is missing or not private mode 0600")
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "disable", "--now", *NON_BOOT_UNITS])
        _run(["systemctl", "--user", "enable", "--now", *START_UNITS])
        services = _service_states(START_UNITS)
        inactive = [name for name, state in services.items() if state != "active"]
        if inactive:
            raise RuntimeError("wake services did not become active: " + ", ".join(inactive))

    return {
        "ok": True,
        "model": str(DEFAULT_RUNTIME_MODEL),
        "model_sha256": verified["model_sha256"],
        "provenance": str(DEFAULT_RUNTIME_PROVENANCE),
        "manifest": str(DEFAULT_ACCEPTANCE_MANIFEST),
        "threshold": threshold,
        "patience_frames": patience_frames,
        "cooldown_seconds": cooldown_seconds,
        "source_release": installed["release"],
        "feature_models": feature_models,
        "units": units,
        "services": services,
        "passive_observation": (
            "started" if start_services else "staged, services not started"
        ),
        "field_acceptance": "pending real-room observation",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        type=Path,
        default=Path.home() / "Downloads" / "hey_serena.onnx",
    )
    parser.add_argument("--training-metadata", type=Path)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--patience-frames", type=int, default=1)
    parser.add_argument("--cooldown-seconds", type=float, default=3.0)
    parser.add_argument("--device")
    parser.add_argument("--no-start", action="store_true")
    parser.add_argument("--allow-existing-observation", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = activate(
            args.model,
            training_metadata=args.training_metadata,
            threshold=args.threshold,
            patience_frames=args.patience_frames,
            cooldown_seconds=args.cooldown_seconds,
            device=args.device,
            start_services=not args.no_start,
            allow_existing_observation=args.allow_existing_observation,
        )
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
