#!/usr/bin/env python3
"""Sanitized, read-only SideStore and LiveContainer inventory.

The collector deliberately never emits or caches full device identifiers,
signing identities, pairing-record contents, certificates, keys, or raw logs.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Iterator


SCHEMA_VERSION = 1
COMMAND_TIMEOUT_SECONDS = 45
LOG_TAIL_BYTES = 512 * 1024

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
UUID_RE = re.compile(
    r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b"
)
LONG_SECRET_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_./+=-]{80,}")
DEVICE_ID_RE = re.compile(r"\b(?:[0-9A-Fa-f]{24,}|[0-9A-Fa-f]{8}-[0-9A-Fa-f-]{20,})\b")

LOG_SIGNALS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "local-vpn-seen",
        "info",
        re.compile(r"using the first .*vpn interface|mode:\s*\.localVPN", re.I),
    ),
    (
        "local-vpn-or-wifi-missing",
        "error",
        re.compile(r"\b1414\b|no (?:side|stos|localdev)?vpn endpoint|no wi-?fi", re.I),
    ),
    (
        "pairing-or-udid",
        "error",
        re.compile(
            r"\b1006\b|could not determine (?:this )?device.?s udid|"
            r"pairing file.{0,80}(?:invalid|missing|expired|corrupt)",
            re.I,
        ),
    ),
    (
        "minimuxer-afc",
        "error",
        re.compile(r"minimuxer.{0,120}(?:error|failed)|\bAFC\b.{0,80}\b27\b", re.I),
    ),
    (
        "anisette-unreachable",
        "error",
        re.compile(r"\b1412\b|unable to connect to a v3 anisette", re.I),
    ),
    (
        "anisette-invalid-or-stale",
        "error",
        re.compile(r"\b3021\b|invalid anisette|\b-45061\b", re.I),
    ),
    (
        "app-id-capacity",
        "error",
        re.compile(r"\b1009\b|\b3013\b|10 app ids|no app ids remaining", re.I),
    ),
    (
        "certificate-revoked-or-missing",
        "error",
        re.compile(r"certificate.{0,100}(?:revoked|not found|missing)|\b3008\b", re.I),
    ),
    (
        "code-signature-invalid",
        "error",
        re.compile(r"code signature.{0,80}invalid|invalid signature", re.I),
    ),
    (
        "ipa-invalid-format",
        "error",
        re.compile(r"\b1007\b|app is in an invalid format|invalid ipa", re.I),
    ),
    (
        "disk-write-failure",
        "error",
        re.compile(r"failed to write to disk|nscocoaerrordomain.{0,30}\b512\b", re.I),
    ),
    (
        "network-interruption",
        "warning",
        re.compile(r"econnreset|early eof|network connection was lost|timed out", re.I),
    ),
)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_utc(value: float | dt.datetime) -> str:
    if isinstance(value, (int, float)):
        value = dt.datetime.fromtimestamp(value, dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def safe_text(value: str, limit: int = 500) -> str:
    text = EMAIL_RE.sub("[redacted-email]", value)
    text = UUID_RE.sub("[redacted-id]", text)
    text = DEVICE_ID_RE.sub("[redacted-device-id]", text)
    text = LONG_SECRET_RE.sub("[redacted-secret]", text)
    return " ".join(text.split())[:limit]


def run_command(argv: list[str], timeout: int = COMMAND_TIMEOUT_SECONDS) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        return {"ok": False, "returncode": 127, "stdout": b"", "error": "tool unavailable"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": 124, "stdout": b"", "error": "command timed out"}

    stderr = completed.stderr.decode("utf-8", errors="replace")
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "error": safe_text(stderr) if completed.returncode else "",
    }


def parse_plist_bytes(raw: bytes) -> Any:
    start = raw.find(b"<?xml")
    if start >= 0:
        raw = raw[start:]
    end = raw.rfind(b"</plist>")
    if end >= 0:
        raw = raw[: end + len(b"</plist>")]
    return plistlib.loads(raw)


def load_plist(path: Path) -> Any:
    return plistlib.loads(path.read_bytes())


def device_fingerprint(udid: str) -> str:
    return hashlib.sha256(("serena-sideload-device-v1:" + udid).encode()).hexdigest()[:12]


def state_file() -> Path:
    override = os.environ.get("SIDELOAD_STATE_DIR")
    if override:
        root = Path(override).expanduser()
    elif sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        root = Path(os.environ["LOCALAPPDATA"]) / "Serena" / "sideload"
    else:
        root = Path.home() / ".local" / "state" / "serena" / "sideload"
    return root / "latest.json"


def load_policy() -> dict[str, Any]:
    policy_path = Path(__file__).resolve().parent.parent / "references" / "local-policy.json"
    try:
        parsed = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"apps": {}}
    return parsed if isinstance(parsed, dict) else {"apps": {}}


def public_policy(policy: dict[str, Any], bundle_id: str) -> dict[str, Any] | None:
    apps = policy.get("apps")
    if not isinstance(apps, dict):
        return None
    entry = apps.get(bundle_id)
    if not isinstance(entry, dict):
        return None
    allowed = ("name", "preferred_placement", "preserve", "source_url")
    return {key: entry[key] for key in allowed if key in entry}


def discover_device_ids(include_network: bool, requested: str | None) -> list[tuple[str, str]]:
    if requested:
        return [(requested, "requested")]

    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    usb = run_command(["idevice_id", "-l"])
    if usb["ok"]:
        for value in usb["stdout"].decode(errors="replace").splitlines():
            value = value.strip()
            if value and value not in seen:
                found.append((value, "usb"))
                seen.add(value)

    if include_network or not found:
        network = run_command(["idevice_id", "-n", "-l"])
        if network["ok"]:
            for value in network["stdout"].decode(errors="replace").splitlines():
                value = value.strip()
                if value and value not in seen:
                    found.append((value, "network"))
                    seen.add(value)
    return found


def read_device_info(udid: str, network: bool) -> tuple[dict[str, Any], str | None]:
    argv = ["ideviceinfo", "-u", udid]
    if network:
        argv.append("-n")
    argv.append("-x")
    result = run_command(argv)
    if not result["ok"]:
        return {}, result["error"] or "device information unavailable"
    try:
        info = parse_plist_bytes(result["stdout"])
    except Exception as exc:  # plist formats vary by libimobiledevice release
        return {}, f"device information was not a readable plist: {type(exc).__name__}"
    if not isinstance(info, dict):
        return {}, "device information had an unexpected format"
    selected = {
        "name": info.get("DeviceName") or "iPhone",
        "product_type": info.get("ProductType"),
        "ios_version": info.get("ProductVersion"),
        "build_version": info.get("BuildVersion"),
    }
    return {key: value for key, value in selected.items() if value is not None}, None


def validate_host_pairing(udid: str) -> dict[str, Any]:
    result = run_command(["idevicepair", "-u", udid, "validate"])
    return {
        "status": "valid" if result["ok"] else "invalid-or-unavailable",
        "detail": "" if result["ok"] else result["error"],
    }


def is_developer_signed(app: dict[str, Any]) -> bool:
    signer = str(app.get("SignerIdentity") or "").lower()
    bundle = str(app.get("CFBundleIdentifier") or "").lower()
    return bool(app.get("ALTBundleIdentifier")) or "developer" in signer or any(
        marker in bundle for marker in ("sidestore", "livecontainer")
    )


def canonical_bundle_id(app: dict[str, Any]) -> str:
    return str(app.get("ALTBundleIdentifier") or app.get("CFBundleIdentifier") or "unknown")


def native_app_public(app: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    canonical = canonical_bundle_id(app)
    signer = str(app.get("SignerIdentity") or "").lower()
    entitlements = app.get("Entitlements")
    entitlement_names = sorted(str(key) for key in entitlements) if isinstance(entitlements, dict) else []
    background = app.get("UIBackgroundModes")
    entry: dict[str, Any] = {
        "name": app.get("CFBundleDisplayName") or app.get("CFBundleName") or canonical,
        "bundle_id": canonical,
        "version": app.get("CFBundleShortVersionString"),
        "build": app.get("CFBundleVersion"),
        "manager_hint": "sidestore" if app.get("ALTBundleIdentifier") else "developer-signing",
        "signing_class": "development" if "developer" in signer else "unknown",
        "profile_validated": app.get("ProfileValidated"),
        "background_modes": background if isinstance(background, list) else [],
        "entitlement_names": entitlement_names,
    }
    annotation = public_policy(policy, canonical)
    if annotation:
        entry["policy"] = annotation
    return entry


def list_native_apps(
    udid: str, network: bool, policy: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    argv = ["ideviceinstaller", "-u", udid]
    if network:
        argv.append("-n")
    argv.extend(["-l", "-o", "xml"])
    result = run_command(argv, timeout=90)
    if not result["ok"]:
        return [], [], result["error"] or "application inventory unavailable"
    try:
        parsed = parse_plist_bytes(result["stdout"])
    except Exception as exc:
        return [], [], f"application inventory was not a readable plist: {type(exc).__name__}"
    if not isinstance(parsed, list):
        return [], [], "application inventory had an unexpected format"

    private_apps = [app for app in parsed if isinstance(app, dict) and is_developer_signed(app)]
    public_apps = [native_app_public(app, policy) for app in private_apps]
    public_apps.sort(key=lambda item: (str(item["name"]).lower(), item["bundle_id"]))
    return public_apps, private_apps, None


def unmount_command() -> list[str] | None:
    for command in ("fusermount3", "fusermount"):
        if shutil.which(command):
            return [command, "-u"]
    if shutil.which("umount"):
        return ["umount"]
    return None


@contextlib.contextmanager
def mounted_container(
    udid: str,
    actual_bundle_id: str,
    issues: list[str],
) -> Iterator[Path | None]:
    if not shutil.which("ifuse"):
        issues.append("ifuse is unavailable; app-container discovery was skipped")
        yield None
        return

    mount_path = Path(tempfile.mkdtemp(prefix="sideload-readonly-"))
    mounted = False
    try:
        result = run_command(
            [
                "ifuse",
                "--udid",
                udid,
                "-o",
                "ro",
                "--container",
                actual_bundle_id,
                str(mount_path),
            ],
            timeout=30,
        )
        if not result["ok"]:
            issues.append("container could not be mounted read-only: " + (result["error"] or "unknown error"))
            yield None
            return
        mounted = True
        yield mount_path
    finally:
        if mounted:
            command = unmount_command()
            if command:
                result = run_command(command + [str(mount_path)], timeout=20)
                mounted = not result["ok"]
            if mounted:
                issues.append("read-only container unmount failed; mountpoint was preserved")
        if not mounted:
            try:
                mount_path.rmdir()
            except OSError:
                # Never recursively remove a path that may still be a device mount.
                pass


def pairing_file_status(path: Path, udid: str) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing"}
    try:
        stat = path.stat()
        parsed = load_plist(path)
    except Exception as exc:
        return {
            "status": "malformed-or-unreadable",
            "detail": type(exc).__name__,
        }
    if not isinstance(parsed, dict):
        return {"status": "malformed", "detail": "not a dictionary plist"}

    required = ("HostID", "SystemBUID", "HostCertificate", "HostPrivateKey", "EscrowBag", "UDID")
    missing = [key for key in required if not parsed.get(key)]
    record_udid = str(parsed.get("UDID") or "")
    if missing:
        status = "incomplete"
    elif record_udid != udid:
        status = "belongs-to-another-device"
    else:
        status = "structure-valid-and-device-matched"
    return {
        "status": status,
        "missing_required_fields": missing,
        "size_bytes": stat.st_size,
        "modified_at": iso_utc(stat.st_mtime),
        "validity_note": "structure and device match do not prove that Apple still accepts the record",
    }


def read_tail(path: Path, maximum: int = LOG_TAIL_BYTES) -> str:
    with path.open("rb") as handle:
        try:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - maximum))
        except OSError:
            pass
        return handle.read(maximum).decode("utf-8", errors="replace")


def scan_sidestore_logs(log_dir: Path) -> dict[str, Any]:
    try:
        logs = sorted(
            (path for path in log_dir.iterdir() if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:3]
    except OSError:
        logs = []
    if not logs:
        return {"status": "unavailable", "signals": []}

    combined = "\n".join(read_tail(path) for path in reversed(logs))
    signals = []
    for code, severity, pattern in LOG_SIGNALS:
        if pattern.search(combined):
            signals.append({"code": code, "severity": severity})
    newest_mtime = max(path.stat().st_mtime for path in logs)
    return {
        "status": "classified",
        "latest_log_modified_at": iso_utc(newest_mtime),
        "files_considered": len(logs),
        "signals": signals,
        "note": "signals are categorical matches from recent log tails, not proof of current state",
    }


def scan_sidestore_root(root: Path, udid: str) -> dict[str, Any]:
    pairing = root / "Documents" / "ALTPairingFile.mobiledevicepairing"
    logs = root / "Documents" / "ConsoleLogs"
    return {
        "pairing_file": pairing_file_status(pairing, udid),
        "recent_logs": scan_sidestore_logs(logs),
    }


def safe_lc_settings(parsed: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "installationDate",
        "lastLaunched",
        "LCPatchRevision",
        "fixFilePickerNew",
        "fixLocalNotification",
        "isJITNeeded",
        "isHidden",
        "MultitaskSpecified",
    )
    def public_value(value: Any) -> Any:
        if isinstance(value, dt.datetime):
            return iso_utc(value)
        if isinstance(value, bytes):
            return {"data_present": True, "size_bytes": len(value)}
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, list):
            return [public_value(item) for item in value]
        if isinstance(value, dict):
            return {str(key): public_value(item) for key, item in value.items()}
        return safe_text(str(value))

    return {key: public_value(parsed[key]) for key in keys if key in parsed}


def scan_livecontainer(
    mount_root: Path,
    udid: str,
    policy: dict[str, Any],
) -> dict[str, Any]:
    documents = mount_root / "Documents"
    guest_root = documents / "Applications"
    data_root = documents / "Data" / "Application"
    side_root = documents / "SideStore"

    guests: list[dict[str, Any]] = []
    referenced_data_folders: set[str] = set()
    if guest_root.is_dir():
        for app_path in sorted(guest_root.glob("*.app")):
            try:
                info = load_plist(app_path / "Info.plist")
            except Exception:
                info = {}
            try:
                lc_info = load_plist(app_path / "LCAppInfo.plist")
            except Exception:
                lc_info = {}
            if not isinstance(info, dict):
                info = {}
            if not isinstance(lc_info, dict):
                lc_info = {}

            bundle_id = str(info.get("CFBundleIdentifier") or app_path.stem)
            containers = lc_info.get("LCContainers")
            if isinstance(containers, list):
                for container in containers:
                    if isinstance(container, dict) and container.get("folderName"):
                        referenced_data_folders.add(str(container["folderName"]))
            executable_name = info.get("CFBundleExecutable")
            executable_ok = bool(executable_name and (app_path / str(executable_name)).is_file())
            plugins = list((app_path / "PlugIns").glob("*.appex")) if (app_path / "PlugIns").is_dir() else []
            background = info.get("UIBackgroundModes")
            guest: dict[str, Any] = {
                "name": info.get("CFBundleDisplayName") or info.get("CFBundleName") or bundle_id,
                "bundle_id": bundle_id,
                "version": info.get("CFBundleShortVersionString"),
                "build": info.get("CFBundleVersion"),
                "main_executable_present": executable_ok,
                "signature_directory_present": (app_path / "_CodeSignature").is_dir(),
                "app_extensions": len(plugins),
                "background_modes": background if isinstance(background, list) else [],
                "data_containers_declared": len(containers) if isinstance(containers, list) else 0,
                "livecontainer_settings": safe_lc_settings(lc_info),
            }
            annotation = public_policy(policy, bundle_id)
            if annotation:
                guest["policy"] = annotation
            guests.append(guest)

    data_apps: dict[str, int] = {}
    data_folder_names: set[str] = set()
    data_directories = 0
    data_without_metadata = 0
    if data_root.is_dir():
        for container_path in sorted(path for path in data_root.iterdir() if path.is_dir()):
            data_directories += 1
            data_folder_names.add(container_path.name)
            metadata_path = container_path / "LCContainerInfo.plist"
            try:
                metadata = load_plist(metadata_path)
            except Exception:
                data_without_metadata += 1
                continue
            if isinstance(metadata, dict) and metadata.get("appIdentifier"):
                app_id = str(metadata["appIdentifier"])
                data_apps[app_id] = data_apps.get(app_id, 0) + 1

    guest_bundles = {guest["bundle_id"] for guest in guests}
    data_only_apps = sorted(bundle for bundle in data_apps if bundle not in guest_bundles)
    unreferenced_count = len(data_folder_names - referenced_data_folders)
    result: dict[str, Any] = {
        "combined_sidestore_present": side_root.is_dir(),
        "guests": sorted(guests, key=lambda item: (str(item["name"]).lower(), item["bundle_id"])),
        "data_summary": {
            "container_directories": data_directories,
            "without_metadata": data_without_metadata,
            "not_referenced_by_current_guest_metadata": unreferenced_count,
            "data_for_apps_without_a_guest_bundle": data_only_apps,
            "containers_per_app": dict(sorted(data_apps.items())),
        },
    }
    if side_root.is_dir():
        result["built_in_sidestore"] = scan_sidestore_root(side_root, udid)
    return result


def app_kind(app: dict[str, Any]) -> str:
    canonical = canonical_bundle_id(app).lower()
    actual = str(app.get("CFBundleIdentifier") or "").lower()
    value = canonical + " " + actual
    if "livecontainer" in value:
        return "livecontainer"
    if "sidestore" in value:
        return "sidestore"
    return "app"


def scan_device(
    udid: str,
    connection: str,
    policy: dict[str, Any],
) -> dict[str, Any]:
    network = connection == "network"
    issues: list[str] = []
    info, info_error = read_device_info(udid, network)
    if info_error:
        issues.append(info_error)

    public_apps, private_apps, app_error = list_native_apps(udid, network, policy)
    if app_error:
        issues.append(app_error)

    livecontainers = []
    sidestores = []
    if network:
        issues.append("network transport cannot perform read-only ifuse container discovery")
    else:
        for app in private_apps:
            kind = app_kind(app)
            if kind not in ("livecontainer", "sidestore"):
                continue
            actual_bundle = str(app.get("CFBundleIdentifier") or "")
            container_issues: list[str] = []
            with mounted_container(udid, actual_bundle, container_issues) as mounted:
                if mounted is not None:
                    if kind == "livecontainer":
                        scanned = scan_livecontainer(mounted, udid, policy)
                        scanned["host_bundle_id"] = canonical_bundle_id(app)
                        scanned["host_version"] = app.get("CFBundleShortVersionString")
                        livecontainers.append(scanned)
                    else:
                        scanned = scan_sidestore_root(mounted, udid)
                        scanned["bundle_id"] = canonical_bundle_id(app)
                        scanned["version"] = app.get("CFBundleShortVersionString")
                        sidestores.append(scanned)
            issues.extend(container_issues)

    return {
        "fingerprint": device_fingerprint(udid),
        "connection": connection,
        "device": info,
        "host_pairing": validate_host_pairing(udid),
        "developer_signed_native_apps": public_apps,
        "free_account_reference": {
            "observed_native_app_count": len(public_apps),
            "native_app_limit_including_manager": 3,
            "new_app_id_limit_per_7_days": 10,
            "note": "limits apply only when the signing Apple Account is free",
        },
        "livecontainers": livecontainers,
        "standalone_sidestore_instances": sidestores,
        "issues": sorted(set(issue for issue in issues if issue)),
    }


def inventory_map(snapshot: dict[str, Any]) -> dict[str, str | None]:
    mapped: dict[str, str | None] = {}
    for device in snapshot.get("devices", []):
        prefix = str(device.get("fingerprint") or "unknown")
        for app in device.get("developer_signed_native_apps", []):
            key = f"{prefix}:native:{app.get('bundle_id')}"
            mapped[key] = str(app.get("version") or app.get("build") or "")
        for host in device.get("livecontainers", []):
            for app in host.get("guests", []):
                key = f"{prefix}:guest:{app.get('bundle_id')}"
                mapped[key] = str(app.get("version") or app.get("build") or "")
    return mapped


def diff_snapshot(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    if not previous:
        return {"baseline": "created", "added": [], "removed": [], "version_changed": []}
    current_map = inventory_map(current)
    previous_map = inventory_map(previous)
    added = sorted(key for key in current_map if key not in previous_map)
    removed = sorted(key for key in previous_map if key not in current_map)
    changed = [
        {"app": key, "from": previous_map[key], "to": current_map[key]}
        for key in sorted(current_map.keys() & previous_map.keys())
        if current_map[key] != previous_map[key]
    ]
    return {"baseline": "compared", "added": added, "removed": removed, "version_changed": changed}


def read_cache(path: Path) -> dict[str, Any] | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) and parsed.get("schema_version") == SCHEMA_VERSION else None


def write_cache(path: Path, snapshot: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def print_human(snapshot: dict[str, Any]) -> None:
    print(f"sideload status | {snapshot.get('source')} | {snapshot.get('collected_at')}")
    warning = snapshot.get("warning")
    if warning:
        print(f"warning: {warning}")
    for device in snapshot.get("devices", []):
        details = device.get("device", {})
        product = details.get("product_type") or "unknown model"
        ios = details.get("ios_version") or "unknown iOS"
        print(f"\ndevice {device['fingerprint']} | {product} | iOS {ios} | {device['connection']}")
        print(f"host pairing: {device.get('host_pairing', {}).get('status', 'unknown')}")
        native = device.get("developer_signed_native_apps", [])
        print(f"developer-signed native apps: {len(native)}")
        for app in native:
            version = app.get("version") or "?"
            build = app.get("build") or "?"
            print(f"  - {app['name']} {version} ({build}) | {app['bundle_id']}")
        for host in device.get("livecontainers", []):
            print(
                f"LiveContainer {host.get('host_version') or '?'} | "
                f"combined SideStore: {'yes' if host.get('combined_sidestore_present') else 'no'}"
            )
            for guest in host.get("guests", []):
                print(
                    f"  - guest: {guest['name']} {guest.get('version') or '?'} "
                    f"({guest.get('build') or '?'}) | {guest['bundle_id']}"
                )
            data = host.get("data_summary", {})
            print(
                "  data: "
                f"{data.get('container_directories', 0)} containers, "
                f"{data.get('not_referenced_by_current_guest_metadata', 0)} not referenced"
            )
            built_in = host.get("built_in_sidestore", {})
            pairing = built_in.get("pairing_file", {}).get("status")
            if pairing:
                print(f"  built-in SideStore pairing: {pairing}")
            for signal in built_in.get("recent_logs", {}).get("signals", []):
                print(f"  log signal: {signal['severity']} {signal['code']}")
        for issue in device.get("issues", []):
            print(f"issue: {issue}")
    changes = snapshot.get("changes", {})
    if changes:
        print(
            "\nchanges: "
            f"+{len(changes.get('added', []))} "
            f"-{len(changes.get('removed', []))} "
            f"~{len(changes.get('version_changed', []))}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--device", help="scan one exact device; the value is never emitted or cached")
    parser.add_argument(
        "--network",
        action="store_true",
        help="include network-attached devices; container discovery still requires USB",
    )
    parser.add_argument("--no-cache", action="store_true", help="do not read or update sanitized status cache")
    args = parser.parse_args()

    cache_path = state_file()
    previous = None if args.no_cache else read_cache(cache_path)

    missing_tools = [
        tool for tool in ("idevice_id", "ideviceinfo", "idevicepair", "ideviceinstaller") if not shutil.which(tool)
    ]
    devices = [] if missing_tools else discover_device_ids(args.network, args.device)
    if not devices:
        if previous and not args.no_cache:
            cached = dict(previous)
            cached["source"] = "cached"
            cached["warning"] = (
                "live scan unavailable"
                + (f"; missing tools: {', '.join(missing_tools)}" if missing_tools else "; no device detected")
            )
            if args.json:
                print(json.dumps(cached, indent=2, sort_keys=True))
            else:
                print_human(cached)
            return 0
        failure = {
            "schema_version": SCHEMA_VERSION,
            "source": "unavailable",
            "collected_at": iso_utc(utc_now()),
            "devices": [],
            "error": (
                f"missing required tools: {', '.join(missing_tools)}" if missing_tools else "no device detected"
            ),
        }
        if args.json:
            print(json.dumps(failure, indent=2, sort_keys=True))
        else:
            print_human(failure)
            print("error: " + failure["error"])
        return 2

    policy = load_policy()
    snapshot: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source": "live",
        "collected_at": iso_utc(utc_now()),
        "devices": [scan_device(udid, connection, policy) for udid, connection in devices],
        "privacy": {
            "sanitized": True,
            "excluded": [
                "full device identifiers",
                "Apple Account identities",
                "signing-team identifiers",
                "pairing-record contents",
                "certificates and private keys",
                "raw SideStore logs",
            ],
        },
    }
    snapshot["changes"] = diff_snapshot(snapshot, previous)
    if not args.no_cache:
        write_cache(cache_path, snapshot)

    if args.json:
        print(json.dumps(snapshot, indent=2, sort_keys=True, default=str))
    else:
        print_human(snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
