"""One persistent switch for every Serena sound played on this laptop."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_OUTPUT_MUTE_PATH = Path.home() / ".config" / "serena" / "voice_muted"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "muted"})

__all__ = [
    "DEFAULT_OUTPUT_MUTE_PATH",
    "read_voice_output_muted",
    "write_voice_output_muted",
]


def read_voice_output_muted(path: Path = DEFAULT_OUTPUT_MUTE_PATH) -> bool:
    """Return the current laptop-output setting, defaulting safely to audible."""

    override = os.environ.get("SERENA_VOICE_OUTPUT_MUTED")
    if override is not None:
        return override.strip().casefold() in _TRUE_VALUES
    try:
        value = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return False
    return value.strip().casefold() in _TRUE_VALUES


def write_voice_output_muted(
    muted: bool, path: Path = DEFAULT_OUTPUT_MUTE_PATH
) -> bool:
    """Atomically persist the laptop-output setting and return its value."""

    value = bool(muted)
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text("1\n" if value else "0\n", encoding="utf-8")
    temporary.replace(target)
    return value
