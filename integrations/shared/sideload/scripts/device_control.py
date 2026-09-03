"""Sanitized, screenshot-gated control of a connected iPhone.

The helper uses pymobiledevice3's no-root CoreDevice userspace tunnel. It never
prints or caches a full device identifier. Touches and drags require a recent
screenshot whose dimensions still match the live display, which prevents an
agent from reusing coordinates from a different screen or device.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import plistlib
import re
import shutil
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

COMMAND_TIMEOUT_SECONDS = 90
MAX_SCREEN_AGE_SECONDS = 180
MIN_PMD_VERSION = (11, 3, 0)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
UUID_RE = re.compile(
    r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b"
)
DEVICE_ID_RE = re.compile(r"\b(?:[0-9A-Fa-f]{24,}|[0-9A-Fa-f]{8}-[0-9A-Fa-f-]{20,})\b")
LONG_SECRET_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_./+=-]{80,}")


class ControlError(RuntimeError):
    """A safe, user-actionable control failure."""


def safe_text(value: str, limit: int = 600) -> str:
    text = EMAIL_RE.sub("[redacted-email]", value)
    text = UUID_RE.sub("[redacted-id]", text)
    text = DEVICE_ID_RE.sub("[redacted-device-id]", text)
    text = LONG_SECRET_RE.sub("[redacted-secret]", text)
    return " ".join(text.split())[:limit]


def device_fingerprint(udid: str) -> str:
    return hashlib.sha256(("serena-sideload-device-v1:" + udid).encode()).hexdigest()[:12]


def run_command(
    argv: list[str],
    *,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        raise ControlError(f"required tool is unavailable: {Path(argv[0]).name}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ControlError(f"{Path(argv[0]).name} timed out") from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace")
        raise ControlError(
            safe_text(detail) or f"{Path(argv[0]).name} exited {completed.returncode}"
        )
    return completed


def pmd_command() -> list[str]:
    override = os.environ.get("SIDELOAD_PMD")
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return [str(candidate)]
        raise ControlError("SIDELOAD_PMD does not point to a pymobiledevice3 executable")

    uvx = shutil.which("uvx")
    if uvx:
        return [uvx, "--no-progress", "pymobiledevice3"]

    candidates = [
        shutil.which("pymobiledevice3"),
        str(Path.home() / ".pmd3-venv" / "bin" / "pymobiledevice3"),
        str(Path.home() / ".pmd3-venv" / "Scripts" / "pymobiledevice3.exe"),
        str(Path.home() / "atrium-appliance" / "venv" / "bin" / "pymobiledevice3"),
    ]
    for value in candidates:
        if value and Path(value).is_file():
            return [value]
    raise ControlError(
        "pymobiledevice3 is unavailable; install uv/uvx or create ~/.pmd3-venv from the official package"
    )


def pmd_env(udid: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    env["UV_NO_PROGRESS"] = "1"
    if udid:
        env["PYMOBILEDEVICE3_UDID"] = udid
    return env


def pmd_run(
    args: list[str], *, udid: str | None = None, timeout: int = COMMAND_TIMEOUT_SECONDS
) -> bytes:
    result = run_command(pmd_command() + args, timeout=timeout, env=pmd_env(udid))
    return result.stdout


def parse_json(raw: bytes, label: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ControlError(f"{label} returned unreadable data") from exc


def version_tuple(value: str) -> tuple[int, int, int]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())


def pmd_version() -> str:
    value = pmd_run(["version"]).decode("utf-8", errors="replace").strip()
    if version_tuple(value) < MIN_PMD_VERSION:
        required = ".".join(str(part) for part in MIN_PMD_VERSION)
        raise ControlError(
            f"pymobiledevice3 {safe_text(value)} is too old for direct touch control; {required}+ is required"
        )
    return safe_text(value, 40)


def discover_udids() -> list[str]:
    idevice_id = shutil.which("idevice_id")
    if idevice_id:
        try:
            raw = run_command([idevice_id, "-l"]).stdout.decode("utf-8", errors="replace")
            devices = sorted({line.strip() for line in raw.splitlines() if line.strip()})
            if devices:
                return devices
        except ControlError:
            pass

    completed = run_command(
        pmd_command() + ["usbmux", "list", "--simple", "--usb"],
        env=pmd_env(),
    )
    combined = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
    found = {
        line.strip()
        for line in combined.splitlines()
        if re.fullmatch(r"[0-9A-Fa-f-]{24,64}", line.strip())
    }
    if not found and "Failed to connect to usbmuxd" in combined:
        raise ControlError("usbmuxd is unavailable; start the host service and retry once")
    return sorted(found)


def select_udid(requested: str | None) -> str:
    devices = discover_udids()
    if not devices:
        raise ControlError("no USB-connected iPhone is visible")
    if requested:
        matches = [
            value
            for value in devices
            if requested == value or requested.lower() == device_fingerprint(value).lower()
        ]
        if len(matches) != 1:
            raise ControlError("the requested device does not match exactly one connected iPhone")
        return matches[0]
    if len(devices) != 1:
        fingerprints = ", ".join(device_fingerprint(value) for value in devices)
        raise ControlError(
            f"multiple iPhones are connected; select one by short fingerprint: {fingerprints}"
        )
    return devices[0]


def host_pairing_status(udid: str) -> str:
    tool = shutil.which("idevicepair")
    if not tool:
        return "unknown"
    try:
        run_command([tool, "-u", udid, "validate"], timeout=20)
    except ControlError:
        return "invalid-or-unavailable"
    return "valid"


def live_display_size(udid: str) -> tuple[int, int]:
    parsed = parse_json(
        pmd_run(
            ["developer", "core-device", "get-display-info", "--userspace"],
            udid=udid,
        ),
        "display probe",
    )
    displays = parsed.get("displays") if isinstance(parsed, dict) else parsed
    if not isinstance(displays, list):
        raise ControlError("display probe returned an unexpected shape")
    for display in displays:
        if not isinstance(display, dict):
            continue
        mode = display.get("currentMode")
        size = mode.get("size") if isinstance(mode, dict) else None
        if isinstance(size, list) and len(size) == 2:
            width, height = (int(float(size[0])), int(float(size[1])))
            if width > 0 and height > 0:
                return width, height
    raise ControlError("the phone did not report an active display")


def hid_surfaces(udid: str) -> set[str]:
    parsed = parse_json(
        pmd_run(
            [
                "developer",
                "core-device",
                "universal-hid-service",
                "list-connected",
                "--userspace",
            ],
            udid=udid,
        ),
        "HID probe",
    )
    services = parsed.get("connectedServices") if isinstance(parsed, dict) else None
    if not isinstance(services, list):
        raise ControlError("HID probe returned an unexpected shape")
    surfaces: set[str] = set()
    for service in services:
        if not isinstance(service, dict):
            continue
        product = str(service.get("Product") or "").lower()
        page = service.get("PrimaryUsagePage")
        usage = service.get("PrimaryUsage")
        if "touch" in product or page == 13:
            surfaces.add("touchscreen")
        if "keyboard" in product or (page == 1 and usage == 6):
            surfaces.add("keyboard")
    return surfaces


def png_size(path: Path) -> tuple[int, int]:
    try:
        header = path.read_bytes()[:24]
    except OSError as exc:
        raise ControlError("screenshot could not be read") from exc
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ControlError("screen capture did not produce a valid PNG")
    return struct.unpack(">II", header[16:24])


def new_screen_path() -> Path:
    handle, value = tempfile.mkstemp(prefix="sideload-screen-", suffix=".png")
    os.close(handle)
    path = Path(value)
    path.unlink(missing_ok=True)
    return path


def resolve_output(value: str | None) -> Path:
    if value is None:
        return new_screen_path()
    path = Path(value).expanduser().resolve()
    if not path.parent.is_dir():
        raise ControlError("screenshot parent directory does not exist")
    if path.exists():
        raise ControlError("refusing to overwrite an existing screenshot")
    return path


def capture_screen(udid: str, output: Path) -> tuple[int, int]:
    try:
        pmd_run(
            [
                "developer",
                "core-device",
                "screen-capture",
                "screenshot",
                str(output),
                "--userspace",
            ],
            udid=udid,
        )
        output.chmod(0o600)
        return png_size(output)
    except (ControlError, OSError):
        output.unlink(missing_ok=True)
        raise


def validate_source_screen(udid: str, value: str) -> tuple[Path, int, int]:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ControlError("the source screenshot does not exist")
    age = time.time() - path.stat().st_mtime
    if age < -5 or age > MAX_SCREEN_AGE_SECONDS:
        raise ControlError(
            f"the source screenshot is stale; capture a new one within {MAX_SCREEN_AGE_SECONDS} seconds"
        )
    width, height = png_size(path)
    live_width, live_height = live_display_size(udid)
    if (width, height) != (live_width, live_height):
        raise ControlError(
            "the source screenshot dimensions no longer match the live display; capture a new screenshot"
        )
    return path, width, height


def normalized_coordinate(value: int, maximum: int) -> int:
    if value < 0 or value >= maximum:
        raise ControlError(f"coordinate {value} is outside a {maximum}-pixel axis")
    return round(value * 65535 / max(1, maximum - 1))


def parse_plist_bytes(raw: bytes) -> Any:
    start = raw.find(b"<?xml")
    if start >= 0:
        raw = raw[start:]
    end = raw.rfind(b"</plist>")
    if end >= 0:
        raw = raw[: end + len(b"</plist>")]
    return plistlib.loads(raw)


def installed_apps(udid: str) -> list[dict[str, Any]]:
    tool = shutil.which("ideviceinstaller")
    if tool:
        raw = run_command([tool, "-u", udid, "-l", "-o", "xml"], timeout=90).stdout
        try:
            parsed = parse_plist_bytes(raw)
        except Exception as exc:
            raise ControlError("installed-app inventory was unreadable") from exc
        return (
            [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
        )

    parsed = parse_json(pmd_run(["apps", "list"], udid=udid), "installed-app inventory")
    apps = []
    if isinstance(parsed, dict):
        for bundle_id, details in parsed.items():
            item = dict(details) if isinstance(details, dict) else {}
            item.setdefault("CFBundleIdentifier", bundle_id)
            apps.append(item)
    elif isinstance(parsed, list):
        apps = [item for item in parsed if isinstance(item, dict)]
    return apps


def resolve_app(udid: str, target: str) -> tuple[str, str]:
    wanted = target.casefold().strip()
    matches: list[tuple[str, str]] = []
    for app in installed_apps(udid):
        actual = str(app.get("CFBundleIdentifier") or "")
        canonical = str(app.get("ALTBundleIdentifier") or "")
        label = str(
            app.get("CFBundleDisplayName") or app.get("CFBundleName") or canonical or actual
        )
        values = {value.casefold() for value in (actual, canonical, label) if value}
        if wanted in values and actual:
            matches.append((actual, label))
    if not matches:
        raise ControlError(f"no installed native app exactly matches {safe_text(target, 80)}")
    unique = {actual: label for actual, label in matches}
    if len(unique) != 1:
        labels = ", ".join(sorted(set(unique.values())))
        raise ControlError(f"app name is ambiguous; exact matches were: {safe_text(labels, 200)}")
    actual, label = next(iter(unique.items()))
    return actual, label


def action_probe(udid: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "source": "live",
        "device": device_fingerprint(udid),
        "host_pairing": host_pairing_status(udid),
        "pymobiledevice3": "unavailable",
        "display": "unavailable",
        "screen_capture": "unavailable",
        "touch_control": "unavailable",
        "keyboard_control": "unavailable",
        "blockers": [],
    }
    try:
        result["pymobiledevice3"] = pmd_version()
    except ControlError as exc:
        result["blockers"].append(str(exc))
        return result

    try:
        width, height = live_display_size(udid)
        result["display"] = {"width": width, "height": height}
    except ControlError as exc:
        result["blockers"].append(str(exc))
        return result

    try:
        surfaces = hid_surfaces(udid)
        result["touch_control"] = "ready" if "touchscreen" in surfaces else "unavailable"
        result["keyboard_control"] = "ready" if "keyboard" in surfaces else "unavailable"
        if "touchscreen" not in surfaces:
            result["blockers"].append("the CoreDevice touchscreen surface is unavailable")
    except ControlError as exc:
        result["blockers"].append(str(exc))

    probe_path = new_screen_path()
    try:
        captured = capture_screen(udid, probe_path)
        result["screen_capture"] = "ready" if captured == (width, height) else "dimension-mismatch"
        if captured != (width, height):
            result["blockers"].append("captured screen dimensions do not match the active display")
    except ControlError as exc:
        result["blockers"].append(str(exc))
    finally:
        probe_path.unlink(missing_ok=True)

    result["ok"] = (
        result["host_pairing"] != "invalid-or-unavailable"
        and result["screen_capture"] == "ready"
        and result["touch_control"] == "ready"
    )
    return result


def action_screenshot(udid: str, output: str | None) -> dict[str, Any]:
    path = resolve_output(output)
    width, height = capture_screen(udid, path)
    return {
        "ok": True,
        "device": device_fingerprint(udid),
        "screenshot": str(path),
        "width": width,
        "height": height,
        "captured_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
    }


def action_launch(udid: str, target: str) -> dict[str, Any]:
    actual, label = resolve_app(udid, target)
    pmd_run(
        ["developer", "dvt", "launch", actual, "--userspace"],
        udid=udid,
    )
    return {
        "ok": True,
        "device": device_fingerprint(udid),
        "launched": label,
    }


def after_action_screen(udid: str, output: str | None, wait_seconds: float) -> dict[str, Any]:
    time.sleep(wait_seconds)
    path = resolve_output(output)
    width, height = capture_screen(udid, path)
    return {"screenshot": str(path), "width": width, "height": height}


def action_tap(
    udid: str,
    screen: str,
    x: int,
    y: int,
    after: str | None,
    wait_seconds: float,
) -> dict[str, Any]:
    _, width, height = validate_source_screen(udid, screen)
    hid_x = normalized_coordinate(x, width)
    hid_y = normalized_coordinate(y, height)
    pmd_run(
        [
            "developer",
            "core-device",
            "universal-hid-service",
            "tap",
            "--userspace",
            "--",
            str(hid_x),
            str(hid_y),
        ],
        udid=udid,
    )
    captured = after_action_screen(udid, after, wait_seconds)
    return {
        "ok": True,
        "device": device_fingerprint(udid),
        "action": "tap",
        "after": captured,
    }


def action_swipe(
    udid: str,
    screen: str,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    duration: float,
    after: str | None,
    wait_seconds: float,
) -> dict[str, Any]:
    _, width, height = validate_source_screen(udid, screen)
    coordinates = [
        normalized_coordinate(x1, width),
        normalized_coordinate(y1, height),
        normalized_coordinate(x2, width),
        normalized_coordinate(y2, height),
    ]
    pmd_run(
        [
            "developer",
            "core-device",
            "universal-hid-service",
            "drag",
            "--duration",
            str(duration),
            "--userspace",
            "--",
            *(str(value) for value in coordinates),
        ],
        udid=udid,
    )
    captured = after_action_screen(udid, after, wait_seconds)
    return {
        "ok": True,
        "device": device_fingerprint(udid),
        "action": "swipe",
        "after": captured,
    }


def print_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if result.get("ok"):
        print(
            "device control: ready" if result.get("source") == "live" else "device action: complete"
        )
    else:
        print("device control: unavailable")
    for key, value in result.items():
        if key not in {"ok", "source", "blockers"}:
            print(f"{key}: {value}")
    for blocker in result.get("blockers", []):
        print(f"blocker: {blocker}")


def bounded_wait(value: str) -> float:
    parsed = float(value)
    if not 0 <= parsed <= 10:
        raise argparse.ArgumentTypeError("wait must be between 0 and 10 seconds")
    return parsed


def bounded_duration(value: str) -> float:
    parsed = float(value)
    if not 0.05 <= parsed <= 5:
        raise argparse.ArgumentTypeError("duration must be between 0.05 and 5 seconds")
    return parsed


def add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--device",
        help="exact UDID or sanitized status fingerprint; the full value is never emitted or cached",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    probe = commands.add_parser(
        "probe", help="prove screenshot and touch control without changing the UI"
    )
    add_json_flag(probe)

    screenshot = commands.add_parser("screenshot", help="capture the current phone screen")
    screenshot.add_argument("--output", help="new PNG path; defaults to a private temporary path")
    add_json_flag(screenshot)

    launch = commands.add_parser(
        "launch", help="launch one installed native app by exact name or identity"
    )
    launch.add_argument("app")
    add_json_flag(launch)

    tap = commands.add_parser(
        "tap", help="tap a point from a recent screenshot and capture the result"
    )
    tap.add_argument("--screen", required=True, help="recent PNG produced by this helper")
    tap.add_argument("--x", required=True, type=int, help="x coordinate in screenshot pixels")
    tap.add_argument("--y", required=True, type=int, help="y coordinate in screenshot pixels")
    tap.add_argument("--after", help="new output PNG; defaults to a private temporary path")
    tap.add_argument("--wait", type=bounded_wait, default=0.8)
    add_json_flag(tap)

    swipe = commands.add_parser("swipe", help="drag between two points and capture the result")
    swipe.add_argument("--screen", required=True, help="recent PNG produced by this helper")
    swipe.add_argument("--x1", required=True, type=int)
    swipe.add_argument("--y1", required=True, type=int)
    swipe.add_argument("--x2", required=True, type=int)
    swipe.add_argument("--y2", required=True, type=int)
    swipe.add_argument("--duration", type=bounded_duration, default=0.6)
    swipe.add_argument("--after", help="new output PNG; defaults to a private temporary path")
    swipe.add_argument("--wait", type=bounded_wait, default=0.8)
    add_json_flag(swipe)

    args = parser.parse_args()
    try:
        udid = select_udid(args.device)
        if args.command == "probe":
            result = action_probe(udid)
        elif args.command == "screenshot":
            result = action_screenshot(udid, args.output)
        elif args.command == "launch":
            result = action_launch(udid, args.app)
        elif args.command == "tap":
            result = action_tap(udid, args.screen, args.x, args.y, args.after, args.wait)
        elif args.command == "swipe":
            result = action_swipe(
                udid,
                args.screen,
                args.x1,
                args.y1,
                args.x2,
                args.y2,
                args.duration,
                args.after,
                args.wait,
            )
        else:  # pragma: no cover - argparse guarantees a known command
            raise ControlError("unknown command")
    except ControlError as exc:
        failure = {"ok": False, "error": safe_text(str(exc))}
        print_result(failure, getattr(args, "json", False))
        return 2

    print_result(result, args.json)
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
