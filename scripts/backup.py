"""Create a private, checksummed Serena state snapshot.

The default snapshot excludes credentials, frontier-model session histories,
and large local model caches. Those categories require explicit flags.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.runtime_manifest import DEFAULT_MANIFEST, expand_path, load_manifest

DEFAULT_OUTPUT = Path("~/.local/share/serena/backups").expanduser()


def _matches(path: Path, patterns: Iterable[str]) -> bool:
    text = path.as_posix()
    return any(
        fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(text, pattern)
        for pattern in patterns
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(source: Path, repo_root: Path) -> Path:
    resolved = source.expanduser().absolute()
    try:
        return Path("repo") / resolved.relative_to(repo_root.absolute())
    except ValueError:
        pass
    try:
        return Path("home") / resolved.relative_to(Path.home().absolute())
    except ValueError:
        return Path("external") / resolved.as_posix().lstrip("/")


def _copy_database(source: Path, destination: Path) -> bool:
    if source.suffix.lower() not in {".sqlite", ".sqlite3", ".db"}:
        return False
    try:
        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        destination_connection = sqlite3.connect(destination)
        with source_connection, destination_connection:
            source_connection.backup(destination_connection)
        source_connection.close()
        destination_connection.close()
        shutil.copystat(source, destination)
        return True
    except sqlite3.DatabaseError:
        destination.unlink(missing_ok=True)
        return False


def _copy_file(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_for_copy = source.resolve() if source.is_symlink() else source
    copied_as = "sqlite-backup" if _copy_database(source_for_copy, destination) else "file"
    if copied_as == "file":
        shutil.copy2(source_for_copy, destination)
    destination.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return copied_as


def _iter_files(root: Path, transient: list[str]) -> Iterable[Path]:
    if root.is_file() or (root.is_symlink() and root.resolve().is_file()):
        yield root
        return
    if not root.is_dir():
        return
    for directory, names, files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        names[:] = [name for name in names if not _matches(directory_path / name, transient)]
        for name in files:
            path = directory_path / name
            if not _matches(path, transient) and (
                path.is_file() or (path.is_symlink() and path.resolve().is_file())
            ):
                yield path


def create_snapshot(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    output_root: Path = DEFAULT_OUTPUT,
    include_secrets: bool = False,
    include_sessions: bool = False,
    include_models: bool = False,
    include_auth: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    repo_root = manifest_path.resolve().parents[1]
    created = now or datetime.now(timezone.utc)
    timestamp = created.strftime("%Y%m%dT%H%M%SZ")
    snapshot_root = output_root.expanduser() / f"serena-{timestamp}"
    files_root = snapshot_root / "files"
    if not dry_run:
        snapshot_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    sensitive_patterns = manifest["sensitive_names"]
    transient_patterns = manifest["transient_names"]

    logical_roots: list[tuple[str, Path]] = []
    for value in manifest["private_repository_paths"]:
        logical_roots.append(("private", expand_path(value, repo_root)))
    for value in manifest["runtime_paths"]:
        logical_roots.append(("runtime", expand_path(value, repo_root)))
    if include_sessions:
        for value in manifest["session_paths"]:
            logical_roots.append(("sessions", expand_path(value, repo_root)))
    if include_models:
        for value in manifest["model_paths"]:
            logical_roots.append(("models", expand_path(value, repo_root)))
    if include_auth:
        if not include_secrets:
            raise ValueError("--include-auth requires --include-secrets")
        for value in manifest["auth_paths"]:
            logical_roots.append(("auth", expand_path(value, repo_root)))

    model_roots = [expand_path(value, repo_root).absolute() for value in manifest["model_paths"]]
    seen: set[Path] = set()
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    skipped_sensitive = 0
    skipped_models = 0

    for category, root in logical_roots:
        if not root.exists() and not root.is_symlink():
            missing.append(str(root))
            continue
        for source in _iter_files(root, transient_patterns):
            absolute = source.absolute()
            if absolute in seen:
                continue
            seen.add(absolute)
            is_model = any(absolute == model or model in absolute.parents for model in model_roots)
            if is_model and not include_models:
                skipped_models += 1
                continue
            sensitive = _matches(source, sensitive_patterns)
            if sensitive and not include_secrets:
                skipped_sensitive += 1
                continue
            relative = _safe_relative(source, repo_root)
            destination = files_root / relative
            record: dict[str, Any] = {
                "category": category,
                "source": str(source),
                "archive_path": str(Path("files") / relative),
                "sensitive": sensitive,
            }
            if dry_run:
                record["size"] = source.resolve().stat().st_size
                record["copy_mode"] = "dry-run"
            else:
                record["copy_mode"] = _copy_file(source, destination)
                record["size"] = destination.stat().st_size
                record["sha256"] = _sha256(destination)
            records.append(record)

    summary: dict[str, Any] = {
        "schema_version": 1,
        "created_at": created.isoformat(),
        "repository": str(repo_root),
        "snapshot": str(snapshot_root),
        "dry_run": dry_run,
        "options": {
            "include_secrets": include_secrets,
            "include_sessions": include_sessions,
            "include_models": include_models,
            "include_auth": include_auth,
        },
        "records": records,
        "missing": sorted(set(missing)),
        "skipped_sensitive": skipped_sensitive,
        "skipped_models": skipped_models,
        "files": len(records),
        "bytes": sum(int(record.get("size", 0)) for record in records),
    }
    if not dry_run:
        (snapshot_root / "snapshot.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (snapshot_root / "snapshot.json").chmod(0o600)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--include-secrets", action="store_true")
    parser.add_argument("--include-sessions", action="store_true")
    parser.add_argument("--include-models", action="store_true")
    parser.add_argument("--include-auth", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = create_snapshot(
            manifest_path=args.manifest,
            output_root=args.output,
            include_secrets=args.include_secrets,
            include_sessions=args.include_sessions,
            include_models=args.include_models,
            include_auth=args.include_auth,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
