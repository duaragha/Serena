"""One persistent switch for audio Serena is allowed to hear on this laptop."""

from __future__ import annotations

import os
from pathlib import Path

from core.brain_lifetime import secure_directory

DEFAULT_INPUT_MUTE_PATH = Path.home() / ".config" / "serena" / "microphone_muted"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "muted"})

__all__ = [
    "DEFAULT_INPUT_MUTE_PATH",
    "read_voice_input_muted",
    "write_voice_input_muted",
]


def read_voice_input_muted(path: Path = DEFAULT_INPUT_MUTE_PATH) -> bool:
    """Return whether Serena input is muted, defaulting safely to listening."""

    override = os.environ.get("SERENA_VOICE_INPUT_MUTED")
    if override is not None:
        return override.strip().casefold() in _TRUE_VALUES
    try:
        value = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return False
    return value.strip().casefold() in _TRUE_VALUES


def write_voice_input_muted(
    muted: bool, path: Path = DEFAULT_INPUT_MUTE_PATH
) -> bool:
    """Atomically persist the Serena-only microphone setting."""

    value = bool(muted)
    target = Path(path).expanduser()
    secure_directory(target.parent)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text("1\n" if value else "0\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(target)
    target.chmod(0o600)
    return value
