"""Validate or install Serena's local runtime wiring."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.runtime_manifest import DEFAULT_MANIFEST, expand_path, load_manifest


def _check(name: str, ok: bool, detail: str, *, required: bool = True) -> dict[str, Any]:
    return {"name": name, "ok": ok, "required": required, "detail": detail}


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def run_doctor(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    repo_root: Path | None = None,
    source_only: bool = False,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    root = (repo_root or manifest_path.resolve().parents[1]).resolve()
    checks: list[dict[str, Any]] = []

    for value in manifest["portable_source"]:
        path = expand_path(value, root)
        checks.append(_check(f"source:{value}", path.exists(), str(path)))

    required_python = str(manifest["project"]["python"])
    version_ok = sys.version_info >= (3, 10)
    checks.append(
        _check(
            "python-version",
            version_ok,
            f"{sys.version.split()[0]} required {required_python}",
        )
    )

    for command in manifest["required_commands"]:
        executable = shutil.which(command)
        checks.append(_check(f"command:{command}", bool(executable), executable or "missing"))

    original_path = list(sys.path)
    try:
        sys.path.insert(0, str(root))
        for module in manifest["required_python_imports"]:
            try:
                available = importlib.util.find_spec(module) is not None
            except (ImportError, ModuleNotFoundError, AttributeError, ValueError) as exc:
                checks.append(_check(f"import:{module}", False, str(exc)))
            else:
                checks.append(
                    _check(f"import:{module}", available, "available" if available else "missing")
                )
    finally:
        sys.path[:] = original_path

    if not source_only:
        config_root = Path("~/.config/serena").expanduser()
        checks.append(
            _check(
                "runtime:config-directory",
                config_root.is_dir() and _mode(config_root) == 0o700,
                f"{config_root} mode {oct(_mode(config_root)) if config_root.exists() else 'missing'}",
            )
        )
        if config_root.is_dir():
            insecure = [
                str(path)
                for path in config_root.rglob("*")
                if path.is_file() and _mode(path) & 0o077
            ]
            checks.append(
                _check(
                    "runtime:private-file-modes",
                    not insecure,
                    "all files are private"
                    if not insecure
                    else f"{len(insecure)} files are group/world accessible",
                )
            )

        for value in manifest["auth_paths"]:
            path = expand_path(value, root)
            checks.append(_check(f"auth:{path.name}", path.is_file(), str(path)))

        for value in manifest["model_paths"]:
            path = expand_path(value, root)
            checks.append(
                _check(
                    f"models:{value}",
                    path.exists(),
                    str(path),
                    required=False,
                )
            )

        service_root = Path("~/.config/systemd/user").expanduser()
        source_service_root = root / "systemd"
        required_units = set(manifest["services"]["always_on"]) | set(
            manifest["services"]["activation_only"]
        )
        for source in sorted(source_service_root.iterdir() if source_service_root.is_dir() else []):
            if not source.is_file() or source.suffix not in {".service", ".timer", ".path"}:
                continue
            installed = service_root / source.name
            linked = installed.is_symlink() and installed.resolve() == source.resolve()
            matching_copy = (
                installed.is_file()
                and not installed.is_symlink()
                and installed.read_bytes() == source.read_bytes()
            )
            checks.append(
                _check(
                    f"service:{source.name}",
                    linked or matching_copy,
                    "linked"
                    if linked
                    else "matching copy"
                    if matching_copy
                    else "missing or drifted",
                    required=source.name in required_units,
                )
            )

    required_failures = [check for check in checks if check["required"] and not check["ok"]]
    return {
        "ok": not required_failures,
        "source_only": source_only,
        "repository": str(root),
        "checks": checks,
        "required_failures": len(required_failures),
        "warnings": sum(1 for check in checks if not check["required"] and not check["ok"]),
    }


def install_services(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    repo_root: Path | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    root = (repo_root or manifest_path.resolve().parents[1]).resolve()
    source_root = root / "systemd"
    destination_root = Path("~/.config/systemd/user").expanduser()
    if not source_root.is_dir():
        raise FileNotFoundError(f"systemd source directory is missing: {source_root}")

    sources = [
        path
        for path in sorted(source_root.iterdir())
        if path.is_file() and path.suffix in {".service", ".timer", ".path"}
    ]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = destination_root / ".serena-backup" / timestamp
    changes: list[dict[str, str]] = []

    if apply:
        destination_root.mkdir(parents=True, exist_ok=True)
    for source in sources:
        destination = destination_root / source.name
        if destination.is_symlink() and destination.resolve() == source.resolve():
            changes.append({"unit": source.name, "action": "unchanged", "target": str(source)})
            continue
        action = "link"
        if destination.exists() or destination.is_symlink():
            action = "replace"
        changes.append({"unit": source.name, "action": action, "target": str(source)})
        if not apply:
            continue
        if destination.exists() and not destination.is_symlink():
            backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(destination, backup_root / destination.name)
        temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(source)
        os.replace(temporary, destination)

    enabled: list[str] = []
    if apply:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        always_on = list(manifest["services"]["always_on"])
        if always_on:
            subprocess.run(["systemctl", "--user", "enable", *always_on], check=True)
            enabled = always_on

    return {
        "ok": True,
        "applied": apply,
        "repository": str(root),
        "changes": changes,
        "enabled": enabled,
        "backup": str(backup_root) if apply and backup_root.exists() else None,
    }


def _print_human(result: dict[str, Any]) -> None:
    if "checks" not in result:
        for change in result["changes"]:
            print(f"{change['action']:10} {change['unit']} -> {change['target']}")
        print("applied" if result["applied"] else "dry run")
        return
    for check in result["checks"]:
        status = "ok" if check["ok"] else "warn" if not check["required"] else "fail"
        print(f"{status:4} {check['name']}: {check['detail']}")
    print(
        f"doctor {'passed' if result['ok'] else 'failed'}: "
        f"{result['required_failures']} required failures, {result['warnings']} warnings"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--repo", type=Path)
    doctor_parser.add_argument("--source-only", action="store_true")
    doctor_parser.add_argument("--json", action="store_true")

    install_parser = subparsers.add_parser("install-services")
    install_parser.add_argument("--repo", type=Path)
    install_parser.add_argument("--apply", action="store_true")
    install_parser.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = run_doctor(
                manifest_path=args.manifest,
                repo_root=args.repo,
                source_only=args.source_only,
            )
        else:
            result = install_services(
                manifest_path=args.manifest,
                repo_root=args.repo,
                apply=args.apply,
            )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_human(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
