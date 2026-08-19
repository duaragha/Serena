"""The contract every Serena device adapter implements.

Kept deliberately small. An adapter declares which capabilities it can perform
and what tier of effect each one has, says whether its hardware or service is
actually reachable right now, and can always describe an action without doing
it. Everything above this layer (authority, scenes, rollback) is shared, so a
new adapter is a small file rather than a new subsystem.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class AdapterStatus:
    """Whether this adapter can act at all, and the honest reason if not."""

    available: bool
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DeviceCommand:
    """One thing to do to one device."""

    capability: str
    target: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 15.0
    idempotency_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DeviceResult:
    """What happened. `simulated` is never reported as `ok` without saying so."""

    ok: bool
    status: str
    capability: str
    target: str
    detail: str = ""
    simulated: bool = False
    postcondition_checked: bool = False
    postcondition_ok: bool | None = None
    receipt: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class DeviceAdapter(Protocol):
    """What core.device_actions requires of anything it drives."""

    name: str

    def capabilities(self) -> Mapping[str, str]:
        """Capability name to declared effect, as core.action_authority means it."""

    def status(self) -> AdapterStatus:
        """Is the device or service reachable right now."""

    def describe(self, command: DeviceCommand) -> str:
        """A plain sentence about what running this would do."""

    def execute(self, command: DeviceCommand) -> DeviceResult:
        """Actually do it. Only ever called after authority allowed it."""

    def compensate(self, command: DeviceCommand) -> DeviceResult | None:
        """Undo a completed command, or None when there is no honest undo."""

    def postcondition(self, command: DeviceCommand) -> bool | None:
        """Did the world actually change. None when it cannot be checked."""


class AdapterRegistry:
    """A small named registry so scenes can refer to adapters by name."""

    def __init__(self) -> None:
        self._adapters: dict[str, DeviceAdapter] = {}

    def register(self, adapter: DeviceAdapter) -> DeviceAdapter:
        name = str(getattr(adapter, "name", "") or "").strip()
        if not name:
            raise ValueError("a device adapter must have a name")
        self._adapters[name] = adapter
        return adapter

    def get(self, name: str) -> DeviceAdapter | None:
        return self._adapters.get(str(name or "").strip())

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def for_capability(self, capability: str) -> DeviceAdapter | None:
        """The adapter that owns this capability, by its declared list."""

        wanted = str(capability or "").strip()
        for _name, adapter in sorted(self._adapters.items()):
            if wanted in adapter.capabilities():
                return adapter
        return None

    def clear(self) -> None:
        self._adapters.clear()

    def snapshot(self) -> dict[str, Any]:
        """Every adapter and whether it is actually usable, for the cockpit."""

        report: dict[str, Any] = {}
        for name, adapter in sorted(self._adapters.items()):
            try:
                status = adapter.status()
            except Exception as error:  # an adapter must never break the report
                status = AdapterStatus(False, f"{type(error).__name__}: status check failed")
            report[name] = {
                **status.to_dict(),
                "capabilities": dict(adapter.capabilities()),
            }
        return report


registry = AdapterRegistry()
