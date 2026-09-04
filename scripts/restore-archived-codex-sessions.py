#!/usr/bin/env python3
"""Bring back Codex chats that survive only in Syncthing's version archive.

Syncthing keeps the previous bytes of every file it replaces or deletes under
``.stversions``. When a rollout is deleted on one machine that deletion syncs,
and the only remaining copy of the conversation is the archived one. Those
files are complete and readable, but the Codex CLI looks in its own tree and
has no record of the id, so ``codex resume`` answers

    ERROR: No saved session found with ID ...

Serena used to index the archive directly, which made the chats visible and
still unopenable. Copying them back into the live tree makes them real again:
the CLI can resume them, and the scanner finds them where it should.

Only sessions with no live copy are restored, and only interactive CLI ones --
an editor-extension session is not a chat and is usually enormous.

    python scripts/restore-archived-codex-sessions.py            # dry run
    python scripts/restore-archived-codex-sessions.py --apply
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
ARCHIVE_DIR = ".stversions"
FILENAME_RE = re.compile(
    r"rollout-.*?-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)


def _source(path: Path) -> str:
    """The originator recorded in the rollout's first line."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            payload = json.loads(handle.readline()).get("payload") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""
    source = payload.get("source")
    if isinstance(source, dict):
        return next(iter(source), "").lower()
    return str(source or "").lower()


def _split_tree() -> tuple[dict[str, Path], dict[str, Path]]:
    archived: dict[str, Path] = {}
    live: dict[str, Path] = {}
    for path in SESSIONS_ROOT.rglob("rollout-*.jsonl"):
        match = FILENAME_RE.search(path.name)
        if not match or not path.is_file():
            continue
        (archived if ARCHIVE_DIR in path.parts else live)[match.group(1)] = path
    return archived, live


def _live_destination(archived: Path) -> Path:
    """The same location with the archive directory taken out of the path."""
    parts = [part for part in archived.relative_to(SESSIONS_ROOT).parts if part != ARCHIVE_DIR]
    return SESSIONS_ROOT.joinpath(*parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually copy (default: dry run)")
    parser.add_argument(
        "--include-editor-sessions",
        action="store_true",
        help="also restore vscode/extension rollouts, which are not chats",
    )
    args = parser.parse_args()

    if not SESSIONS_ROOT.is_dir():
        print(f"no codex sessions at {SESSIONS_ROOT}")
        return 1

    archived, live = _split_tree()
    orphans = sorted(set(archived) - set(live))
    if not orphans:
        print("every archived rollout still has a live copy; nothing to restore")
        return 0

    restored = skipped = 0
    total_bytes = 0
    for sid in orphans:
        source_path = archived[sid]
        origin = _source(source_path)
        destination = _live_destination(source_path)
        size = source_path.stat().st_size

        if origin != "cli" and not args.include_editor_sessions:
            print(f"  skip    {sid[:8]}  source={origin or 'unknown'!r} ({size / 1e6:.0f} MB)")
            skipped += 1
            continue
        if destination.exists():
            print(f"  skip    {sid[:8]}  a live copy appeared at {destination}")
            skipped += 1
            continue

        print(f"  restore {sid[:8]}  {size / 1e6:>7.1f} MB  -> {destination.relative_to(SESSIONS_ROOT)}")
        total_bytes += size
        restored += 1
        if args.apply:
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Copy rather than move: the archive stays intact, so a bad restore
            # costs disk space and nothing else.
            shutil.copy2(source_path, destination)

    verb = "restored" if args.apply else "would restore"
    print(f"\n{verb} {restored} session(s), {total_bytes / 1e6:.0f} MB; skipped {skipped}")
    if not args.apply and restored:
        print("re-run with --apply to copy them, then: chats index --refresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
