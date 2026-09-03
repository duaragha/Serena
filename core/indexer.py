"""SQLite index for session metadata and full-text search."""

import json
import re
import shutil
import sqlite3
from contextlib import suppress
import threading
from bisect import bisect_left
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from core.config import DATA_DIR, DB_PATH, claude_project_dir_for
from core import metadata as meta_sync
from core.parser import SessionMeta, parse_messages_for_search, parse_metadata
from core.scanner import scan_sessions
from core.codex_scanner import scan_codex_sessions, parse_codex_metadata, _FILENAME_RE as _CODEX_FILE_RE
from core.gemini_scanner import scan_gemini_sessions, parse_gemini_metadata
from core.locket_scanner import scan_locket_sessions, parse_locket_metadata
from core.voice_transcripts import (
    VOICE_SESSION_ID,
    parse_voice_metadata,
    scan_voice_sessions,
    voice_transcript_path,
)
from chats.titles import generate_title

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


_schema_ready = False
_INDEX_THREAD_LOCK = threading.RLock()
_INDEX_LOCK_PATH = DATA_DIR / "index-update.lock"


def _is_locked_error(exc: Exception) -> bool:
    return "database is locked" in str(exc).lower()


def _db_write_available(timeout: float = 0.1) -> bool:
    """Cheaply test whether SQLite can take a write lock right now."""
    conn = sqlite3.connect(str(DB_PATH), timeout=timeout)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
        return True
    except sqlite3.OperationalError as e:
        if _is_locked_error(e):
            return False
        raise
    finally:
        conn.close()


@contextmanager
def _index_update_lock(skip_if_running: bool = False):
    """Serialize expensive index rebuilds across Flask threads/processes.

    The desktop UI can request refreshes faster than update_index() can scan and
    parse active sessions. Let at most one refresh run; stale reads are better
    than stacking writers behind SQLite.
    """
    got_thread = _INDEX_THREAD_LOCK.acquire(blocking=not skip_if_running)
    if not got_thread:
        yield False
        return

    lock_file = None
    locked = False
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        lock_file = _INDEX_LOCK_PATH.open("a+b")
        if fcntl is not None:
            flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if skip_if_running else 0)
            try:
                fcntl.flock(lock_file.fileno(), flags)
                locked = True
            except BlockingIOError:
                yield False
                return
        yield True
    finally:
        if locked and fcntl is not None and lock_file is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        if lock_file is not None:
            try:
                lock_file.close()
            except OSError:
                pass
        _INDEX_THREAD_LOCK.release()



def _get_db() -> sqlite3.Connection:
    global _schema_ready
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # timeout=30: a full reindex of a few hundred sessions can hold the write
    # lock past sqlite's default 5s — writers (renames, stars, meta) should
    # wait it out, not die with "database is locked".
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # DDL only once per process — running CREATE/ALTER on every connection
    # made every caller (even reads) contend for the write lock.
    if not _schema_ready:
        _create_tables(conn)
        _migrate(conn)
        _schema_ready = True
    return conn


def _create_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            project_dir TEXT NOT NULL,
            cwd TEXT,
            device TEXT,
            first_message TEXT,
            title TEXT,
            custom_title TEXT,
            starred INTEGER DEFAULT 0,
            first_timestamp TEXT,
            last_timestamp TEXT,
            message_count INTEGER,
            model TEXT,
            git_branch TEXT,
            slug TEXT,
            file_path TEXT NOT NULL,
            file_size INTEGER,
            file_mtime REAL,
            indexed_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS tags (
            session_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY (session_id, tag),
            FOREIGN KEY (session_id) REFERENCES sessions(session_id)
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_timestamp ON sessions(first_timestamp);
        CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_dir);
        CREATE INDEX IF NOT EXISTS idx_sessions_starred ON sessions(starred);
        CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

        CREATE TABLE IF NOT EXISTS knowledge_topics (
            slug TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            file_count INTEGER,
            total_size INTEGER,
            modified REAL,
            indexed_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS knowledge_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_slug TEXT NOT NULL,
            filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_size INTEGER,
            file_mtime REAL,
            indexed_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (topic_slug) REFERENCES knowledge_topics(slug),
            UNIQUE(topic_slug, filename)
        );

        CREATE TABLE IF NOT EXISTS session_topics (
            session_id TEXT NOT NULL,
            topic_slug TEXT NOT NULL,
            link_type TEXT DEFAULT 'manual',
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (session_id, topic_slug),
            FOREIGN KEY (session_id) REFERENCES sessions(session_id),
            FOREIGN KEY (topic_slug) REFERENCES knowledge_topics(slug)
        );

        -- The permanent voice transcript is append-only.  This offset lets
        -- its FTS rows be added once per turn rather than rebuilt forever.
        CREATE TABLE IF NOT EXISTS voice_fts_state (
            session_id TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            indexed_offset INTEGER NOT NULL
        );
    """)

    for vt_sql in [
        """CREATE VIRTUAL TABLE messages_fts USING fts5(
            content, session_id UNINDEXED, role UNINDEXED, timestamp UNINDEXED
        )""",
        """CREATE VIRTUAL TABLE knowledge_fts USING fts5(
            content, topic_slug UNINDEXED, filename UNINDEXED
        )""",
    ]:
        try:
            conn.execute(vt_sql)
        except sqlite3.OperationalError:
            pass

    conn.commit()


def _migrate(conn: sqlite3.Connection):
    """Add columns that may not exist in older databases."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    migrations = {
        "title": "ALTER TABLE sessions ADD COLUMN title TEXT",
        "custom_title": "ALTER TABLE sessions ADD COLUMN custom_title TEXT",
        "starred": "ALTER TABLE sessions ADD COLUMN starred INTEGER DEFAULT 0",
        "input_tokens": "ALTER TABLE sessions ADD COLUMN input_tokens INTEGER DEFAULT 0",
        "output_tokens": "ALTER TABLE sessions ADD COLUMN output_tokens INTEGER DEFAULT 0",
        "cache_read_tokens": "ALTER TABLE sessions ADD COLUMN cache_read_tokens INTEGER DEFAULT 0",
        "cache_create_tokens": "ALTER TABLE sessions ADD COLUMN cache_create_tokens INTEGER DEFAULT 0",
        "last_cwd": "ALTER TABLE sessions ADD COLUMN last_cwd TEXT",
        "raw_message_count": "ALTER TABLE sessions ADD COLUMN raw_message_count INTEGER",
        "is_teammate": "ALTER TABLE sessions ADD COLUMN is_teammate INTEGER DEFAULT 0",
        "is_done": "ALTER TABLE sessions ADD COLUMN is_done INTEGER DEFAULT 0",
        "done_at": "ALTER TABLE sessions ADD COLUMN done_at TEXT",
        "agent": "ALTER TABLE sessions ADD COLUMN agent TEXT NOT NULL DEFAULT 'claude'",
        "originator": "ALTER TABLE sessions ADD COLUMN originator TEXT",
        "parent_session_id": "ALTER TABLE sessions ADD COLUMN parent_session_id TEXT",
        "devices_used": "ALTER TABLE sessions ADD COLUMN devices_used TEXT",
    }
    for col, sql in migrations.items():
        if col not in columns:
            conn.execute(sql)
    conn.commit()


def update_index(
    force: bool = False,
    progress_callback=None,
    skip_if_running: bool = False,
) -> tuple[int, int]:
    """Scan for new/changed sessions and update the index.

    Returns (new_count, updated_count).
    """
    with _index_update_lock(skip_if_running=skip_if_running) as acquired:
        if not acquired:
            return 0, 0
        if skip_if_running and not _db_write_available():
            return 0, 0
        try:
            return _update_index_locked(force=force, progress_callback=progress_callback)
        except sqlite3.OperationalError as e:
            if skip_if_running and _is_locked_error(e):
                return 0, 0
            raise


def _update_index_locked(force: bool = False, progress_callback=None) -> tuple[int, int]:
    conn = _get_db()

    existing = {}
    existing_paths = {}
    needs_reparse = set()
    for row in conn.execute(
        "SELECT session_id, file_size, file_mtime, file_path, raw_message_count FROM sessions"
    ):
        existing[row["session_id"]] = (row["file_size"], row["file_mtime"])
        existing_paths[row["session_id"]] = row["file_path"]
        if row["raw_message_count"] is None:
            needs_reparse.add(row["session_id"])

    # Combined discovery: claude sessions (project-dir layout) + codex sessions
    # (date-tree layout). Tag each entry with its agent so the rest of the
    # pipeline can dispatch parsing + spawning correctly.
    discovered: list[tuple[str, str, Path]] = []
    for project_dir, fp in scan_sessions():
        discovered.append(("claude", project_dir, fp))
    for project_dir, fp in scan_codex_sessions():
        discovered.append(("codex", project_dir, fp))
    for project_dir, fp in scan_locket_sessions():
        discovered.append(("locket", project_dir, fp))
    for project_dir, fp in scan_voice_sessions():
        discovered.append(("serena-voice", project_dir, fp))
    for agent_name, fp in scan_gemini_sessions():
        discovered.append((agent_name, "gemini", fp))

    # The same Claude session can legitimately exist under several cwd/OS
    # slug folders. Indexing every copy reparses large transcripts repeatedly
    # and makes the final row depend on scanner order. Keep one stable,
    # most-complete copy per session id instead.
    discovered = _dedupe_discovered(discovered, existing_paths)

    discovered_ids: set[str] = set()
    for agent, _, fp in discovered:
        session_id = _discovered_session_id(agent, fp)
        if session_id:
            discovered_ids.add(session_id)

    # Prune zombie rows: indexed sessions the scanners no longer surface.
    # This catches files deleted on disk or filtered out by scanner policy.
    zombies = [sid for sid in existing_paths if sid not in discovered_ids]
    for sid in zombies:
        conn.execute("DELETE FROM tags WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM messages_fts WHERE session_id = ?", (sid,))
        existing.pop(sid, None)
    all_meta = meta_sync.get_all_meta()
    new_count = 0
    updated_count = 0
    total = len(discovered)

    for i, (agent, project_dir, file_path) in enumerate(discovered):
        session_id = _discovered_session_id(agent, file_path)
        if not session_id:
            continue

        if progress_callback:
            progress_callback(i + 1, total, session_id)

        if not force and session_id in existing and session_id not in needs_reparse:
            old_size, old_mtime = existing[session_id]
            try:
                stat = file_path.stat()
            except OSError:
                continue
            if stat.st_size == old_size and stat.st_mtime == old_mtime:
                # Still apply synced metadata even if file hasn't changed
                synced = all_meta.get(session_id, {})
                if synced:
                    _apply_synced_meta(conn, session_id, synced)
                continue
            updated_count += 1
        else:
            if session_id in existing:
                updated_count += 1
            else:
                new_count += 1

        if agent == "claude":
            meta = parse_metadata(file_path, project_dir)
        elif agent == "locket":
            meta = parse_locket_metadata(file_path)
            if meta is None:
                continue
        elif agent == "serena-voice":
            _index_voice_transcript_locked(conn, file_path)
            continue
        elif agent == "gemini":
            meta = parse_gemini_metadata(file_path)
            if meta is None:
                continue
            project_dir = meta.project_dir
        else:
            meta = parse_codex_metadata(file_path)
            if meta is None:
                continue
            # Re-derive project_dir from the parsed cwd-slug now that we have it
            project_dir = meta.project_dir
        _upsert_session(conn, meta, all_meta, agent=agent)

    _hide_internal_sessions(conn)
    _attribute_codex_parents(conn)
    _repair_custom_title_groups(conn)
    conn.commit()
    conn.close()

    try:
        from core.project_mirror import sync_mirrors
        sync_mirrors()
    except Exception:
        pass

    return new_count, updated_count



def _discovered_session_id(agent: str, file_path: Path) -> str | None:
    if agent == "gemini":
        # Antigravity names each conversation file after its id, which is also
        # what `agy --conversation <id>` takes to reopen it.
        return file_path.stem
    if agent == "codex":
        match = _CODEX_FILE_RE.match(file_path.name)
        return match.group(1) if match else None
    if agent == "serena-voice":
        return VOICE_SESSION_ID
    return file_path.stem


def _dedupe_discovered(
    discovered: list[tuple[str, str, Path]],
    existing_paths: dict[str, str],
) -> list[tuple[str, str, Path]]:
    selected: dict[str, tuple[tuple[int, int, int, int], tuple[str, str, Path]]] = {}
    for entry in discovered:
        agent, project_dir, file_path = entry
        session_id = _discovered_session_id(agent, file_path)
        if not session_id:
            continue
        try:
            stat = file_path.stat()
        except OSError:
            continue
        cwd_match = _claude_slug_affinity(project_dir, file_path) if agent == "claude" else 0
        stable = int(existing_paths.get(session_id) == str(file_path))
        # Completeness wins. For equal copies, prefer the folder matching the
        # transcript's own cwd before preserving a stale Syncthing copy path.
        score = (stat.st_size, cwd_match, stable, stat.st_mtime_ns)
        current = selected.get(session_id)
        if current is None or score > current[0]:
            selected[session_id] = (score, entry)
    return [item[1] for item in selected.values()]


def _claude_slug_affinity(project_dir: str, file_path: Path) -> int:
    """Return 1 when a copy lives under the slug recorded by its transcript."""
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= 200:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = record.get("cwd")
                if not isinstance(cwd, str) or not cwd:
                    continue
                expected = claude_project_dir_for(cwd)
                normalize = lambda value: value.replace("_", "-").casefold()
                return int(normalize(expected) == normalize(project_dir))
    except OSError:
        pass
    return 0


def _project_identity(row: sqlite3.Row) -> str:
    raw = str(row["last_cwd"] or row["cwd"] or row["project_dir"] or "")
    value = raw.replace("\\", "/").replace("_", "-").casefold().rstrip("/")
    for marker in ("/documents/projects/", "/projects/", "-documents-projects-", "-projects-"):
        if marker in value:
            return value.split(marker, 1)[1].replace("/", "-")
    return value


def _repair_custom_title_groups(conn: sqlite3.Connection) -> int:
    """Re-link same-project duplicate names when one is explicitly custom.

    Syncthing can restore a session or metadata file before its group siblings.
    The custom title is the durable user signal that these rows are one named
    thread, so use it to heal missing group metadata without deleting history.
    """
    rows = conn.execute(
        """
        SELECT session_id, project_dir, cwd, last_cwd, title, custom_title
        FROM sessions
        WHERE COALESCE(is_teammate, 0) = 0
          AND COALESCE(custom_title, title, '') <> ''
        """
    ).fetchall()
    buckets: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        display = str(row["custom_title"] or row["title"] or "").strip().casefold()
        if display:
            buckets.setdefault((display, _project_identity(row)), []).append(row)

    all_meta = meta_sync.get_all_meta()
    repaired = 0
    for members in buckets.values():
        if len(members) < 2 or not any(row["custom_title"] for row in members):
            continue
        session_ids = [row["session_id"] for row in members]
        groups = [(all_meta.get(sid) or {}).get("group") for sid in session_ids]
        if groups[0] and all(group == groups[0] for group in groups):
            continue
        meta_sync.link_sessions(session_ids)
        repaired += 1
    return repaired


def _is_internal_project(project_dir: str | None) -> bool:
    value = (project_dir or "").casefold()
    return (
        "serena-headless" in value
        or "-tmp-serena-" in value
        or "cache-serena-headless" in value
    )


def _hide_internal_sessions(conn: sqlite3.Connection) -> None:
    """Keep resident-brain rotations indexed without showing them as chats."""
    conn.execute(
        """
        UPDATE sessions
        SET is_teammate = 1
        WHERE project_dir LIKE '%serena-headless%'
           OR project_dir LIKE '%-tmp-serena-%'
           OR project_dir LIKE '%cache-serena-headless%'
        """
    )



def _apply_synced_meta(conn: sqlite3.Connection, session_id: str, synced: dict):
    """Apply synced metadata to an existing session without re-parsing the file."""
    updates = []
    params = []
    if "custom_title" in synced:
        updates.append("custom_title = ?")
        params.append(synced["custom_title"])
    if "starred" in synced:
        updates.append("starred = ?")
        params.append(1 if synced["starred"] else 0)
    if "done" in synced:
        updates.append("is_done = ?")
        params.append(1 if synced["done"] else 0)
        updates.append("done_at = ?")
        params.append(synced.get("done_at") or None)
    if updates:
        params.append(session_id)
        conn.execute(f"UPDATE sessions SET {', '.join(updates)} WHERE session_id = ?", params)
    if "tags" in synced:
        for tag in synced["tags"]:
            conn.execute(
                "INSERT OR IGNORE INTO tags (session_id, tag) VALUES (?, ?)",
                (session_id, tag),
            )


def _upsert_session(conn: sqlite3.Connection, meta: SessionMeta, all_meta: dict | None = None, agent: str = "claude"):
    title = "Serena" if agent == "serena-voice" else generate_title(meta.first_message)

    # Get synced metadata (stars, tags, custom_title) from the shared JSON
    if all_meta is not None:
        synced = all_meta.get(meta.session_id, {})
    else:
        synced = meta_sync.get_meta(meta.session_id)

    custom_title = synced.get("custom_title")
    starred = 1 if synced.get("starred") else 0
    synced_tags = synced.get("tags", [])

    # Done flag — auto-unmark when new activity arrives after the done_at timestamp
    is_done = 1 if synced.get("done") else 0
    done_at = synced.get("done_at") or None
    last_ts = meta.last_timestamp.isoformat() if meta.last_timestamp else None
    if is_done and done_at and last_ts and last_ts > done_at:
        is_done = 0
        done_at = None
        try:
            meta_sync.set_done(meta.session_id, False)
        except Exception:
            pass

    devices_used_csv = ",".join(getattr(meta, "devices_used", []) or [])
    conn.execute("""
        INSERT OR REPLACE INTO sessions
        (session_id, project_dir, cwd, last_cwd, device, first_message, title,
         custom_title, starred, is_done, done_at,
         first_timestamp, last_timestamp,
         message_count, raw_message_count, is_teammate,
         model, git_branch, slug, file_path, file_size, file_mtime,
         input_tokens, output_tokens, cache_read_tokens, cache_create_tokens, agent, originator,
         devices_used)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        meta.session_id, meta.project_dir, meta.cwd, meta.last_cwd, meta.device,
        meta.first_message, title, custom_title, starred, is_done, done_at,
        meta.first_timestamp.isoformat() if meta.first_timestamp else None,
        meta.last_timestamp.isoformat() if meta.last_timestamp else None,
        meta.message_count, meta.raw_message_count,
        1 if (meta.is_teammate or _is_internal_project(meta.project_dir)) else 0,
        meta.model, meta.git_branch, meta.slug,
        meta.file_path, meta.file_size, meta.file_mtime,
        meta.input_tokens, meta.output_tokens, meta.cache_read_tokens, meta.cache_create_tokens,
        agent, getattr(meta, "originator", None),
        devices_used_csv or None,
    ))

    # Apply synced tags
    if synced_tags:
        for tag in synced_tags:
            conn.execute(
                "INSERT OR IGNORE INTO tags (session_id, tag) VALUES (?, ?)",
                (meta.session_id, tag),
            )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _norm_cwd(cwd: str | None) -> str:
    if not cwd:
        return ""
    norm = cwd.replace("\\", "/").rstrip("/")
    if len(norm) >= 2 and norm[1] == ":":
        norm = norm[0].lower() + norm[1:]
    return norm or "/"


def _cwd_distance(a: str | None, b: str | None) -> int | None:
    """Return the directory-segment distance between two cwds, or None if
    they're unrelated (no shared path lineage). 0 = same dir; 1 = direct
    parent/child; n = n levels apart along the same path.

    A loose ancestor like '/home/raghav' has distance ~4 from
    '/home/raghav/Documents/Projects/serena' — too loose to be a meaningful
    match. The caller should reject distances above some threshold
    (typically 3) so generic home/root claude chats don't claim every codex
    session below them.
    """
    a = _norm_cwd(a)
    b = _norm_cwd(b)
    if not a or not b:
        return None
    if a == b:
        return 0
    if a == "/" or b == "/":
        return None
    if a.startswith(b + "/"):
        return a.count("/") - b.count("/")
    if b.startswith(a + "/"):
        return b.count("/") - a.count("/")
    return None


def _cwd_same_or_child(child_cwd: str | None, parent_cwd: str | None) -> bool:
    """Backward-compat shim — true if the two paths are on the same lineage.
    Use _cwd_distance directly when you need the magnitude.
    """
    return _cwd_distance(child_cwd, parent_cwd) is not None


def _session_seconds(row: sqlite3.Row) -> float | None:
    first = _parse_dt(row["first_timestamp"])
    last = _parse_dt(row["last_timestamp"]) or first
    if not first or not last:
        return None
    return max(0.0, (last - first).total_seconds())


def _is_agent_spawned_candidate(row: sqlite3.Row) -> bool:
    """True when a codex session might have been spawned by another agent.
    The originator is packed as "<originator>:<source>".

    Definite agent-spawned (always candidate): source=mcp, source=vscode,
        or originator=Claude Code.
    Ambiguous (candidate, but only nests if a Claude parent matches by
        cwd+time): source=exec — `codex exec` can be user-initiated OR
        called by claude's Bash tool. The time-window check in
        _find_claude_parent does the disambiguation. No parent match
        means real user one-shot, stays top-level.
    Definitely user-initiated (never candidate): source=cli — real
        interactive `codex` CLI use; would never overlap with a claude
        session in the same cwd at the same time.
    """
    origin = (row["originator"] or "").lower()
    if ":" in origin:
        orig_part, source = origin.split(":", 1)
        source = source.strip()
        if source in ("mcp", "vscode", "exec"):
            return True
        if source == "cli":
            return False
        origin_check = orig_part.strip()
    else:
        origin_check = origin
    return origin_check == "claude code"


_MAX_CWD_DISTANCE = 3  # tighter than this rejects loose ancestors like /home/raghav
_PARENT_MESSAGE_WINDOW = timedelta(minutes=30)


def _dt_epoch_seconds(dt: datetime) -> float:
    return dt.timestamp()


def _is_home_cwd(cwd: str | None) -> bool:
    if not cwd:
        return False
    home = _norm_cwd(str(Path.home().resolve()))
    if _norm_cwd(cwd) == home:
        return True
    try:
        return _norm_cwd(str(Path(cwd).expanduser().resolve(strict=False))) == home
    except (OSError, RuntimeError):
        return False


def _read_claude_message_timestamps(file_path: str | None) -> list[datetime]:
    """Read only user/assistant record timestamps from a Claude JSONL file."""
    if not file_path:
        return []

    timestamps: list[datetime] = []
    try:
        with open(Path(file_path), "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if record.get("type") not in ("user", "assistant"):
                    continue
                ts = _parse_dt(record.get("timestamp"))
                if ts is not None:
                    timestamps.append(ts)
    except (OSError, PermissionError):
        return []

    timestamps.sort(key=_dt_epoch_seconds)
    return timestamps


def _claude_message_timestamps(
    claude: sqlite3.Row, cache: dict[str, list[datetime]]
) -> list[datetime]:
    session_id = claude["session_id"]
    if session_id not in cache:
        cache[session_id] = _read_claude_message_timestamps(claude["file_path"])
    return cache[session_id]


def _closest_message_delta(
    codex_first: datetime, claude_timestamps: list[datetime]
) -> timedelta | None:
    if not claude_timestamps:
        return None

    target = _dt_epoch_seconds(codex_first)
    idx = bisect_left(claude_timestamps, target, key=_dt_epoch_seconds)

    closest: float | None = None
    for candidate_idx in (idx - 1, idx):
        if 0 <= candidate_idx < len(claude_timestamps):
            delta = abs(_dt_epoch_seconds(claude_timestamps[candidate_idx]) - target)
            if closest is None or delta < closest:
                closest = delta

    if closest is None:
        return None
    return timedelta(seconds=closest)


def _find_claude_parent(
    codex: sqlite3.Row,
    claude_rows: list[sqlite3.Row],
    claude_timestamp_cache: dict[str, list[datetime]],
) -> str | None:
    codex_first = _parse_dt(codex["first_timestamp"])
    if not codex_first:
        return None

    # Score each candidate by (cwd_distance, closest_message_delta), then keep
    # the previous most-recently-active tiebreaker for otherwise equal matches.
    matches: list[tuple[int, timedelta, datetime, bool, str]] = []
    for claude in claude_rows:
        dist = _cwd_distance(codex["cwd"], claude["cwd"])
        if dist is None or dist > _MAX_CWD_DISTANCE:
            continue

        timestamps = _claude_message_timestamps(claude, claude_timestamp_cache)
        if not timestamps:
            continue

        claude_first = _parse_dt(claude["first_timestamp"])
        claude_last = _parse_dt(claude["last_timestamp"]) or claude_first or timestamps[-1]
        closest_delta = _closest_message_delta(codex_first, timestamps)
        if closest_delta is None or closest_delta > _PARENT_MESSAGE_WINDOW:
            continue

        matches.append(
            (
                dist,
                closest_delta,
                claude_last,
                _is_home_cwd(claude["cwd"]),
                claude["session_id"],
            )
        )

    if not matches:
        return None

    # A Claude session rooted only at $HOME is too generic to claim Codex work
    # unless the Codex cwd is also $HOME and no more-specific Claude candidate
    # was active near the Codex start.
    non_home_matches = [m for m in matches if not m[3]]
    if non_home_matches:
        matches = non_home_matches
    elif not _is_home_cwd(codex["cwd"]):
        return None

    matches.sort(key=lambda m: (m[0], m[1], -m[2].timestamp()))
    return matches[0][4]


def _attribute_codex_parents(conn: sqlite3.Connection):
    claude_rows = conn.execute("""
        SELECT session_id, cwd, first_timestamp, last_timestamp, file_path
        FROM sessions
        WHERE agent = 'claude'
    """).fetchall()
    codex_rows = conn.execute("""
        SELECT session_id, cwd, first_timestamp, last_timestamp, originator
        FROM sessions
        WHERE agent = 'codex'
    """).fetchall()
    claude_timestamp_cache: dict[str, list[datetime]] = {}

    for row in codex_rows:
        parent_id = None
        # Fleet has its own durable run grouping. A direct Codex worker happens
        # to look like an ordinary agent-spawned `codex exec` rollout, but it
        # must never be nested under the nearby Claude chat that launched Fleet.
        is_fleet_worker = bool(meta_sync.get_meta(row["session_id"]).get("fleet_worker"))
        if not is_fleet_worker and _is_agent_spawned_candidate(row):
            parent_id = _find_claude_parent(row, claude_rows, claude_timestamp_cache)
            if not parent_id and (row["originator"] or "").lower() == "claude code":
                parent_id = "orphan-claude-code"
        conn.execute(
            "UPDATE sessions SET parent_session_id = ? WHERE session_id = ?",
            (parent_id, row["session_id"]),
        )


def build_fts(progress_callback=None):
    """Build/rebuild the full-text search index."""
    conn = _get_db()
    conn.execute("DELETE FROM messages_fts")
    # A full rebuild owns the voice rows too.  Reset the append cursor so it
    # cannot claim rows are present when this rebuild is interrupted.
    conn.execute("DELETE FROM voice_fts_state")

    rows = conn.execute("SELECT session_id, file_path FROM sessions").fetchall()
    total = len(rows)

    for i, row in enumerate(rows):
        if progress_callback:
            progress_callback(i + 1, total, row["session_id"][:8])

        file_path = Path(row["file_path"])
        if not file_path.exists():
            continue

        messages = parse_messages_for_search(file_path)
        for role, text, ts in messages:
            conn.execute(
                "INSERT INTO messages_fts (content, session_id, role, timestamp) VALUES (?, ?, ?, ?)",
                (text, row["session_id"], role, ts),
            )
        if row["session_id"] == VOICE_SESSION_ID:
            try:
                indexed_offset = file_path.stat().st_size
            except OSError:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO voice_fts_state (session_id, file_path, indexed_offset) VALUES (?, ?, ?)",
                (VOICE_SESSION_ID, str(file_path), indexed_offset),
            )

    conn.commit()
    conn.close()


def index_voice_transcript(
    file_path: Path | None = None,
    *,
    skip_if_running: bool = True,
) -> bool:
    """Upsert only the permanent Serena chat and refresh only its FTS rows."""

    path = (file_path or voice_transcript_path()).expanduser().resolve()
    with _index_update_lock(skip_if_running=skip_if_running) as acquired:
        if not acquired:
            return False
        if skip_if_running and not _db_write_available():
            return False
        try:
            conn = _get_db()
            if not _index_voice_transcript_locked(conn, path):
                conn.close()
                return False
            conn.commit()
            conn.close()
            return True
        except sqlite3.OperationalError as exc:
            if skip_if_running and _is_locked_error(exc):
                return False
            raise


def _voice_rows_since(
    path: Path, offset: int
) -> tuple[list[tuple[str, str, str, dict]], int]:
    """Read only complete user/assistant records appended after *offset*."""

    rows: list[tuple[str, str, str, dict]] = []
    with path.open("rb") as handle:
        handle.seek(max(0, offset))
        for raw in handle:
            try:
                record = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            role = record.get("type")
            if role not in {"user", "assistant"}:
                continue
            message = record.get("message")
            voice = record.get("serena_voice")
            if not isinstance(message, dict) or not isinstance(voice, dict):
                continue
            text = _extract_search_text(message.get("content"))
            if text:
                rows.append((role, text, str(record.get("timestamp") or ""), voice))
        return rows, handle.tell()


def _extract_search_text(content) -> str:
    """Keep the dedicated voice index to visible text blocks only."""

    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()


def _index_voice_transcript_locked(conn: sqlite3.Connection, path: Path) -> bool:
    """Synchronize one append-only transcript while the caller owns the lock."""

    try:
        stat = path.stat()
    except OSError:
        return False
    state = conn.execute(
        "SELECT file_path, indexed_offset FROM voice_fts_state WHERE session_id = ?",
        (VOICE_SESSION_ID,),
    ).fetchone()
    existing = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (VOICE_SESSION_ID,)
    ).fetchone()
    incremental = (
        state is not None
        and existing is not None
        and state["file_path"] == str(path)
        and 0 <= int(state["indexed_offset"]) <= stat.st_size
    )
    start = int(state["indexed_offset"]) if incremental else 0
    try:
        rows, end = _voice_rows_since(path, start)
    except OSError:
        return False

    if not incremental:
        meta = parse_voice_metadata(path)
        if meta is None:
            return False
        conn.execute("DELETE FROM messages_fts WHERE session_id = ?", (VOICE_SESSION_ID,))
        _upsert_session(conn, meta, agent="serena-voice")
    elif rows:
        last_voice = rows[-1][3]
        surfaces = {
            value for value in str(existing["devices_used"] or "").split(",") if value
        }
        surfaces.update(
            str(voice.get("surface") or "")
            for _, _, _, voice in rows
            if str(voice.get("surface") or "")
        )
        last_timestamp = _parse_dt(rows[-1][2]) or _parse_dt(existing["last_timestamp"])
        meta = SessionMeta(
            session_id=VOICE_SESSION_ID,
            project_dir="serena-voice",
            device=str(last_voice.get("surface") or existing["device"] or "voice"),
            first_message=str(existing["first_message"] or "Serena"),
            first_timestamp=_parse_dt(existing["first_timestamp"]),
            last_timestamp=last_timestamp,
            message_count=int(existing["message_count"] or 0) + len(rows),
            raw_message_count=int(existing["raw_message_count"] or 0) + len(rows),
            model=str(last_voice.get("model") or existing["model"] or "") or None,
            file_path=str(path),
            file_size=stat.st_size,
            file_mtime=stat.st_mtime,
            devices_used=sorted(surfaces),
        )
        _upsert_session(conn, meta, agent="serena-voice")

    for role, text, timestamp, _ in rows:
        conn.execute(
            "INSERT INTO messages_fts (content, session_id, role, timestamp) VALUES (?, ?, ?, ?)",
            (text, VOICE_SESSION_ID, role, timestamp),
        )
    conn.execute(
        "INSERT OR REPLACE INTO voice_fts_state (session_id, file_path, indexed_offset) VALUES (?, ?, ?)",
        (VOICE_SESSION_ID, str(path), end),
    )
    return True


_VOICE_RECALL_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_VOICE_RECALL_STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "are",
    "been",
    "before",
    "but",
    "can",
    "could",
    "did",
    "does",
    "for",
    "from",
    "have",
    "her",
    "him",
    "how",
    "into",
    "just",
    "like",
    "me",
    "my",
    "not",
    "now",
    "our",
    "she",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "they",
    "this",
    "was",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "will",
    "with",
    "would",
    "you",
    "your",
}


def _safe_voice_fts_query(text: str, *, max_terms: int = 10) -> str:
    terms: list[str] = []
    seen: set[str] = set()
    for match in _VOICE_RECALL_WORD.finditer(text.casefold()):
        term = match.group(0)
        if len(term) < 3 or term in _VOICE_RECALL_STOPWORDS or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= max_terms:
            break
    return " OR ".join(f'"{term}"' for term in terms)


def recall_voice_history(
    query: str,
    *,
    limit: int = 6,
    max_characters: int = 3_500,
) -> list[dict]:
    """Return bounded relevant text from the permanent Serena transcript.

    The caller supplies arbitrary speech, so this function constructs the FTS
    expression only from tokenizer-safe words rather than passing user syntax
    to SQLite MATCH.
    """

    expression = _safe_voice_fts_query(str(query or ""))
    if not expression or limit < 1 or max_characters < 1:
        return []
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(
            DB_PATH.expanduser().resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=0.2,
        )
        conn.row_factory = sqlite3.Row
    except sqlite3.OperationalError:
        return []
    try:
        rows = conn.execute(
            """
            SELECT content, role, timestamp, bm25(messages_fts) AS relevance
            FROM messages_fts
            WHERE messages_fts MATCH ?
              AND session_id = ?
            ORDER BY relevance, timestamp DESC
            LIMIT ?
            """,
            (expression, VOICE_SESSION_ID, int(limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()

    results: list[dict] = []
    used = 0
    seen: set[tuple[str, str]] = set()
    for row in rows:
        role = str(row["role"] or "")
        content = str(row["content"] or "").strip()
        key = role, content
        if not content or key in seen:
            continue
        seen.add(key)
        remaining = max_characters - used
        if remaining <= 0:
            break
        clipped = content[:remaining]
        results.append(
            {
                "role": role,
                "text": clipped,
                "timestamp": str(row["timestamp"] or ""),
            }
        )
        used += len(clipped)
    return results


def search_fts(query: str, limit: int = 20) -> list[dict]:
    """Search the FTS index."""
    conn = _get_db()

    count = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
    if count == 0:
        conn.close()
        return []

    sql = """
        SELECT messages_fts.session_id,
               snippet(messages_fts, 0, '>>>', '<<<', '...', 40) as snippet,
               messages_fts.role, messages_fts.timestamp
        FROM messages_fts
        JOIN sessions s ON s.session_id = messages_fts.session_id
        WHERE messages_fts.content MATCH ?
          AND COALESCE(s.is_teammate, 0) = 0
        ORDER BY rank
        LIMIT ?
    """
    try:
        results = conn.execute(sql, (query, limit)).fetchall()
    except sqlite3.OperationalError:
        # Voice transcription routinely includes hyphens and punctuation.
        # Keep ordinary FTS syntax for power users, but recover invalid input
        # as literal words instead of making `chats recall` crash.
        terms = [match.group(0) for match in _VOICE_RECALL_WORD.finditer(query)]
        fallback = " AND ".join(f'"{term}"' for term in terms[:12])
        if not fallback:
            conn.close()
            return []
        results = conn.execute(sql, (fallback, limit)).fetchall()

    out = []
    for r in results:
        session = conn.execute(
            "SELECT title, custom_title, project_dir, device, first_timestamp, starred, agent FROM sessions WHERE session_id = ?",
            (r["session_id"],),
        ).fetchone()

        out.append({
            "session_id": r["session_id"],
            "snippet": r["snippet"],
            "role": r["role"],
            "timestamp": r["timestamp"],
            "title": (session["custom_title"] or session["title"]) if session else "",
            "project_dir": session["project_dir"] if session else "",
            "device": session["device"] if session else "",
            "first_timestamp": session["first_timestamp"] if session else "",
            "starred": session["starred"] if session else 0,
            "agent": session["agent"] if session else "claude",
        })

    conn.close()
    return out


def list_sessions(
    project: str | None = None,
    device: str | None = None,
    tag: str | None = None,
    starred_only: bool = False,
    limit: int = 50,
) -> list[dict]:
    """List sessions from the index."""
    conn = _get_db()

    query = "SELECT s.* FROM sessions s"
    params = []
    conditions = ["COALESCE(s.is_teammate, 0) = 0"]

    if tag:
        query += " JOIN tags t ON s.session_id = t.session_id"
        conditions.append("t.tag = ?")
        params.append(tag)

    if project:
        conditions.append("s.project_dir LIKE ?")
        params.append(f"%{project}%")

    if device:
        conditions.append("s.device = ?")
        params.append(device)

    if starred_only:
        conditions.append("s.starred = 1")

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY s.starred DESC, s.last_timestamp DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    result = [dict(r) for r in rows]

    for r in result:
        tags = conn.execute(
            "SELECT tag FROM tags WHERE session_id = ?", (r["session_id"],)
        ).fetchall()
        r["tags"] = [t["tag"] for t in tags]
        # Resolve display title
        r["display_title"] = r.get("custom_title") or r.get("title") or "Untitled chat"

    conn.close()
    return result


def get_session(session_id_prefix: str) -> dict | None:
    """Get a session by full or partial ID."""
    conn = _get_db()

    row = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id_prefix,)
    ).fetchone()

    if not row:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE session_id LIKE ?",
            (session_id_prefix + "%",),
        ).fetchall()
        if len(rows) == 1:
            row = rows[0]
        elif len(rows) > 1:
            conn.close()
            raise ValueError(
                f"Ambiguous ID '{session_id_prefix}', matches: "
                + ", ".join(r["session_id"][:12] for r in rows[:5])
            )
        else:
            conn.close()
            return None

    result = dict(row)
    tags = conn.execute(
        "SELECT tag FROM tags WHERE session_id = ?", (result["session_id"],)
    ).fetchall()
    result["tags"] = [t["tag"] for t in tags]
    result["display_title"] = result.get("custom_title") or result.get("title") or "Untitled chat"

    conn.close()
    return result


def add_tag(session_id_prefix: str, tag: str):
    session = get_session(session_id_prefix)
    if not session:
        raise ValueError(f"No session found with ID '{session_id_prefix}'")

    sid = session["session_id"]
    conn = _get_db()
    conn.execute("INSERT OR IGNORE INTO tags (session_id, tag) VALUES (?, ?)", (sid, tag))
    conn.commit()
    conn.close()
    meta_sync.add_tag_meta(sid, tag)


def remove_tag(session_id_prefix: str, tag: str):
    session = get_session(session_id_prefix)
    if not session:
        raise ValueError(f"No session found with ID '{session_id_prefix}'")

    sid = session["session_id"]
    conn = _get_db()
    conn.execute("DELETE FROM tags WHERE session_id = ? AND tag = ?", (sid, tag))
    conn.commit()
    conn.close()
    meta_sync.remove_tag_meta(sid, tag)


def toggle_star(session_id_prefix: str) -> bool:
    """Toggle star on a session. Returns new starred state."""
    session = get_session(session_id_prefix)
    if not session:
        raise ValueError(f"No session found with ID '{session_id_prefix}'")

    sid = session["session_id"]
    new_val = 0 if session.get("starred") else 1
    meta_sync.set_starred(sid, bool(new_val))
    conn = _get_db()
    try:
        conn.execute("UPDATE sessions SET starred = ? WHERE session_id = ?", (new_val, sid))
        conn.commit()
    except sqlite3.OperationalError as e:
        if not _is_locked_error(e):
            raise
        # Synced metadata is authoritative; the next successful index refresh
        # will apply it to SQLite. Avoid surfacing transient refresh locks to UI.
    finally:
        conn.close()
    return bool(new_val)


def toggle_done(session_id_prefix: str) -> bool:
    """Toggle done state. Returns new done state."""
    from datetime import datetime, timezone
    session = get_session(session_id_prefix)
    if not session:
        raise ValueError(f"No session found with ID '{session_id_prefix}'")

    sid = session["session_id"]
    new_val = 0 if session.get("is_done") else 1
    done_at = datetime.now(timezone.utc).isoformat() if new_val else None
    meta_sync.set_done(sid, bool(new_val), done_at)
    conn = _get_db()
    try:
        conn.execute(
            "UPDATE sessions SET is_done = ?, done_at = ? WHERE session_id = ?",
            (new_val, done_at, sid),
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        if not _is_locked_error(e):
            raise
    finally:
        conn.close()
    return bool(new_val)


def set_title(session_id_prefix: str, title: str):
    """Set a custom title for a session."""
    session = get_session(session_id_prefix)
    if not session:
        raise ValueError(f"No session found with ID '{session_id_prefix}'")

    sid = session["session_id"]
    meta_sync.set_custom_title(sid, title)
    conn = _get_db()
    try:
        conn.execute("UPDATE sessions SET custom_title = ? WHERE session_id = ?", (title, sid))
        conn.commit()
    except sqlite3.OperationalError as e:
        if not _is_locked_error(e):
            raise
    finally:
        conn.close()


def list_projects(*, include_machinery: bool = False) -> list[dict]:
    """List projects with chat counts, one row per real project.

    Rows are keyed by the project root a chat's working directory resolves to,
    not by Claude's slug. The slug format changed between releases, so a single
    folder minted several of them and a project's history was split across
    duplicate rows; per-ticket worktrees and renamed folders fragmented it
    further.

    Two cases need the slug rather than the cwd. A chat run inside a Fleet
    worktree records a path under Serena's state directory, which says which
    machine ran it and nothing about which project it was for, and a handful of
    old sessions never recorded a cwd at all. Both fall back to the slug, which
    is mapped back to a real directory using the cwds other sessions recorded.
    """

    from core.config import claude_project_dir_for
    from core.projects import is_machinery, project_root

    conn = _get_db()
    rows = [
        dict(r)
        for r in conn.execute("""
            SELECT session_id, project_dir, device, first_timestamp,
                   COALESCE(NULLIF(last_cwd, ''), NULLIF(cwd, '')) AS cwd
            FROM sessions
            WHERE COALESCE(is_teammate, 0) = 0
        """).fetchall()
    ]
    conn.close()

    # Slug -> real root, learned from every session that did record a usable
    # cwd. This is what lets a Fleet-worktree session rejoin its own project.
    slug_to_root: dict[str, str] = {}
    for row in rows:
        cwd = row.get("cwd") or ""
        if not cwd or is_machinery(cwd):
            continue
        root = project_root(cwd)
        if not root:
            continue
        with suppress(Exception):
            slug_to_root.setdefault(str(claude_project_dir_for(cwd)), root)

    grouped: dict[str, dict] = {}
    for row in rows:
        cwd = row.get("cwd") or ""
        slug = str(row.get("project_dir") or "")
        if cwd and not is_machinery(cwd):
            key = project_root(cwd)
        else:
            key = slug_to_root.get(slug, "")
        machinery = False
        if not key:
            # No project could be recovered: this really is scratch space.
            key = slug or cwd
            machinery = is_machinery(cwd) or is_machinery(slug) or slug.startswith("-tmp-")
        if machinery and not include_machinery:
            continue

        entry = grouped.get(key)
        if entry is None:
            grouped[key] = {
                "project_key": key,
                "project_dir": slug,
                "cwd": key if key.startswith(("/", "C:", "~")) else (cwd or key),
                "device": row.get("device"),
                "chat_count": 1,
                "latest": row.get("first_timestamp"),
                "earliest": row.get("first_timestamp"),
                "is_machinery": machinery,
                "source_project_dirs": [slug],
            }
            continue
        entry["chat_count"] += 1
        if slug not in entry["source_project_dirs"]:
            entry["source_project_dirs"].append(slug)
        stamp = row.get("first_timestamp") or ""
        if stamp > (entry.get("latest") or ""):
            entry["latest"] = stamp
            entry["project_dir"] = slug
            entry["device"] = row.get("device")
        if not entry.get("earliest") or (stamp and stamp < entry["earliest"]):
            entry["earliest"] = stamp

    return sorted(
        grouped.values(), key=lambda item: item.get("latest") or "", reverse=True
    )


def delete_session(session_id_prefix: str, *, source: str = "unknown") -> str:
    """Remove a session from the index while retaining a recoverable copy."""
    session = get_session(session_id_prefix)
    if not session:
        raise ValueError(f"No session found with ID '{session_id_prefix}'")

    sid = session["session_id"]
    if sid == VOICE_SESSION_ID or session.get("agent") == "serena-voice":
        raise PermissionError("Serena's permanent conversation cannot be deleted")
    file_path = Path(session["file_path"])

    # Remove from DB
    conn = _get_db()
    conn.execute("DELETE FROM tags WHERE session_id = ?", (sid,))
    conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
    conn.execute("DELETE FROM messages_fts WHERE session_id = ?", (sid,))
    conn.commit()
    conn.close()

    # Keep the source transcript recoverable. Syncthing's trashcan only
    # protects remote deletions, so a local unlink can otherwise be permanent.
    if file_path.exists():
        recovery_dir = DATA_DIR / "deleted-sessions" / sid
        if recovery_dir.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            recovery_dir = recovery_dir.with_name(f"{sid}-{stamp}")
        recovery_dir.mkdir(parents=True, exist_ok=False)
        shutil.move(str(file_path), str(recovery_dir / file_path.name))
        manifest = {
            "session_id": sid,
            "original_path": str(file_path),
            "deleted_at": datetime.now().astimezone().isoformat(),
            "deleted_via": source,
            "metadata": meta_sync.get_meta(sid),
        }
        (recovery_dir / "recovery.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    # Remove from synced metadata
    meta_sync.delete_meta(sid)

    return str(file_path)


def drop_index():
    conn = _get_db()
    conn.executescript("""
        DELETE FROM sessions;
        DELETE FROM tags;
        DELETE FROM messages_fts;
        DELETE FROM knowledge_topics;
        DELETE FROM knowledge_files;
        DELETE FROM knowledge_fts;
        DELETE FROM session_topics;
    """)
    conn.commit()
    conn.close()


# ── Knowledge indexing ────────────────────────────────────────

def update_knowledge_index(force: bool = False, progress_callback=None) -> tuple[int, int]:
    """Scan knowledge directory and index topics + files. Returns (new, updated)."""
    from core.config import KNOWLEDGE_DIR
    from knowledge.reader import list_topics, get_topic_files

    conn = _get_db()
    existing = {}
    for row in conn.execute("SELECT slug, modified FROM knowledge_topics"):
        existing[row["slug"]] = row["modified"]

    topics = list_topics()
    new_count = 0
    updated_count = 0

    for i, t in enumerate(topics):
        if progress_callback:
            progress_callback(i + 1, len(topics), t["slug"])

        if not force and t["slug"] in existing:
            if existing[t["slug"]] == t["modified"]:
                continue
            updated_count += 1
        else:
            if t["slug"] in existing:
                updated_count += 1
            else:
                new_count += 1

        conn.execute("""
            INSERT OR REPLACE INTO knowledge_topics
            (slug, title, description, file_count, total_size, modified)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (t["slug"], t["title"], t["description"], t["file_count"], t["total_size"], t["modified"]))

        # Index individual files
        conn.execute("DELETE FROM knowledge_files WHERE topic_slug = ?", (t["slug"],))
        for f in get_topic_files(t["slug"]):
            stat = Path(f["path"]).stat()
            conn.execute("""
                INSERT OR REPLACE INTO knowledge_files
                (topic_slug, filename, file_path, file_size, file_mtime)
                VALUES (?, ?, ?, ?, ?)
            """, (t["slug"], f["name"], f["path"], f["size"], stat.st_mtime))

    # Remove topics that no longer exist on disk
    indexed_slugs = {t["slug"] for t in topics}
    for slug in list(existing.keys()):
        if slug not in indexed_slugs:
            conn.execute("DELETE FROM knowledge_topics WHERE slug = ?", (slug,))
            conn.execute("DELETE FROM knowledge_files WHERE topic_slug = ?", (slug,))
            conn.execute("DELETE FROM knowledge_fts WHERE topic_slug = ?", (slug,))

    conn.commit()
    conn.close()
    return new_count, updated_count


def build_knowledge_fts(progress_callback=None):
    """Build FTS index for knowledge files."""
    conn = _get_db()
    conn.execute("DELETE FROM knowledge_fts")

    rows = conn.execute("SELECT topic_slug, filename, file_path FROM knowledge_files").fetchall()

    for i, row in enumerate(rows):
        if progress_callback:
            progress_callback(i + 1, len(rows), row["filename"])
        fp = Path(row["file_path"])
        if not fp.exists():
            continue
        text = fp.read_text(errors="replace")
        conn.execute(
            "INSERT INTO knowledge_fts (content, topic_slug, filename) VALUES (?, ?, ?)",
            (text, row["topic_slug"], row["filename"]),
        )

    conn.commit()
    conn.close()


def search_knowledge_fts(query: str, limit: int = 20) -> list[dict]:
    """Search knowledge FTS index."""
    conn = _get_db()
    try:
        count = conn.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0]
    except sqlite3.OperationalError:
        conn.close()
        return []
    if count == 0:
        conn.close()
        return []

    results = conn.execute("""
        SELECT topic_slug, filename,
               snippet(knowledge_fts, 0, '>>>', '<<<', '...', 40) as snippet
        FROM knowledge_fts
        WHERE content MATCH ?
        ORDER BY rank
        LIMIT ?
    """, (query, limit)).fetchall()

    out = []
    for r in results:
        topic = conn.execute(
            "SELECT title, description FROM knowledge_topics WHERE slug = ?",
            (r["topic_slug"],),
        ).fetchone()
        out.append({
            "source": "knowledge",
            "topic_slug": r["topic_slug"],
            "filename": r["filename"],
            "snippet": r["snippet"],
            "topic_title": topic["title"] if topic else r["topic_slug"],
            "topic_description": topic["description"] if topic else "",
        })

    conn.close()
    return out


def unified_search(query: str, limit: int = 30) -> list[dict]:
    """Search across chats, knowledge, and memories."""
    results = []

    # Chat messages
    chat_results = search_fts(query, limit=limit)
    for r in chat_results:
        r["source"] = "chat"
        results.append(r)

    # Knowledge files
    k_results = search_knowledge_fts(query, limit=limit)
    results.extend(k_results)

    # Memories
    try:
        from memory.v2 import MemoryV2Store

        if MemoryV2Store.authority_is_active():
            store = MemoryV2Store()
            for hit in store.retrieve(query, limit=limit, surface="private"):
                results.append(
                    {
                        "source": "memory",
                        "snippet": hit.record.content[:200],
                        "memory_id": hit.record.record_id,
                        "memory_type": hit.record.record_type,
                        "score": hit.score,
                    }
                )
        else:
            from memory.store import search_memories

            mem_results = search_memories(query)
            for m in mem_results:
                results.append(
                    {
                        "source": "memory",
                        "snippet": m["content"][:200],
                        "memory_id": m["id"],
                        "memory_type": m["type"],
                    }
                )
    except Exception:
        pass

    return results


# ── Cross-linking ─────────────────────────────────────────────

def link_session_topic(session_id: str, topic_slug: str, link_type: str = "manual"):
    """Link a session to a knowledge topic."""
    conn = _get_db()
    conn.execute(
        "INSERT OR IGNORE INTO session_topics (session_id, topic_slug, link_type) VALUES (?, ?, ?)",
        (session_id, topic_slug, link_type),
    )
    conn.commit()
    conn.close()


def unlink_session_topic(session_id: str, topic_slug: str):
    """Remove a link between a session and a topic."""
    conn = _get_db()
    conn.execute(
        "DELETE FROM session_topics WHERE session_id = ? AND topic_slug = ?",
        (session_id, topic_slug),
    )
    conn.commit()
    conn.close()


def get_session_topics(session_id: str) -> list[dict]:
    """Get knowledge topics linked to a session."""
    conn = _get_db()
    rows = conn.execute("""
        SELECT kt.slug, kt.title, kt.description, st.link_type
        FROM session_topics st
        JOIN knowledge_topics kt ON st.topic_slug = kt.slug
        WHERE st.session_id = ?
    """, (session_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_topic_sessions(topic_slug: str) -> list[dict]:
    """Get sessions linked to a knowledge topic."""
    conn = _get_db()
    rows = conn.execute("""
        SELECT s.session_id, s.title, s.custom_title, s.first_timestamp, st.link_type
        FROM session_topics st
        JOIN sessions s ON st.session_id = s.session_id
        WHERE st.topic_slug = ?
        ORDER BY s.first_timestamp DESC
    """, (topic_slug,)).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["display_title"] = d.get("custom_title") or d.get("title") or "Untitled"
        result.append(d)
    return result


def list_knowledge_topics() -> list[dict]:
    """List knowledge topics from the index DB."""
    conn = _get_db()
    rows = conn.execute("""
        SELECT kt.*, COUNT(st.session_id) as linked_sessions
        FROM knowledge_topics kt
        LEFT JOIN session_topics st ON kt.slug = st.topic_slug
        GROUP BY kt.slug
        ORDER BY kt.modified DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Usage stats ───────────────────────────────────────────────

# USD per 1M tokens, (input, output, cache_read, cache_create)
_MODEL_RATES = {
    "opus": (15.0, 75.0, 1.50, 18.75),
    "sonnet": (3.0, 15.0, 0.30, 3.75),
    "haiku": (0.80, 4.0, 0.08, 1.00),
}
_DEFAULT_RATES = _MODEL_RATES["sonnet"]


def _model_tier(model: str | None) -> str:
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return "unknown"


def _model_cost_usd(model: str | None, inp: int, out: int, cr: int, cc: int) -> float:
    rates = _MODEL_RATES.get(_model_tier(model), _DEFAULT_RATES)
    return (inp * rates[0] + out * rates[1] + cr * rates[2] + cc * rates[3]) / 1_000_000.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _achievements(summary: dict) -> list[dict]:
    """Compute achievement badges from already-aggregated stats."""
    rules = [
        ("century",    "100 Prompts Club",  "100+ sessions logged",           summary["sessions"] >= 100),
        ("streak14",   "Two-Week Streak",   "14-day coding streak",            summary["longest_streak"] >= 14),
        ("streak7",    "One-Week Streak",   "7-day coding streak",             summary["longest_streak"] >= 7),
        ("night_owl",  "Night Owl",         "Peak hour between 10pm and 3am",  summary["peak_hour"] is not None and summary["peak_hour"] in (22, 23, 0, 1, 2, 3)),
        ("early_bird", "Early Bird",        "Peak hour between 5am and 8am",   summary["peak_hour"] is not None and summary["peak_hour"] in (5, 6, 7, 8)),
        ("polyglot",   "Polyglot",          "Active in 5+ projects",           summary["distinct_projects"] >= 5),
        ("marathoner", "Marathoner",        "Single session over 2 hours",     summary["longest_session_seconds"] >= 7200),
        ("veteran",    "Veteran",           "30+ active days",                 summary["active_days"] >= 30),
        ("collector",  "Collector",         "10+ starred sessions",            summary["starred_count"] >= 10),
        ("scholar",    "Scholar",           "10+ knowledge topics",            summary["knowledge_topics"] >= 10),
        ("scribe",     "Scribe",            "20+ memories saved",              summary["memory_count"] >= 20),
        ("cache_pro",  "Cache Connoisseur", "Cache hit rate over 90%",         summary["cache_hit_rate"] >= 0.9),
    ]
    return [{"key": k, "label": lbl, "desc": desc, "unlocked": bool(ok)} for k, lbl, desc, ok in rules]


def _compute_streaks(days: list[str]) -> tuple[int, int]:
    """Given sorted list of ISO dates (YYYY-MM-DD), compute (current, longest)."""
    from datetime import date, timedelta
    if not days:
        return (0, 0)
    day_set = set(days)
    today = date.today()
    current = 0
    cursor = today
    if cursor.isoformat() not in day_set:
        cursor = today - timedelta(days=1)
    while cursor.isoformat() in day_set:
        current += 1
        cursor -= timedelta(days=1)
    longest = 0
    run = 1
    prev = date.fromisoformat(days[0])
    for d_str in days[1:]:
        d = date.fromisoformat(d_str)
        if (d - prev).days == 1:
            run += 1
        else:
            longest = max(longest, run)
            run = 1
        prev = d
    longest = max(longest, run)
    return (current, longest)


def get_usage_stats(range_days: int | None = None) -> dict:
    """Compute usage dashboard stats.

    range_days=None returns all-time totals. Streaks are always all-time.
    Heatmap window is the range (or 365 days when range is None).
    """
    from datetime import datetime, timedelta, timezone

    conn = _get_db()

    where = ""
    params: list = []
    if range_days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=range_days)).isoformat()
        where = "WHERE first_timestamp >= ?"
        params = [cutoff]

    totals = conn.execute(f"""
        SELECT COUNT(*) AS sessions,
               COALESCE(SUM(COALESCE(raw_message_count, message_count)), 0) AS messages,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
               COALESCE(SUM(cache_create_tokens), 0) AS cache_create_tokens,
               COUNT(DISTINCT DATE(first_timestamp, 'localtime')) AS active_days,
               MIN(first_timestamp) AS earliest,
               MAX(first_timestamp) AS latest
        FROM sessions {where}
    """, params).fetchone()

    model_conj = "AND" if where else "WHERE"

    hour_row = conn.execute(f"""
        SELECT CAST(strftime('%H', first_timestamp, 'localtime') AS INTEGER) AS hour,
               COUNT(*) AS c
        FROM sessions {where}{' AND' if where else ' WHERE'} first_timestamp IS NOT NULL
        GROUP BY hour
        ORDER BY c DESC
        LIMIT 1
    """, params).fetchone()

    fav = conn.execute(f"""
        SELECT model, COUNT(*) AS c
        FROM sessions {where}{' AND' if where else ' WHERE'} model IS NOT NULL AND model != ''
        GROUP BY model
        ORDER BY c DESC
        LIMIT 1
    """, params).fetchone()

    heatmap_days = range_days if range_days else 365
    heatmap_cutoff = (datetime.now(timezone.utc) - timedelta(days=heatmap_days - 1)).isoformat()
    heatmap_rows = conn.execute("""
        SELECT DATE(first_timestamp, 'localtime') AS day,
               COUNT(*) AS sessions,
               COALESCE(SUM(COALESCE(raw_message_count, message_count)), 0) AS messages,
               COALESCE(SUM(input_tokens + output_tokens + cache_create_tokens), 0) AS tokens
        FROM sessions
        WHERE first_timestamp >= ?
        GROUP BY day
        ORDER BY day
    """, [heatmap_cutoff]).fetchall()

    all_day_rows = conn.execute("""
        SELECT DISTINCT DATE(first_timestamp, 'localtime') AS day
        FROM sessions
        WHERE first_timestamp IS NOT NULL
        ORDER BY day
    """).fetchall()
    all_days = [r["day"] for r in all_day_rows if r["day"]]
    current_streak, longest_streak = _compute_streaks(all_days)

    models = conn.execute(f"""
        SELECT model,
               COUNT(*) AS sessions,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
               COALESCE(SUM(cache_create_tokens), 0) AS cache_create_tokens
        FROM sessions {where}{' AND' if where else ' WHERE'} model IS NOT NULL AND model != ''
        GROUP BY model
        ORDER BY sessions DESC
    """, params).fetchall()

    total_tokens = (
        totals["input_tokens"] + totals["output_tokens"] + totals["cache_create_tokens"]
    )

    # Hour-of-day histogram (24 buckets)
    hour_rows = conn.execute(f"""
        SELECT CAST(strftime('%H', first_timestamp, 'localtime') AS INTEGER) AS hour,
               COUNT(*) AS c
        FROM sessions {where}{' AND' if where else ' WHERE'} first_timestamp IS NOT NULL
        GROUP BY hour
    """, params).fetchall()
    hourly = [0] * 24
    for r in hour_rows:
        if r["hour"] is not None and 0 <= r["hour"] < 24:
            hourly[r["hour"]] = r["c"]

    # Top projects by session count
    project_rows = conn.execute(f"""
        SELECT project_dir,
               COUNT(*) AS sessions,
               COALESCE(SUM(COALESCE(raw_message_count, message_count)), 0) AS messages,
               COALESCE(SUM(input_tokens + output_tokens + cache_create_tokens), 0) AS tokens
        FROM sessions {where}{' AND' if where else ' WHERE'} project_dir IS NOT NULL AND project_dir != ''
        GROUP BY project_dir
        ORDER BY sessions DESC
        LIMIT 8
    """, params).fetchall()
    top_projects = [dict(r) for r in project_rows]
    for p in top_projects:
        p["pct"] = round(p["sessions"] / totals["sessions"] * 100, 1) if totals["sessions"] else 0

    distinct_projects_row = conn.execute(f"""
        SELECT COUNT(DISTINCT project_dir) AS n
        FROM sessions {where}{' AND' if where else ' WHERE'} project_dir IS NOT NULL AND project_dir != ''
    """, params).fetchone()
    distinct_projects = distinct_projects_row["n"] if distinct_projects_row else 0

    # Session duration (seconds) — avg, median, longest
    duration_rows = conn.execute(f"""
        SELECT session_id,
               (julianday(last_timestamp) - julianday(first_timestamp)) * 86400 AS secs
        FROM sessions {where}{' AND' if where else ' WHERE'}
             first_timestamp IS NOT NULL AND last_timestamp IS NOT NULL
    """, params).fetchall()
    durations = [max(0.0, r["secs"] or 0.0) for r in duration_rows]
    avg_session_seconds = sum(durations) / len(durations) if durations else 0.0
    median_session_seconds = _median(durations)
    longest_session_seconds = max(durations) if durations else 0.0
    longest_session_id = None
    if durations:
        for r in duration_rows:
            if r["secs"] and r["secs"] >= longest_session_seconds - 0.5:
                longest_session_id = r["session_id"]
                break

    # Cache hit rate: cache_read / (cache_read + fresh input + cache_create)
    denom = totals["cache_read_tokens"] + totals["input_tokens"] + totals["cache_create_tokens"]
    cache_hit_rate = (totals["cache_read_tokens"] / denom) if denom else 0.0

    # Cost in USD by model tier
    cost_usd = 0.0
    for m in models:
        cost_usd += _model_cost_usd(
            m["model"], m["input_tokens"], m["output_tokens"],
            m["cache_read_tokens"], m["cache_create_tokens"],
        )

    # Starred count (all-time, not scoped to range — starred is a cross-cutting signal)
    starred_count_row = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE COALESCE(starred, 0) = 1 AND COALESCE(is_teammate, 0) = 0"
    ).fetchone()
    starred_count = starred_count_row["n"] if starred_count_row else 0

    # Knowledge + memory counts
    knowledge_topic_row = conn.execute("SELECT COUNT(*) AS n FROM knowledge_topics").fetchone()
    knowledge_file_row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(file_size), 0) AS bytes FROM knowledge_files"
    ).fetchone()
    knowledge_topics = knowledge_topic_row["n"] if knowledge_topic_row else 0
    knowledge_files = knowledge_file_row["n"] if knowledge_file_row else 0
    knowledge_bytes = knowledge_file_row["bytes"] if knowledge_file_row else 0

    try:
        from memory.store import list_memories as _list_memories
        memory_count = len(_list_memories())
    except Exception:
        memory_count = 0

    conn.close()

    achievement_inputs = {
        "sessions": totals["sessions"],
        "longest_streak": longest_streak,
        "peak_hour": hour_row["hour"] if hour_row else None,
        "distinct_projects": distinct_projects,
        "longest_session_seconds": longest_session_seconds,
        "active_days": totals["active_days"],
        "starred_count": starred_count,
        "knowledge_topics": knowledge_topics,
        "memory_count": memory_count,
        "cache_hit_rate": cache_hit_rate,
    }
    achievements = _achievements(achievement_inputs)

    _ = model_conj  # kept for clarity in SQL shape

    return {
        "range_days": range_days,
        "sessions": totals["sessions"],
        "messages": totals["messages"],
        "total_tokens": total_tokens,
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        "cache_read_tokens": totals["cache_read_tokens"],
        "cache_create_tokens": totals["cache_create_tokens"],
        "active_days": totals["active_days"],
        "earliest": totals["earliest"],
        "latest": totals["latest"],
        "peak_hour": hour_row["hour"] if hour_row else None,
        "favorite_model": fav["model"] if fav else None,
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "heatmap": [dict(r) for r in heatmap_rows],
        "heatmap_days": heatmap_days,
        "models": [dict(r) for r in models],
        "hourly": hourly,
        "top_projects": top_projects,
        "distinct_projects": distinct_projects,
        "avg_session_seconds": avg_session_seconds,
        "median_session_seconds": median_session_seconds,
        "longest_session_seconds": longest_session_seconds,
        "longest_session_id": longest_session_id,
        "cache_hit_rate": cache_hit_rate,
        "cost_usd": cost_usd,
        "starred_count": starred_count,
        "knowledge_topics": knowledge_topics,
        "knowledge_files": knowledge_files,
        "knowledge_bytes": knowledge_bytes,
        "memory_count": memory_count,
        "achievements": achievements,
    }
