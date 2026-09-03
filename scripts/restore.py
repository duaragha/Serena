"""Verify or restore a Serena private snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def restore_snapshot(
    snapshot: Path,
    *,
    repo_root: Path,
    home_root: Path,
    apply: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    snapshot = snapshot.expanduser().resolve()
    metadata_path = snapshot / "snapshot.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("schema_version") != 1:
        raise ValueError("unsupported snapshot schema")

    verified = 0
    planned: list[dict[str, str]] = []
    for record in metadata.get("records", []):
        archive_path = Path(str(record["archive_path"]))
        source = snapshot / archive_path
        if not source.is_file():
            raise FileNotFoundError(f"snapshot file is missing: {source}")
        expected_hash = record.get("sha256")
        if expected_hash and _sha256(source) != expected_hash:
            raise ValueError(f"snapshot checksum failed: {archive_path}")
        verified += 1

        parts = archive_path.parts
        if len(parts) < 3 or parts[0] != "files":
            raise ValueError(f"unsafe archive path: {archive_path}")
        if parts[1] == "repo":
            destination = repo_root.joinpath(*parts[2:])
        elif parts[1] == "home":
            destination = home_root.joinpath(*parts[2:])
        else:
            raise ValueError(f"external restore path requires manual handling: {archive_path}")
        destination = destination.resolve()
        allowed_root = repo_root.resolve() if parts[1] == "repo" else home_root.resolve()
        if destination != allowed_root and allowed_root not in destination.parents:
            raise ValueError(f"restore path escapes destination root: {destination}")

        if destination.exists() and not overwrite:
            action = "skip-existing"
        else:
            action = "restore"
            if apply:
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copy2(source, destination)
                destination.chmod(0o600)
        planned.append(
            {"archive": str(archive_path), "destination": str(destination), "action": action}
        )

    return {
        "ok": True,
        "applied": apply,
        "verified": verified,
        "planned": planned,
        "restored": sum(1 for item in planned if item["action"] == "restore"),
        "skipped": sum(1 for item in planned if item["action"] == "skip-existing"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = restore_snapshot(
            args.snapshot,
            repo_root=args.repo,
            home_root=args.home,
            apply=args.apply,
            overwrite=args.overwrite,
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
