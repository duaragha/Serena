"""This laptop, through the broker that already guards it.

core.laptop_actions keeps every one of its own checks. This adapter does not
reimplement them and does not get a way around them; it only lets the shared
scene and authority machinery drive the same door everyone else uses.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.adapters.base import AdapterStatus, DeviceCommand, DeviceResult

# Capability name to declared effect. Opening a URL leaves this machine, so it
# is external even though the click feels small.
_CAPABILITIES = {
    "laptop.volume_up": "reversible",
    "laptop.volume_down": "reversible",
    "laptop.mute": "reversible",
    "laptop.unmute": "reversible",
    "laptop.toggle_mute": "reversible",
    "laptop.media_play_pause": "reversible",
    "laptop.media_next": "reversible",
    "laptop.media_previous": "reversible",
    "laptop.open_app": "reversible",
    "browser.open_url": "external",
}

# What undoes what, when an undo is honest. Skipping a track cannot be undone
# by going back, because "previous" restarts rather than restores, so the
# media moves deliberately have no compensation.
_COMPENSATIONS = {
    "laptop.volume_up": "laptop.volume_down",
    "laptop.volume_down": "laptop.volume_up",
    "laptop.mute": "laptop.unmute",
    "laptop.unmute": "laptop.mute",
}

_ACTION_NAMES = {
    "laptop.volume_up": "volume_up",
    "laptop.volume_down": "volume_down",
    "laptop.mute": "mute",
    "laptop.unmute": "unmute",
    "laptop.toggle_mute": "toggle_mute",
    "laptop.media_play_pause": "media_play_pause",
    "laptop.media_next": "media_next",
    "laptop.media_previous": "media_previous",
    "laptop.open_app": "open_app",
    "browser.open_url": "open_url",
}


class LaptopAdapter:
    name = "laptop"

    def __init__(self, *, executor=None, context_reader=None) -> None:
        self._executor = executor
        self._context_reader = context_reader

    def capabilities(self) -> Mapping[str, str]:
        return dict(_CAPABILITIES)

    def status(self) -> AdapterStatus:
        try:
            context = self._read_context()
        except Exception as error:
            return AdapterStatus(False, f"{type(error).__name__}: desktop context unavailable")
        if not context.get("session_type"):
            return AdapterStatus(
                False,
                "no graphical desktop session is attached",
                {"context": context},
            )
        return AdapterStatus(True, "", {"context": context})

    def describe(self, command: DeviceCommand) -> str:
        action = _ACTION_NAMES.get(command.capability, command.capability)
        target = f" {command.target}" if command.target else ""
        return f"run the local {action} action{target}"

    def execute(self, command: DeviceCommand) -> DeviceResult:
        action = _ACTION_NAMES.get(command.capability)
        if action is None:
            return DeviceResult(
                False, "unsupported", command.capability, command.target,
                f"{command.capability} is not a laptop capability",
            )
        origin = command.params.get("origin")
        if not isinstance(origin, Mapping):
            # The laptop broker judges the real spoken turn. Without one it
            # would refuse anyway; saying so here is clearer than a denial
            # that looks like the action failed.
            return DeviceResult(
                False, "unauthorized", command.capability, command.target,
                "the laptop broker needs the originating local turn",
            )
        result = self._execute(action, command.target, origin)
        ok = bool(getattr(result, "ok", False))
        return DeviceResult(
            ok,
            "completed" if ok else str(getattr(result, "status", "failed")),
            command.capability,
            command.target,
            str(getattr(result, "message", "")),
            receipt={"receipt_id": str(getattr(result, "receipt_id", ""))},
        )

    def compensate(self, command: DeviceCommand) -> DeviceResult | None:
        inverse = _COMPENSATIONS.get(command.capability)
        if inverse is None:
            return None
        return self.execute(
            DeviceCommand(
                capability=inverse,
                target=command.target,
                params=dict(command.params),
                timeout_seconds=command.timeout_seconds,
            )
        )

    def postcondition(self, command: DeviceCommand) -> bool | None:
        """Only the audio moves are checkable from here, so only those answer."""

        if command.capability not in {"laptop.mute", "laptop.unmute"}:
            return None
        try:
            audio = self._read_context().get("audio")
        except Exception:
            return None
        if not isinstance(audio, Mapping) or audio.get("muted") is None:
            return None
        return bool(audio["muted"]) is (command.capability == "laptop.mute")

    def _read_context(self) -> dict[str, Any]:
        if self._context_reader is not None:
            return dict(self._context_reader())
        from core.laptop_actions import read_laptop_context

        return dict(read_laptop_context())

    def _execute(self, action: str, target: str, origin: Mapping[str, object]):
        if self._executor is not None:
            return self._executor(action, target, origin=origin)
        from core.laptop_actions import execute_laptop_action

        return execute_laptop_action(action, target, origin=origin)
