"""Import-safe Serena call pipeline.

The lightweight protocol is available immediately. Runtime entry points are
loaded on first access so isolated utilities such as the wake-word collector do
not inherit the call server's task, model, or process dependencies.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .protocol import (
    FLAG_FINAL,
    HEADER_SIZE,
    MAGIC,
    VERSION,
    AudioFrame,
    AudioHeader,
    AudioKind,
    ProtocolError,
)

if TYPE_CHECKING:
    from .orchestrator import CallRuntime

_RUNTIME_EXPORTS = {
    "CallRuntime",
    "handle_websocket",
    "serve_websocket",
    "warm_default_runtime_background",
}


def __getattr__(name: str) -> Any:
    if name not in _RUNTIME_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(".orchestrator", __name__), name)
    globals()[name] = value
    return value

__all__ = [
    "AudioFrame",
    "AudioHeader",
    "AudioKind",
    "CallRuntime",
    "FLAG_FINAL",
    "HEADER_SIZE",
    "MAGIC",
    "ProtocolError",
    "VERSION",
    "handle_websocket",
    "serve_websocket",
    "warm_default_runtime_background",
]
