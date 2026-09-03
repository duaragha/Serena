#!/usr/bin/env python3
"""Inspect an IPA without extracting or modifying it."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import plistlib
import stat
import struct
import sys
from typing import Any
import zipfile


MAX_INFO_PLIST_BYTES = 4 * 1024 * 1024
MAX_EXECUTABLE_INSPECTION_BYTES = 512 * 1024 * 1024

MH_MAGIC = b"\xfe\xed\xfa\xce"
MH_CIGAM = b"\xce\xfa\xed\xfe"
MH_MAGIC_64 = b"\xfe\xed\xfa\xcf"
MH_CIGAM_64 = b"\xcf\xfa\xed\xfe"
FAT_MAGIC = b"\xca\xfe\xba\xbe"
FAT_CIGAM = b"\xbe\xba\xfe\xca"
FAT_MAGIC_64 = b"\xca\xfe\xba\xbf"
FAT_CIGAM_64 = b"\xbf\xba\xfe\xca"
LC_ENCRYPTION_INFO = 0x21
LC_ENCRYPTION_INFO_64 = 0x2C

CPU_NAMES = {
    7: "x86",
    12: "arm",
    0x01000007: "x86_64",
    0x0100000C: "arm64",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def zip_mode(info: zipfile.ZipInfo) -> int:
    return (info.external_attr >> 16) & 0xFFFF


def is_executable(info: zipfile.ZipInfo) -> bool:
    mode = zip_mode(info)
    return bool(mode & 0o111)


def safe_archive_path(name: str) -> bool:
    if not name or name.startswith(("/", "\\")) or "\\" in name:
        return False
    path = PurePosixPath(name)
    return ".." not in path.parts


def read_small(zf: zipfile.ZipFile, name: str, maximum: int = MAX_INFO_PLIST_BYTES) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > maximum:
        raise ValueError(f"archive member exceeds {maximum} bytes")
    return zf.read(info)


def parse_plist_member(zf: zipfile.ZipFile, name: str) -> dict[str, Any]:
    parsed = plistlib.loads(read_small(zf, name))
    if not isinstance(parsed, dict):
        raise ValueError("plist root is not a dictionary")
    return parsed


def decode_embedded_profile(raw: bytes) -> dict[str, Any] | None:
    start = raw.find(b"<?xml")
    end = raw.rfind(b"</plist>")
    if start < 0 or end < start:
        return None
    try:
        parsed = plistlib.loads(raw[start : end + len(b"</plist>")])
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def cpu_name(value: int) -> str:
    return CPU_NAMES.get(value & 0xFFFFFFFF, f"cpu-{value & 0xFFFFFFFF:#x}")


def parse_thin_macho(data: bytes, offset: int, size: int) -> dict[str, Any]:
    if offset < 0 or size < 4 or offset + size > len(data):
        return {"format": "invalid-slice", "encrypted": None}
    magic = data[offset : offset + 4]
    if magic == MH_CIGAM:
        endian, is_64 = "<", False
    elif magic == MH_CIGAM_64:
        endian, is_64 = "<", True
    elif magic == MH_MAGIC:
        endian, is_64 = ">", False
    elif magic == MH_MAGIC_64:
        endian, is_64 = ">", True
    else:
        return {"format": "not-mach-o", "encrypted": None}

    header_size = 32 if is_64 else 28
    if size < header_size:
        return {"format": "truncated-mach-o", "encrypted": None}
    try:
        cpu_type = struct.unpack_from(endian + "I", data, offset + 4)[0]
        command_count = struct.unpack_from(endian + "I", data, offset + 16)[0]
        commands_size = struct.unpack_from(endian + "I", data, offset + 20)[0]
    except struct.error:
        return {"format": "truncated-mach-o", "encrypted": None}

    maximum_end = min(offset + size, offset + header_size + commands_size)
    cursor = offset + header_size
    cryptids: list[int] = []
    malformed = False
    for _ in range(min(command_count, 100_000)):
        if cursor + 8 > maximum_end:
            malformed = True
            break
        command, command_size = struct.unpack_from(endian + "II", data, cursor)
        if command_size < 8 or cursor + command_size > maximum_end:
            malformed = True
            break
        if (command & 0x7FFFFFFF) in (LC_ENCRYPTION_INFO, LC_ENCRYPTION_INFO_64):
            if command_size >= 20:
                cryptids.append(struct.unpack_from(endian + "I", data, cursor + 16)[0])
            else:
                malformed = True
        cursor += command_size

    encrypted: bool | None
    if cryptids:
        encrypted = any(value != 0 for value in cryptids)
    else:
        encrypted = False
    return {
        "format": "mach-o-64" if is_64 else "mach-o-32",
        "architecture": cpu_name(cpu_type),
        "encrypted": encrypted,
        "encryption_commands": len(cryptids),
        "load_commands_malformed": malformed,
    }


def parse_macho(data: bytes) -> list[dict[str, Any]]:
    if len(data) < 8:
        return [{"format": "truncated", "encrypted": None}]
    magic = data[:4]
    if magic not in (FAT_MAGIC, FAT_CIGAM, FAT_MAGIC_64, FAT_CIGAM_64):
        return [parse_thin_macho(data, 0, len(data))]

    endian = ">" if magic in (FAT_MAGIC, FAT_MAGIC_64) else "<"
    is_64 = magic in (FAT_MAGIC_64, FAT_CIGAM_64)
    count = struct.unpack_from(endian + "I", data, 4)[0]
    record_size = 32 if is_64 else 20
    cursor = 8
    slices: list[dict[str, Any]] = []
    for _ in range(min(count, 128)):
        if cursor + record_size > len(data):
            slices.append({"format": "truncated-fat-header", "encrypted": None})
            break
        if is_64:
            cpu_type, _, arch_offset, arch_size, _, _ = struct.unpack_from(
                endian + "IIQQII", data, cursor
            )
        else:
            cpu_type, _, arch_offset, arch_size, _ = struct.unpack_from(
                endian + "IIIII", data, cursor
            )
        parsed = parse_thin_macho(data, int(arch_offset), int(arch_size))
        parsed.setdefault("architecture", cpu_name(cpu_type))
        slices.append(parsed)
        cursor += record_size
    return slices


def signable_roots(names: list[str], app_root: str) -> list[str]:
    suffixes = (".app", ".appex", ".framework")
    roots = {app_root.rstrip("/")}
    for name in names:
        parts = PurePosixPath(name).parts
        accumulated: list[str] = []
        for part in parts:
            accumulated.append(part)
            if part.endswith(suffixes):
                candidate = "/".join(accumulated)
                if candidate.startswith(app_root):
                    roots.add(candidate)
    return sorted(roots, key=lambda value: (value.count("/"), value))


def inspect(path: Path, expected_bundle: str | None, expected_version: str | None) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    result: dict[str, Any] = {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "valid_for_attempt": False,
        "errors": errors,
        "warnings": warnings,
    }

    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        errors.append(f"not a readable ZIP/IPA: {type(exc).__name__}")
        return result

    with zf:
        infos = zf.infolist()
        names = [info.filename for info in infos]
        counts = Counter(names)
        duplicates = sorted(name for name, count in counts.items() if count > 1)
        if duplicates:
            errors.append(f"archive contains {len(duplicates)} duplicate paths")

        case_counts = Counter(name.casefold() for name in names)
        case_collisions = sum(1 for count in case_counts.values() if count > 1)
        if case_collisions:
            errors.append(f"archive contains {case_collisions} case-insensitive path collisions")

        unsafe = [name for name in names if not safe_archive_path(name)]
        if unsafe:
            errors.append(f"archive contains {len(unsafe)} unsafe paths")

        metadata_junk = [
            name for name in names if name.startswith("__MACOSX/") or "/._" in name or name.startswith("._")
        ]
        if metadata_junk:
            warnings.append(f"archive contains {len(metadata_junk)} AppleDouble/__MACOSX entries")

        symlinks = []
        unsafe_symlinks = []
        for info in infos:
            mode = zip_mode(info)
            if not stat.S_ISLNK(mode):
                continue
            symlinks.append(info.filename)
            try:
                target = zf.read(info).decode("utf-8", errors="replace")
            except Exception:
                target = ""
            if target.startswith(("/", "\\")) or ".." in PurePosixPath(target).parts:
                unsafe_symlinks.append(info.filename)
        if unsafe_symlinks:
            errors.append(f"archive contains {len(unsafe_symlinks)} escaping symlinks")

        info_members = [
            name
            for name in names
            if len(PurePosixPath(name).parts) == 3
            and PurePosixPath(name).parts[0] == "Payload"
            and PurePosixPath(name).parts[1].endswith(".app")
            and PurePosixPath(name).parts[2] == "Info.plist"
        ]
        if len(info_members) != 1:
            errors.append(f"expected one Payload/*.app/Info.plist, found {len(info_members)}")
            return result

        info_member = info_members[0]
        app_root = info_member.rsplit("/", 1)[0]
        try:
            info = parse_plist_member(zf, info_member)
        except Exception as exc:
            errors.append(f"main Info.plist is unreadable: {type(exc).__name__}: {exc}")
            return result

        bundle_id = str(info.get("CFBundleIdentifier") or "")
        version = str(info.get("CFBundleShortVersionString") or "")
        build = str(info.get("CFBundleVersion") or "")
        display_name = str(
            info.get("CFBundleDisplayName") or info.get("CFBundleName") or Path(app_root).stem
        )
        executable = str(info.get("CFBundleExecutable") or "")
        executable_member = f"{app_root}/{executable}" if executable else ""

        if not bundle_id:
            errors.append("CFBundleIdentifier is missing")
        if not executable:
            errors.append("CFBundleExecutable is missing")
        elif executable_member not in counts:
            errors.append("the declared main executable is missing")
        elif not is_executable(zf.getinfo(executable_member)):
            errors.append("the declared main executable lacks ZIP executable mode bits")

        if expected_bundle and bundle_id != expected_bundle:
            errors.append(f"bundle ID mismatch: expected {expected_bundle}, found {bundle_id}")
        if expected_version and version != expected_version:
            errors.append(f"version mismatch: expected {expected_version}, found {version or '[missing]'}")

        extension_infos = [
            name
            for name in names
            if name.startswith(f"{app_root}/PlugIns/")
            and name.endswith(".appex/Info.plist")
        ]
        extensions = []
        for extension_info in sorted(extension_infos):
            try:
                parsed = parse_plist_member(zf, extension_info)
            except Exception:
                extensions.append({"path": extension_info, "status": "unreadable-info-plist"})
                continue
            extensions.append(
                {
                    "name": parsed.get("CFBundleDisplayName") or parsed.get("CFBundleName"),
                    "bundle_id": parsed.get("CFBundleIdentifier"),
                    "version": parsed.get("CFBundleShortVersionString"),
                }
            )
        if extensions:
            warnings.append(
                f"app contains {len(extensions)} extension(s); native signing may consume App IDs and "
                "LiveContainer cannot register guest extensions"
            )

        background_modes = info.get("UIBackgroundModes")
        if isinstance(background_modes, list) and background_modes:
            warnings.append(
                "app declares background modes that may not work as a LiveContainer guest: "
                + ", ".join(str(value) for value in background_modes)
            )

        roots = signable_roots(names, app_root)
        signature_presence = {
            root: any(name.startswith(root + "/_CodeSignature/") for name in names) for root in roots
        }
        signed_count = sum(1 for present in signature_presence.values() if present)
        if signed_count == 0:
            signing_state = "unsigned"
        elif signed_count == len(signature_presence):
            signing_state = "signed"
        else:
            signing_state = "mixed"
            warnings.append("archive has mixed signature-directory presence across nested bundles")

        profile_member = f"{app_root}/embedded.mobileprovision"
        profile_summary: dict[str, Any] | None = None
        if profile_member in counts:
            try:
                profile = decode_embedded_profile(read_small(zf, profile_member, 16 * 1024 * 1024))
            except Exception:
                profile = None
            if profile:
                entitlements = profile.get("Entitlements")
                profile_summary = {
                    "expiration": str(profile.get("ExpirationDate")) if profile.get("ExpirationDate") else None,
                    "entitlement_names": (
                        sorted(str(key) for key in entitlements) if isinstance(entitlements, dict) else []
                    ),
                    "note": "SideStore replaces packaged provisioning during signing",
                }
            else:
                profile_summary = {"status": "present-but-not-decodable"}

        macho_slices: list[dict[str, Any]] = []
        if executable_member in counts:
            executable_info = zf.getinfo(executable_member)
            if executable_info.file_size <= MAX_EXECUTABLE_INSPECTION_BYTES:
                try:
                    macho_slices = parse_macho(zf.read(executable_info))
                except Exception as exc:
                    warnings.append(f"main executable inspection failed: {type(exc).__name__}")
            else:
                warnings.append("main executable is too large for bounded Mach-O inspection")

        encrypted_values = [item.get("encrypted") for item in macho_slices]
        encrypted = any(value is True for value in encrypted_values)
        if encrypted:
            errors.append("main executable still has an active FairPlay encryption command")
        if macho_slices and all(item.get("format") == "not-mach-o" for item in macho_slices):
            warnings.append("declared main executable was not recognized as Mach-O")

        total_uncompressed = sum(info.file_size for info in infos)
        total_compressed = sum(info.compress_size for info in infos)
        compression_ratio = round(total_uncompressed / max(total_compressed, 1), 2)
        if compression_ratio > 200:
            warnings.append(f"unusually high archive compression ratio: {compression_ratio}:1")

        result.update(
            {
                "app": {
                    "name": display_name,
                    "bundle_id": bundle_id,
                    "version": version,
                    "build": build,
                    "minimum_ios": info.get("MinimumOSVersion") or info.get("LSMinimumSystemVersion"),
                    "background_modes": background_modes if isinstance(background_modes, list) else [],
                    "extensions": extensions,
                },
                "archive": {
                    "members": len(infos),
                    "uncompressed_bytes": total_uncompressed,
                    "compression_ratio": compression_ratio,
                    "symlinks": len(symlinks),
                    "signable_bundles": len(roots),
                    "signature_state": signing_state,
                    "embedded_profile": profile_summary,
                },
                "main_executable": {
                    "member": executable_member,
                    "size_bytes": zf.getinfo(executable_member).file_size if executable_member in counts else None,
                    "macho_slices": macho_slices,
                    "fairplay_encrypted": encrypted,
                },
            }
        )
        result["valid_for_attempt"] = not errors
    return result


def print_human(result: dict[str, Any]) -> None:
    app = result.get("app", {})
    if app:
        print(
            f"{app.get('name')} | {app.get('bundle_id')} | "
            f"{app.get('version') or '?'} ({app.get('build') or '?'})"
        )
    print(f"IPA: {result.get('path')}")
    print(f"SHA-256: {result.get('sha256')}")
    print(f"valid for an install attempt: {'yes' if result.get('valid_for_attempt') else 'no'}")
    archive = result.get("archive", {})
    if archive:
        print(
            f"archive: {archive.get('members')} members | "
            f"{archive.get('signable_bundles')} signable bundles | "
            f"{archive.get('signature_state')}"
        )
    executable = result.get("main_executable", {})
    if executable:
        print(f"FairPlay encrypted: {'yes' if executable.get('fairplay_encrypted') else 'no'}")
    for error in result.get("errors", []):
        print(f"error: {error}")
    for warning in result.get("warnings", []):
        print(f"warning: {warning}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ipa", type=Path)
    parser.add_argument("--expected-bundle")
    parser.add_argument("--expected-version")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not args.ipa.is_file():
        failure = {
            "path": str(args.ipa),
            "valid_for_attempt": False,
            "errors": ["IPA path is not a file"],
            "warnings": [],
        }
        if args.json:
            print(json.dumps(failure, indent=2, sort_keys=True))
        else:
            print_human(failure)
        return 2

    result = inspect(args.ipa, args.expected_bundle, args.expected_version)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print_human(result)
    return 0 if result.get("valid_for_attempt") else 2


if __name__ == "__main__":
    raise SystemExit(main())
