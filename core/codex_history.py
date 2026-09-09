"""Resolve native Codex shared-history prefixes without following arbitrary paths."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID


class HistoryUnavailable(ValueError):
    """A referenced history cannot be reconstructed faithfully."""


def history_segments(path: Path, records: list[dict]):
    """Yield ancestor segments followed by this file; preserve per-file formats."""
    root = next((p.parent for p in path.absolute().parents
                 if p.name in {"sessions", "archived_sessions"}), None)
    yield from _segments(path.resolve(), records, root, set())


def _segments(path, records, root, seen):
    if path in seen or len(seen) >= 64:
        raise HistoryUnavailable("Codex history ancestry is cyclic or too deep")
    seen = seen | {path}
    metas = [r.get("payload") for r in records if r.get("type") == "session_meta"]
    references = [m for m in metas if isinstance(m, dict) and m.get("history_base") is not None]
    if references:
        if len(metas) != 1 or root is None or not path.is_relative_to(root.resolve()):
            raise HistoryUnavailable("Codex shared history has ambiguous metadata or storage")
        base = references[0]["history_base"]
        if not isinstance(base, dict):
            raise HistoryUnavailable("Codex history reference is malformed")
        sid, offset, end = (base.get(k) for k in ("thread_id", "end_byte_offset", "end_ordinal_exclusive"))
        try:
            valid_id = isinstance(sid, str) and str(UUID(sid)) == sid
        except ValueError:
            valid_id = False
        if not valid_id or type(offset) is not int or offset <= 0 or type(end) is not int or end <= 0:
            raise HistoryUnavailable("Codex history reference has invalid bounds or identity")
        candidates = [p for directory in (root / "sessions", root / "archived_sessions")
                      for p in directory.rglob(f"*{sid}.jsonl")]
        if len(candidates) != 1:
            raise HistoryUnavailable("Codex history ancestor is missing or ambiguous")
        parent = candidates[0].resolve()
        if not parent.is_relative_to(root.resolve()):
            raise HistoryUnavailable("Codex history ancestor escapes its store")
        prefix = []
        try:
            with parent.open("rb") as stream:
                if offset > parent.stat().st_size:
                    raise HistoryUnavailable("Codex history ancestor is truncated")
                remaining = offset
                previous = -1
                while remaining:
                    line = stream.readline(remaining)
                    remaining -= len(line)
                    if not line or not line.endswith(b"\n"):
                        raise HistoryUnavailable("Codex history boundary splits a record")
                    record = json.loads(line)
                    ordinal = record.get("ordinal") if isinstance(record, dict) else None
                    if type(ordinal) is not int or not previous < ordinal < end:
                        raise HistoryUnavailable("Codex history ordinals contradict its boundary")
                    previous = ordinal
                    prefix.append(record)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise HistoryUnavailable("Codex history ancestor cannot be read") from error
        parent_meta = [r.get("payload") for r in prefix if r.get("type") == "session_meta"]
        if len(parent_meta) != 1 or not isinstance(parent_meta[0], dict):
            raise HistoryUnavailable("Codex history ancestor metadata is missing or ambiguous")
        if parent_meta[0].get("id", parent_meta[0].get("session_id")) != sid:
            raise HistoryUnavailable("Codex history ancestor identity does not match")
        yield from _segments(parent, prefix, root, seen)
    yield records
