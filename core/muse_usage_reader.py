"""Muse quota for the limits pill: honestly unavailable.

Muse exposes no quota surface at all: no usage file to tail, no ``/usage``
print mode, no local meter endpoint. The CLI answers ``muse exec`` until the
subscription says otherwise, and the only exhaustion signal is the failure of
a turn itself, which the brain and Fleet failure paths already recognise.

So this reader never invents a percentage. It reports the model the pill is
about, whether the CLI is even installed, and ``available: False`` otherwise.
A blank pill is honest; 0% used would be a lie that reads as healthy.
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

SOURCE = "muse-cli-usage"
DEFAULT_MODEL = "muse-spark"

REFRESH_SECONDS = 300.0

_LOCK = threading.Lock()
_CACHE: dict[str, object] = {"at": 0.0, "data": None}


def selected_model() -> str:
    """The Serena identity a Muse turn runs under."""
    return DEFAULT_MODEL


def _binary() -> str | None:
    found = shutil.which("muse")
    if found:
        return found
    # Desktop services inherit a smaller PATH than interactive terminals.
    home = Path.home()
    for directory in (home / ".local" / "bin", home / "bin"):
        found = shutil.which("muse", path=str(directory))
        if found:
            return found
    return None


def parse_usage(text: str, *, now: float | None = None) -> dict:
    """There is no usage text to parse; the pill stays blank but named."""
    del text
    moment = int(now if now is not None else time.time())
    return {
        "available": False,
        "source": SOURCE,
        "model": selected_model(),
        "updated_at": moment,
        "reason": "Muse exposes no quota readout",
    }


def read_muse_usage(*, now: float | None = None, force: bool = False) -> dict:
    """Current Muse limits, cached. Never raises: the panel must still draw."""
    moment = now if now is not None else time.time()
    with _LOCK:
        cached = _CACHE.get("data")
        if not force and cached is not None and moment - float(_CACHE["at"]) < REFRESH_SECONDS:
            return dict(cached)  # type: ignore[arg-type]
        if _binary() is None:
            result = {
                "available": False,
                "source": SOURCE,
                "model": selected_model(),
                "reason": "Muse CLI is not installed",
            }
        else:
            result = parse_usage("", now=moment)
        _CACHE["at"] = moment
        _CACHE["data"] = result
        return dict(result)
