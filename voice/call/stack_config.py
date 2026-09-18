"""One synced description of the voice stack, so every machine sounds the same.

Her phone line is served by the voice host on the PC and her desk voice by the
mobile host on the laptop, but both build the same ``CallRuntime`` out of the
same code: ``build_desk_runtime`` literally reuses the call runtime's stt,
brain and tts objects. Nothing about the two is meant to differ.

What differed anyway was the environment of whichever service happened to be
hosting it. The PC had ``SERENA_CALL_TTS_BACKEND`` set at the Windows user
level; the laptop's mobile host unit set nothing at all and fell through to the
built-in Kokoro default. Same Serena, same code, same policy -- and she
answered the phone as Grace and answered the desk as a robot.

So the choice lives in the repository, which Syncthing already keeps byte
identical on both machines, instead of in two hand-maintained unit files that
nothing compares. Environment still wins wherever it is set, so one machine can
be pinned for a test without touching the file everyone reads.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "voice-stack.json"


def config_path() -> Path:
    override = os.environ.get("SERENA_VOICE_STACK_CONFIG", "").strip()
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def stack_config() -> dict[str, Any]:
    """The synced stack, or an empty one when it is missing or unreadable.

    A broken file must never be the reason she cannot speak, so every failure
    here reads as "nothing configured" and the built-in defaults take over.
    """

    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def voice_setting(section: str, key: str, *, env: str, default: str = "") -> str:
    """Environment first, then the synced file, then the built-in default."""

    from_env = os.environ.get(env, "").strip()
    if from_env:
        return from_env
    block = stack_config().get(section)
    if isinstance(block, dict):
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default
