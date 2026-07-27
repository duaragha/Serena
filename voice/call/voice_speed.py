"""How fast Serena talks, as one setting every voice surface reads.

Her speech comes out of three different processes: the typed turn in the
overlay's bridge, the desk conversation in the call server, and the phone host.
A slider that only moved one of them would be a slider that "does not work", so
the value lives in a file all of them read, and it is read fresh for each
utterance rather than at startup so moving the slider is heard on her next
sentence instead of after a restart.
"""

from __future__ import annotations

import os
from pathlib import Path

from voice.call.timestretch import MAX_SPEED, MIN_SPEED, clamp_speed

DEFAULT_SPEED_PATH = Path.home() / ".config" / "serena" / "voice_speed"
DEFAULT_SPEED = 1.0

__all__ = [
    "DEFAULT_SPEED",
    "DEFAULT_SPEED_PATH",
    "MAX_SPEED",
    "MIN_SPEED",
    "read_voice_speed",
    "write_voice_speed",
]


def read_voice_speed(path: Path = DEFAULT_SPEED_PATH) -> float:
    """Current rate, or 1.0 when unset or unreadable.

    Never raises: a broken settings file must not cost her the ability to
    speak. The environment override exists for tests and one-off runs.
    """
    override = os.environ.get("SERENA_CALL_VOICE_RATE")
    if override:
        return clamp_speed(override)
    try:
        raw = Path(path).expanduser().read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return DEFAULT_SPEED
    if not raw:
        return DEFAULT_SPEED
    return clamp_speed(raw)


def write_voice_speed(speed: float, path: Path = DEFAULT_SPEED_PATH) -> float:
    """Persist a clamped rate and return what was actually stored."""
    value = clamp_speed(speed)
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(f"{value:.2f}\n", encoding="utf-8")
    temporary.replace(target)  # atomic: a reader never sees a half-written rate
    return value
