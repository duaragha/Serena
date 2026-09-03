"""An Android phone over ADB, when one is actually plugged in.

Written against a phone that is usually not there. `adb` exists on this
machine and `scrcpy` alongside it, but no device is assumed connected, so
every path starts by asking ADB what it can see and returns an explicit
unavailable status when the answer is nothing.

What is representable here is a closed allowlist on purpose. There is no
generic `adb shell`, no `uninstall`, no `rm`, no wipe. A model that gets
creative with this adapter can turn a screen on and skip a track; it cannot
reach the filesystem or remove an app, because those verbs do not exist.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

from core.adapters.base import AdapterStatus, DeviceCommand, DeviceResult

_CAPABILITIES = {
    "android.devices": "read",
    "android.screen_state": "read",
    "android.battery": "read",
    "android.screen_on": "reversible",
    "android.screen_off": "reversible",
    "android.media_play_pause": "reversible",
    "android.media_next": "reversible",
    "android.media_previous": "reversible",
    "android.volume_up": "reversible",
    "android.volume_down": "reversible",
    "android.open_app": "reversible",
    "android.mirror_start": "reversible",
}

_KEYEVENTS = {
    "android.screen_on": "224",
    "android.screen_off": "223",
    "android.media_play_pause": "85",
    "android.media_next": "87",
    "android.media_previous": "88",
    "android.volume_up": "24",
    "android.volume_down": "25",
}

_COMPENSATIONS = {
    "android.screen_on": "android.screen_off",
    "android.screen_off": "android.screen_on",
    "android.volume_up": "android.volume_down",
    "android.volume_down": "android.volume_up",
}

# Android package names, nothing else. This is what stops a target string from
# becoming shell arguments.
_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")
_SERIAL = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class AndroidAdbAdapter:
    name = "android"

    def __init__(
        self,
        *,
        runner=subprocess.run,
        adb_path: str | None = None,
        scrcpy_path: str | None = None,
        serial: str = "",
    ) -> None:
        self._runner = runner
        self._adb_path = adb_path
        self._scrcpy_path = scrcpy_path
        self._serial = serial if _SERIAL.fullmatch(serial or "") else ""

    def capabilities(self) -> Mapping[str, str]:
        return dict(_CAPABILITIES)

    def status(self) -> AdapterStatus:
        binary = self._adb()
        if not binary:
            return AdapterStatus(False, "adb is not installed on this machine")
        try:
            completed = self._run([binary, "devices"], timeout=10.0)
        except (OSError, subprocess.SubprocessError) as error:
            return AdapterStatus(False, f"{type(error).__name__}: adb could not be queried")
        if completed.returncode != 0:
            return AdapterStatus(False, "adb exited with an error")
        devices = _parse_devices(completed.stdout or "")
        online = [item for item in devices if item["state"] == "device"]
        if not online:
            unauthorized = [item for item in devices if item["state"] == "unauthorized"]
            if unauthorized:
                return AdapterStatus(
                    False,
                    "a phone is connected but has not authorized this computer",
                    {"devices": devices},
                )
            return AdapterStatus(False, "no Android device is connected", {"devices": devices})
        return AdapterStatus(True, "", {"devices": online})

    def describe(self, command: DeviceCommand) -> str:
        if command.capability == "android.open_app":
            return f"open {command.target or 'an app'} on the phone"
        if command.capability == "android.mirror_start":
            return "start mirroring the phone screen with scrcpy"
        verb = command.capability.split(".", 1)[-1].replace("_", " ")
        return f"{verb} on the phone"

    def execute(self, command: DeviceCommand) -> DeviceResult:
        if command.capability not in _CAPABILITIES:
            return self._failed(command, "unsupported", "not an Android capability")
        available = self.status()
        if not available.available:
            # Explicit, never a quiet success. The caller has to know the phone
            # was not there rather than believe the screen came on.
            return DeviceResult(
                False,
                "unavailable",
                command.capability,
                command.target,
                available.reason,
                receipt=dict(available.detail),
            )
        binary = self._adb()
        if not binary:
            return self._failed(command, "unavailable", "adb disappeared between checks")
        try:
            argv = self._argv(binary, command)
        except ValueError as error:
            return self._failed(command, "rejected", str(error))
        try:
            completed = self._run(argv, timeout=max(1.0, float(command.timeout_seconds)))
        except subprocess.TimeoutExpired:
            return self._failed(command, "timeout", "the phone did not answer in time")
        except (OSError, subprocess.SubprocessError) as error:
            return self._failed(command, "failed", f"{type(error).__name__}: {error}")
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "adb failed").strip()
            return self._failed(command, "failed", detail[:300])
        return DeviceResult(
            True,
            "completed",
            command.capability,
            command.target,
            (completed.stdout or "").strip()[:300],
            receipt={"argv": argv[1:]},
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
        if command.capability not in {"android.screen_on", "android.screen_off"}:
            return None
        binary = self._adb()
        if not binary:
            return None
        try:
            completed = self._run(
                [*self._base(binary), "shell", "dumpsys", "power"], timeout=10.0
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        match = re.search(r"mWakefulness=(\w+)", completed.stdout or "")
        if not match:
            return None
        awake = match.group(1).strip().lower() == "awake"
        return awake is (command.capability == "android.screen_on")

    # -- internals ----------------------------------------------------------

    def _argv(self, binary: str, command: DeviceCommand) -> list[str]:
        base = self._base(binary)
        capability = command.capability
        if capability == "android.devices":
            return [binary, "devices", "-l"]
        if capability == "android.screen_state":
            return [*base, "shell", "dumpsys", "power"]
        if capability == "android.battery":
            return [*base, "shell", "dumpsys", "battery"]
        if capability in _KEYEVENTS:
            return [*base, "shell", "input", "keyevent", _KEYEVENTS[capability]]
        if capability == "android.open_app":
            package = str(command.target or "").strip()
            if not _PACKAGE.fullmatch(package):
                raise ValueError(
                    "an Android app must be named by its package, such as com.spotify.music"
                )
            return [
                *base,
                "shell",
                "monkey",
                "-p",
                package,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ]
        if capability == "android.mirror_start":
            scrcpy = self._scrcpy()
            if not scrcpy:
                raise ValueError("scrcpy is not installed on this machine")
            argv = [scrcpy, "--no-audio"]
            if self._serial:
                argv += ["--serial", self._serial]
            return argv
        raise ValueError(f"{capability} has no Android command")

    def _base(self, binary: str) -> list[str]:
        return [binary, "-s", self._serial] if self._serial else [binary]

    def _run(self, argv: Sequence[str], *, timeout: float):
        return self._runner(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def _adb(self) -> str | None:
        return self._adb_path or shutil.which("adb")

    def _scrcpy(self) -> str | None:
        return self._scrcpy_path or shutil.which("scrcpy")

    @staticmethod
    def _failed(command: DeviceCommand, status: str, detail: str) -> DeviceResult:
        return DeviceResult(False, status, command.capability, command.target, detail)


def _parse_devices(output: str) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for line in str(output or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and _SERIAL.fullmatch(parts[0]):
            devices.append({"serial": parts[0], "state": parts[1]})
    return devices
