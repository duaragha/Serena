"""Bounded code discovery. Path guards precede every source-content read."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from core.computer_knowledge import _SECRET_PATTERNS, _SKIP_DIRECTORIES
from core.security_policy import SecurityPolicyError, redact_credentials, validate_protected_path

MAX_FILE_SIZE = 200 * 1024
_SKIP = _SKIP_DIRECTORIES | {".worktrees", ".next", ".pytest_cache", ".mypy_cache", ".ruff_cache", "venv"}
_LANG = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
         ".tsx": "typescript", ".go": "go", ".rs": "rust", ".java": "java", ".c": "c",
         ".h": "c", ".cpp": "cpp", ".cs": "csharp", ".rb": "ruby", ".sh": "shell",
         ".swift": "swift", ".kt": "kotlin", ".json": "json", ".md": "markdown",
         ".html": "html", ".css": "css", ".sql": "sql", ".yaml": "yaml", ".yml": "yaml"}
_IGNORE_CACHE: dict[str, tuple[tuple[int, int], list[re.Pattern]]] = {}


@dataclass
class Source:
    path: Path
    size: int
    mtime: float
    lang: str | None
    text: str | None = None
    content_hash: str = ""


@dataclass
class Scan:
    files: dict[str, Source] = field(default_factory=dict)
    states: dict[str, tuple[int, float, str]] = field(default_factory=dict)
    skipped: Counter = field(default_factory=Counter)
    read: int = 0
    ignore_states: dict = field(default_factory=dict)


def _glob(pattern: str) -> str:
    """Translate the scoped Git glob subset; a single star never crosses /."""
    out = ""
    i = 0
    while i < len(pattern):
        if pattern[i:i + 3] == "**/":
            out += "(?:.*/)?"
            i += 3
        elif pattern[i:i + 2] == "**":
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return out


def _rules(directory: Path, previous=None, collected=None, root=None) -> list[re.Pattern]:
    path = directory / ".gitignore"
    if path.is_symlink():
        return []
    try:
        st = path.stat()
    except FileNotFoundError:
        return []
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_SIZE:
        raise OSError(f"Cannot safely read ignore rules: {path}")
    signature = (st.st_size, st.st_mtime_ns)
    rel = path.relative_to(root).as_posix() if root else str(path)
    persisted = (previous or {}).get(rel)
    if persisted and tuple(persisted[:2]) == signature:
        rules = [re.compile(pattern) for pattern in persisted[2]]
        if collected is not None:
            collected[rel] = persisted
        return rules
    cached = _IGNORE_CACHE.get(str(path))
    if cached and cached[0] == signature:
        if collected is not None:
            collected[rel] = [*signature, [p.pattern for p in cached[1]]]
        return cached[1]
    rules = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        value = raw.rstrip()
        if not value or value.startswith(("#", "!")):
            continue
        directory_only = value.endswith("/")
        value = value.rstrip("/")
        anchored = "/" in value
        value = value.lstrip("/")
        prefix = "^" if anchored else "(?:^|.*/)"
        suffix = "/$" if directory_only else "/?$"
        rules.append(re.compile(prefix + _glob(value) + suffix))
    _IGNORE_CACHE[str(path)] = (signature, rules)
    if collected is not None:
        collected[rel] = [*signature, [p.pattern for p in rules]]
    return rules


def _mask(pattern, replacement: str, text: str) -> str:
    """Substitute, re-emitting every newline the match swallowed.

    Line offsets are the whole point of a citation, so no redaction pass may
    ever shorten the file it rewrites.
    """
    return re.sub(pattern, lambda m: replacement + "\n" * m.group().count("\n"), text)


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = _mask(pattern, "[REDACTED]", text)
    text = _mask(r"\b(?:sk-(?:ant-|proj-)?|ghp_|github_pat_)[A-Za-z0-9_-]{16,}", "[REDACTED]", text)
    text = _mask(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [REDACTED]", text)
    text = _mask(r"(?i)\b(?:postgres(?:ql)?|mysql)://[^\s\"']+", "[REDACTED]", text)
    # The shared helper also matches across newlines; feed it one line at a time
    # so its fixed replacements can never collapse the surrounding source.
    return "\n".join(redact_credentials(line) for line in text.split("\n"))


def read_source(path: Path) -> tuple[str | None, str, str]:
    """Return redacted text, raw content hash, rejection reason; one bounded read."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        st = os.fstat(stream.fileno())
        if not stat.S_ISREG(st.st_mode):
            return None, "", "special"
        if st.st_size > MAX_FILE_SIZE:
            return None, "", "oversize"
        probe = stream.read(8192)
        if b"\0" in probe:
            return None, "", "binary"
        data = probe + stream.read(MAX_FILE_SIZE + 1 - len(probe))
    if len(data) > MAX_FILE_SIZE:
        return None, "", "oversize"
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if lines and sum(map(len, lines)) / len(lines) > 500:
        return None, "", "minified"
    return redact(text), hashlib.sha256(data).hexdigest(), ""


def scan_repo(root: Path, previous: dict | None = None, *, force: bool = False,
              previous_ignores: dict | None = None) -> Scan:
    """Discover a repo; cached accepted and rejected files need only stat calls.

    Traversal errors propagate: an incomplete discovered set must never zombie-prune.
    .gitignore rules are stat-cached, with optional persisted state across processes.
    """
    root = root.resolve(strict=True)
    previous = previous or {}
    result = Scan()

    def walk(directory, inherited):
        rules = [*inherited, (directory, _rules(directory, previous_ignores,
                                               result.ignore_states, root))]
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                path = Path(entry.path)
                if entry.is_symlink():
                    result.skipped["symlink"] += 1
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                if entry.name in _SKIP or entry.name.startswith(".env"):
                    result.skipped["protected"] += 1
                    continue
                try:
                    validate_protected_path(path, allowed_roots=(root,))
                except SecurityPolicyError:
                    result.skipped["protected"] += 1
                    continue
                if any(p.search(path.relative_to(base).as_posix() + ("/" if is_dir else ""))
                       for base, patterns in rules for p in patterns):
                    result.skipped["ignored"] += 1
                    continue
                if is_dir:
                    walk(path, rules)
                    continue
                if entry.name == ".gitignore":
                    continue
                st = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode):
                    result.skipped["special"] += 1
                    continue
                rel = path.relative_to(root).as_posix()
                source = Source(path, st.st_size, st.st_mtime, _LANG.get(path.suffix.lower()))
                signature = (source.size, source.mtime)
                old = previous.get(rel)
                if not force and old and tuple(old[:2]) == signature:
                    reason = old[2]
                elif source.size > MAX_FILE_SIZE:
                    reason = "oversize"
                else:
                    source.text, source.content_hash, reason = read_source(path)
                    result.read += 1
                    after = path.stat()
                    if (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                        raise OSError(f"source changed during scan: {rel}")
                result.states[rel] = (*signature, reason)
                if reason:
                    result.skipped[reason] += 1
                else:
                    result.files[rel] = source

    walk(root, [])
    return result
