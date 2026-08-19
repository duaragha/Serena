"""Durable model preference for coding jobs accepted after the selection."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from core.serena_policy import (
    SerenaPolicyError,
    load_policy,
    model_options,
    normalise_model,
)

AUTO_MODEL = "auto"
TERRA_MODEL = "gpt-5.6-terra"
CODEX_MODEL = "gpt-5.6-sol"
SONNET_MODEL = "claude-sonnet-5"
CLAUDE_MODEL = "claude-opus-5"
CODING_MODELS = (
    AUTO_MODEL,
    TERRA_MODEL,
    CODEX_MODEL,
    SONNET_MODEL,
    CLAUDE_MODEL,
)
DEFAULT_PREFERENCE_PATH = (
    Path.home() / ".local" / "state" / "serena" / "coding-model.json"
)


def preference_path() -> Path:
    configured = os.environ.get("SERENA_CODING_MODEL_PATH", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_PREFERENCE_PATH


def normalise_coding_model(value: object, *, strict: bool = False) -> str:
    try:
        model = normalise_model(value, strict=strict)
    except SerenaPolicyError as exc:
        raise ValueError(f"unsupported coding model: {value}") from exc
    if model in CODING_MODELS:
        return model
    if strict:
        raise ValueError(f"unsupported coding model: {value}")
    return AUTO_MODEL


def preferred_provider_for(model: object) -> str:
    selected = normalise_coding_model(model, strict=True)
    if selected in {TERRA_MODEL, CODEX_MODEL}:
        return "codex"
    if selected in {SONNET_MODEL, CLAUDE_MODEL}:
        return "claude"
    return ""


def read_coding_model_preference(path: Path | None = None) -> str:
    target = Path(path) if path is not None else preference_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AUTO_MODEL
    value = payload.get("model") if isinstance(payload, dict) else payload
    return normalise_coding_model(value)


def write_coding_model_preference(model: object, path: Path | None = None) -> str:
    selected = normalise_coding_model(model, strict=True)
    target = Path(path) if path is not None else preference_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps({"model": selected}, separators=(",", ":")) + "\n"
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = handle.name
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass
    return selected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("get", "set"))
    parser.add_argument("model", nargs="?", default="")
    args = parser.parse_args()
    if args.action == "set":
        model = write_coding_model_preference(args.model)
    else:
        model = read_coding_model_preference()
    print(
        json.dumps(
            {
                "model": model,
                "options": model_options("coding", role="implement", policy=load_policy()),
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
