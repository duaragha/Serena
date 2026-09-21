"""Explicit repository registry and independently locked incremental code FTS."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
from contextlib import closing, contextmanager
from pathlib import Path

from core import code_scanner as scanner
from core.config import DATA_DIR, resolve_session_cwd
from core.projects import canonical_cwd, project_root

DB_PATH = DATA_DIR / "code-index.db"
SCHEMA_VERSION = 2
REGISTRY_PATH = Path(os.environ.get("SERENA_CODE_REPOS_PATH", Path.home() / ".config/serena/code-repos.json"))
_LOCK = threading.RLock()
_REGISTRY_CACHE: dict = {}


def load_registry() -> dict[str, str]:
    path = Path(REGISTRY_PATH)
    try:
        st = path.stat()
    except FileNotFoundError:
        return {}
    signature = (str(path), st.st_size, st.st_mtime_ns)
    if _REGISTRY_CACHE.get("signature") == signature:
        return dict(_REGISTRY_CACHE["data"])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError("Cannot read code repository registry") from exc
    if not isinstance(data, dict) or any(
        not isinstance(k, str) or not k.strip() or not isinstance(v, str) or not v.strip()
        or "\n" in k or "\r" in k for k, v in data.items()
    ):
        raise ValueError("Code registry must map nonempty repo keys to root paths")
    _REGISTRY_CACHE.update(signature=signature, data=dict(data))
    return data


def _save_registry(data):
    path = Path(REGISTRY_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".code-repos-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    _REGISTRY_CACHE.clear()


def portable_key(value: str) -> str:
    """Stable project-layout key, without folding per-ticket clones/worktrees."""
    value = str(value).replace("\\", "/").rstrip("/")
    home = str(Path.home()).replace("\\", "/").rstrip("/")
    if value.startswith(home + "/"):
        value = "~" + value[len(home):]
    value = re.sub(r"^(?:[A-Za-z]:/Users/[^/]+|/(?:home|Users)/[^/]+)(?=/)", "~", value)
    if value.startswith("~/Projects/"):
        value = "~/Documents/Projects/" + value[len("~/Projects/"):]
    return value


def layout_forms(key: str) -> set[str]:
    """Portable spellings the cross-OS layout translator may legitimately return.

    The Windows layout is flat (``~/Projects/x``) while this machine may keep the
    same checkout nested under ``personal_projects``. Both spellings name the very
    same repository, so both are accepted; nothing shorter ever is.
    """
    parts = key.split("/")
    forms = {key}
    if parts[:3] == ["~", "Documents", "Projects"] and len(parts) > 3:
        tail = parts[3:]
        folded = tail[1:] if tail[0] == "personal_projects" else ["personal_projects", *tail]
        if folded:
            forms.add("/".join([*parts[:3], *folded]))
    return forms


def resolve_root(value: str) -> Path:
    canonical = canonical_cwd(value)
    native = Path(canonical).expanduser()
    foreign = (canonical.startswith("/") if sys.platform == "win32"
               else bool(re.match(r"^[A-Za-z]:/", canonical)))
    if foreign or not native.is_dir():
        translated = resolve_session_cwd(canonical)
        # The session helper deliberately falls back to home or a parent. A code
        # scanner accepts a complete layout translation but must never broaden
        # its root to an ancestor when the checkout itself is missing.
        if portable_key(translated) not in layout_forms(portable_key(canonical)):
            raise ValueError(f"Repository unavailable: {value}")
        native = Path(translated)
    native = native.resolve(strict=True)
    if not (native / ".git").exists():
        raise ValueError(f"Not a repository root: {value}")
    # Consult the shared project identity validator, but retain explicit clone
    # paths: its sidebar-specific sibling folding is intentionally not applied.
    if not project_root(str(native)):
        raise ValueError(f"Invalid project root: {value}")
    return native


@contextmanager
def _update_lock(skip_if_running=False):
    acquired = _LOCK.acquire(blocking=not skip_if_running)
    if not acquired:
        yield False
        return
    try:
        path = Path(DB_PATH).with_suffix(".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as stream:
            if os.name == "nt":
                import msvcrt
                if stream.seek(0, 2) == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK if skip_if_running else msvcrt.LK_LOCK, 1)
                except OSError:
                    if not skip_if_running:
                        raise
                    yield False
                    return
                try:
                    yield True
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | (fcntl.LOCK_NB if skip_if_running else 0))
                except BlockingIOError:
                    yield False
                    return
                try:
                    yield True
                finally:
                    fcntl.flock(stream, fcntl.LOCK_UN)
    finally:
        _LOCK.release()


def _get_db() -> sqlite3.Connection:
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    # Rollback journaling permits truly read-only opens without WAL/SHM sidecar
    # creation, and avoids lying to SQLite that a concurrently written DB is immutable.
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        _migrate(conn, version)
    return conn


def _migrate(conn, version=0):
    if 0 < version < 2:
        # v1 mixed split identifier tokens into the displayed content column.
        # The corpus is derived, so drop it and let the next refresh rebuild.
        conn.executescript("DROP TABLE IF EXISTS code_fts;"
                           "DELETE FROM code_files; DELETE FROM code_scan_state;")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS code_repos (
            repo_key TEXT PRIMARY KEY, display_name TEXT, root_path TEXT,
            remote_url TEXT, head_sha TEXT, branch TEXT, file_count INTEGER,
            total_size INTEGER, modified REAL, indexed_at TEXT,
            skip_counts TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS code_files (
            id INTEGER PRIMARY KEY, repo_key TEXT NOT NULL, rel_path TEXT NOT NULL,
            file_path TEXT, lang TEXT, file_size INTEGER, file_mtime REAL,
            content_hash TEXT, indexed_at TEXT, UNIQUE(repo_key, rel_path));
        CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5 (
            content, terms, repo_key UNINDEXED, rel_path UNINDEXED, chunk_ordinal UNINDEXED,
            start_line UNINDEXED, end_line UNINDEXED, lang UNINDEXED, tokenize='unicode61');
        CREATE TABLE IF NOT EXISTS code_scan_state (
            repo_key TEXT, rel_path TEXT, file_size INTEGER, file_mtime REAL, reason TEXT,
            PRIMARY KEY(repo_key, rel_path));
        CREATE TABLE IF NOT EXISTS code_ignore_state (
            repo_key TEXT, rel_path TEXT, state TEXT,
            PRIMARY KEY(repo_key, rel_path));
        PRAGMA user_version=2;
    """)


def add_repo(root: str | Path, repo_key: str | None = None) -> str:
    root = resolve_root(str(root))
    key = repo_key or portable_key(str(root))
    if not key.strip() or any(c in key for c in "\r\n"):
        raise ValueError("Invalid repo key")
    with _update_lock():
        registry = load_registry()
        registry[key] = str(root)
        _save_registry(registry)
    return key


def _delete_file(conn, key, rel):
    for table in ("code_fts", "code_files", "code_scan_state"):
        conn.execute(f"DELETE FROM {table} WHERE repo_key=? AND rel_path=?", (key, rel))


def remove_repo(key: str):
    with _update_lock():
        registry = load_registry()
        if key not in registry:
            raise ValueError(f"Unknown repo: {key}")
        del registry[key]
        _save_registry(registry)
        if Path(DB_PATH).exists():
            with closing(_get_db()) as conn, conn:
                for table in ("code_fts", "code_files", "code_scan_state", "code_ignore_state", "code_repos"):
                    conn.execute(f"DELETE FROM {table} WHERE repo_key=?", (key,))


def identifier_terms(text: str) -> list[str]:
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return re.findall(r"[^\W_]+", text.lower(), re.UNICODE)


def update_code_index(force=False, progress_callback=None, skip_if_running=False, repo_key=None) -> dict:
    from core import repo_brief
    stats = dict(new=0, updated=0, deleted=0, read=0, skipped=False)
    with _update_lock(skip_if_running) as acquired:
        if not acquired:
            return {**stats, "skipped": True}
        registry = load_registry()
        if repo_key is not None:
            if repo_key not in registry:
                raise ValueError(f"Unknown repo: {repo_key}")
            registry = {repo_key: registry[repo_key]}
        if not registry and not Path(DB_PATH).exists():
            return stats
        with closing(_get_db()) as conn, conn:
            if repo_key is None:
                for row in conn.execute("SELECT repo_key FROM code_repos").fetchall():
                    if row[0] not in registry:
                        stats["deleted"] += conn.execute("SELECT count(*) FROM code_files WHERE repo_key=?", (row[0],)).fetchone()[0]
                        for table in ("code_fts", "code_files", "code_scan_state", "code_ignore_state", "code_repos"):
                            conn.execute(f"DELETE FROM {table} WHERE repo_key=?", (row[0],))
            for key, raw_root in registry.items():
                root = resolve_root(raw_root)
                previous = {r["rel_path"]: (r["file_size"], r["file_mtime"], r["reason"])
                            for r in conn.execute("SELECT * FROM code_scan_state WHERE repo_key=?", (key,))}
                existing = {r["rel_path"]: dict(r) for r in conn.execute("SELECT * FROM code_files WHERE repo_key=?", (key,))}
                ignores = {r["rel_path"]: json.loads(r["state"]) for r in conn.execute(
                    "SELECT rel_path,state FROM code_ignore_state WHERE repo_key=?", (key,))}
                scan = scanner.scan_repo(root, previous, force=force, previous_ignores=ignores)
                for rel in ignores.keys() - scan.ignore_states.keys():
                    conn.execute("DELETE FROM code_ignore_state WHERE repo_key=? AND rel_path=?", (key, rel))
                for rel, state in scan.ignore_states.items():
                    if state != ignores.get(rel):
                        conn.execute("INSERT OR REPLACE INTO code_ignore_state VALUES (?,?,?)",
                                     (key, rel, json.dumps(state)))
                stats["read"] += scan.read
                for rel in existing.keys() - scan.files.keys():
                    _delete_file(conn, key, rel)
                    stats["deleted"] += 1
                for rel in previous.keys() - scan.states.keys():
                    conn.execute("DELETE FROM code_scan_state WHERE repo_key=? AND rel_path=?", (key, rel))
                for rel, state in scan.states.items():
                    if state != previous.get(rel):
                        conn.execute("INSERT OR REPLACE INTO code_scan_state VALUES (?,?,?,?,?)", (key, rel, *state))
                for number, (rel, source) in enumerate(scan.files.items(), 1):
                    if progress_callback:
                        progress_callback(number, len(scan.files), rel)
                    old = existing.get(rel)
                    if source.text is None and old:
                        continue
                    if source.text is None:
                        source.text, source.content_hash, reason = scanner.read_source(source.path)
                        if reason:
                            raise OSError(f"Source became unreadable: {rel}")
                        stats["read"] += 1
                    if old and source.content_hash == old["content_hash"]:
                        conn.execute("UPDATE code_files SET file_size=?, file_mtime=? WHERE repo_key=? AND rel_path=?",
                                     (source.size, source.mtime, key, rel))
                        continue
                    conn.execute("DELETE FROM code_fts WHERE repo_key=? AND rel_path=?", (key, rel))
                    conn.execute("""INSERT INTO code_files
                        (repo_key,rel_path,file_path,lang,file_size,file_mtime,content_hash,indexed_at)
                        VALUES (?,?,?,?,?,?,?,datetime('now')) ON CONFLICT(repo_key,rel_path) DO UPDATE SET
                        file_path=excluded.file_path,lang=excluded.lang,file_size=excluded.file_size,
                        file_mtime=excluded.file_mtime,content_hash=excluded.content_hash,indexed_at=excluded.indexed_at""",
                        (key, rel, str(source.path), source.lang, source.size, source.mtime, source.content_hash))
                    lines = source.text.splitlines()
                    for start in range(0, len(lines), 50):
                        chunk = "\n".join(lines[start:start + 50])
                        # Split identifier forms live in their own searchable column,
                        # so displayed snippets stay pure redacted source text and the
                        # line metadata always describes that original source.
                        conn.execute("INSERT INTO code_fts VALUES (?,?,?,?,?,?,?,?)",
                                     (chunk, " ".join(identifier_terms(chunk)), key, rel,
                                      start // 50, start + 1, min(start + 50, len(lines)), source.lang))
                    stats["updated" if old else "new"] += 1
                values = (root.name, str(root), len(scan.files), sum(s.size for s in scan.files.values()),
                          max((s.mtime for s in scan.files.values()), default=0), json.dumps(dict(scan.skipped), sort_keys=True))
                prior = conn.execute("SELECT display_name,root_path,file_count,total_size,modified,skip_counts FROM code_repos WHERE repo_key=?", (key,)).fetchone()
                if prior is None or tuple(prior) != values or scan.read:
                    conn.execute("""INSERT INTO code_repos
                        (repo_key,display_name,root_path,file_count,total_size,modified,skip_counts,indexed_at)
                        VALUES (?,?,?,?,?,?,?,datetime('now')) ON CONFLICT(repo_key) DO UPDATE SET
                        display_name=excluded.display_name,root_path=excluded.root_path,file_count=excluded.file_count,
                        total_size=excluded.total_size,modified=excluded.modified,skip_counts=excluded.skip_counts,
                        indexed_at=excluded.indexed_at""", (key, *values))
                head = repo_brief.head_sha(root)
                stored_head = conn.execute('SELECT head_sha FROM code_repos WHERE repo_key=?', (key,)).fetchone()[0]
                if stored_head != head:
                    conn.execute("UPDATE code_repos SET head_sha=?,indexed_at=datetime('now') WHERE repo_key=?", (head, key))
        # Run only after the index transaction commits; a failed brief remains
        # durably queued without discarding successfully indexed source.
    stats['briefs'] = {}
    for key in registry:
        try:
            stats['briefs'][key] = repo_brief.mark_drift(key)
            repo_brief.refresh_if_needed(key)
        except (OSError, ValueError, sqlite3.Error) as exc:
            stats['briefs'][key] = {'error': str(exc), 'stale': True}
    return stats


def _read_db():
    path = Path(DB_PATH).resolve()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _snippet(content: str, terms: set[str], limit=400) -> str:
    """A match-centred window of the stored source. Never synthetic text.

    FTS5's own snippet() can only centre on tokens it indexed in that column, so
    a query written in the other identifier style would land on the head of the
    chunk. The split forms are searchable in their own column; the window shown
    to a reader is cut from the redacted source alone.
    """
    lines = content.splitlines()
    best = score = 0
    for number, line in enumerate(lines):
        hits = len(terms.intersection(identifier_terms(line)))
        if hits > score:
            best, score = number, hits
    out: list[str] = []
    size = 0
    for line in lines[max(0, best - 2):]:
        size += len(line) + 1
        if out and size > limit:
            break
        out.append(line)
    return "\n".join(out)


def search_code_fts(query: str, limit=20, repo_key=None) -> list[dict]:
    terms = identifier_terms(query)[:32]
    if not terms or limit <= 0 or not Path(DB_PATH).is_file():
        return []
    expression = " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)
    try:
        with closing(_read_db()) as conn:
            # A superseded corpus is never served: read surfaces cannot migrate
            # or refresh, so stale rows wait for the next writing refresh.
            if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                return []
            matches = conn.execute("""SELECT repo_key,rel_path,start_line,end_line,lang,content,rank
                FROM code_fts WHERE code_fts MATCH ? AND (? IS NULL OR repo_key=?)
                ORDER BY rank LIMIT ?""", (expression, repo_key, repo_key, min(limit, 100) * 100)).fetchall()
    except sqlite3.Error:
        return []
    registry = load_registry()
    groups = {}
    wanted = set(terms)
    for row in matches:
        item = dict(row)
        item["snippet"] = _snippet(item.pop("content"), wanted)
        groups.setdefault((item["repo_key"], item["rel_path"]), []).append(item)
    out = []
    for (key, rel), items in groups.items():
        if key not in registry:
            continue
        try:
            root = resolve_root(registry[key])
        except (ValueError, OSError):
            continue
        merged = []
        for item in sorted(items, key=lambda r: r["start_line"]):
            if merged and merged[-1]["end_line"] + 1 == item["start_line"]:
                merged[-1]["end_line"] = item["end_line"]
                merged[-1]["snippet"] = (merged[-1]["snippet"] + "\n" + item["snippet"])[:1600]
                merged[-1]["rank"] = min(merged[-1]["rank"], item["rank"])
            else:
                merged.append(item)
        for item in merged:
            item.update(source="code", file_path=str(root / rel),
                        citation=f"{key}:{rel}:{item['start_line']}-{item['end_line']}")
            out.append(item)
    return sorted(out, key=lambda r: (r["rank"], r["repo_key"], r["rel_path"], r["start_line"]))[:min(limit, 100)]


def code_status() -> list[dict]:
    indexed = {}
    if Path(DB_PATH).is_file():
        with closing(_read_db()) as conn:
            indexed = {r["repo_key"]: dict(r) for r in conn.execute("SELECT * FROM code_repos")}
    return [{**indexed.get(k, {}), "repo_key": k, "root_path": v} for k, v in load_registry().items()]


def drop_code_index():
    """Clear only the derived code corpus, retaining the explicit registry."""
    with _update_lock():
        if Path(DB_PATH).exists():
            with closing(_get_db()) as conn, conn:
                for table in ("code_fts", "code_files", "code_scan_state", "code_ignore_state", "code_repos"):
                    conn.execute(f"DELETE FROM {table}")
