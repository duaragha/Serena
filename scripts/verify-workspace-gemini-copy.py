"""Offline, isolated compatibility probe; never authenticates or starts a provider."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import uuid
from contextlib import closing
from pathlib import Path


def fingerprint(path: Path) -> tuple[int, int, str]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    source = args.source.absolute()
    if source.is_symlink() or source.resolve(strict=True) != source:
        raise ValueError("Source must not contain symlinks")
    if source.suffix != ".db" or str(uuid.UUID(source.stem)) != source.stem:
        raise ValueError("Expected a UUID-named native conversation database")
    sidecars = [Path(str(source) + suffix) for suffix in ("-wal", "-shm", "-journal")]
    if any(path.exists() for path in sidecars):
        raise ValueError("Choose an inactive conversation without SQLite sidecars")
    before = fingerprint(source)
    sys.path.insert(0, str(args.archive.resolve(strict=True)))
    from google3.cloud.developer_experience.antigravity_extensions.acp_server import session_store

    previous_home = os.environ.get("GEMINI_HOME")
    try:
        with tempfile.TemporaryDirectory(prefix="serena-gemini-copy-") as temporary:
            os.environ["GEMINI_HOME"] = temporary
            session_store.ensure_storage_directory()
            destination = Path(session_store.trajectory_path(source.stem))
            # Immutable read avoids creating SQLite sidecars in the original store.
            with (
                closing(sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)) as src,
                closing(sqlite3.connect(destination)) as dst,
            ):
                src.backup(dst)
            steps = session_store.load_session_steps(source.stem)
            if not steps or not all(step.ListFields() for _, step in steps):
                raise AssertionError("No populated native steps decoded")
            expected = []
            for index, step in steps:
                session_store.strip_thought_signatures(step)
                expected.append((index, step.SerializeToString(deterministic=True)))
            signatures = session_store.strip_thought_signatures_from_db(source.stem)
            actual = [(index, step.SerializeToString(deterministic=True))
                      for index, step in session_store.load_session_steps(source.stem)]
            assert actual == expected, "Resume cleanup changed non-signature conversation data"
            assert session_store.strip_thought_signatures_from_db(source.stem) == 0
            assert fingerprint(source) == before, "Source changed during probe"
            assert not any(path.exists() for path in sidecars), "Source became active during probe"
            print(json.dumps({"result": "pass", "steps": len(steps),
                              "signatures_removed_from_copy": signatures,
                              "original_unchanged": True, "provider_started": False,
                              "authenticated_resume_verified": False}))
        assert not Path(temporary).exists()
    finally:
        if previous_home is None:
            os.environ.pop("GEMINI_HOME", None)
        else:
            os.environ["GEMINI_HOME"] = previous_home


if __name__ == "__main__":
    main()
