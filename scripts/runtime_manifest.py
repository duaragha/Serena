"""Load and validate Serena's machine-readable runtime manifest."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "config" / "runtime-manifest.json"


class ManifestError(ValueError):
    """Raised when the runtime manifest is malformed."""


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot load runtime manifest {path}: {exc}") from exc

    if data.get("schema_version") != 1:
        raise ManifestError("runtime manifest schema_version must be 1")

    required_lists = (
        "portable_source",
        "private_repository_paths",
        "runtime_paths",
        "session_paths",
        "auth_paths",
        "model_paths",
        "rebuildable_paths",
        "sensitive_names",
        "transient_names",
        "required_commands",
        "required_python_imports",
    )
    for key in required_lists:
        value = data.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ManifestError(f"runtime manifest {key} must be a list of strings")

    services = data.get("services")
    if not isinstance(services, dict):
        raise ManifestError("runtime manifest services must be an object")
    for key, value in services.items():
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ManifestError(f"runtime manifest services.{key} must be a list of strings")

    return data


def expand_path(value: str, repo_root: Path = REPO_ROOT) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else repo_root / path


def service_names(manifest: dict[str, Any]) -> list[str]:
    names: set[str] = set()
    for group in manifest["services"].values():
        names.update(group)
    return sorted(names)
