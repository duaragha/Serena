"""Device adapters Serena can reach for, present or not.

Every adapter here answers the same three questions honestly: is the thing
actually there, what would this action do, and what happened when it ran. An
adapter for hardware that is not plugged in returns an explicit unavailable
status. It never returns success for doing nothing, because a silent no-op is
how an assistant ends up saying "done" about a light that never moved.
"""

from __future__ import annotations

from core.adapters.base import (
    AdapterStatus,
    DeviceAdapter,
    DeviceCommand,
    DeviceResult,
    registry,
)

__all__ = [
    "AdapterStatus",
    "DeviceAdapter",
    "DeviceCommand",
    "DeviceResult",
    "registry",
]
