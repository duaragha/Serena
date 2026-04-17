"""SQLite index for session metadata and full-text search."""

import sqlite3
from pathlib import Path

from core.config import DATA_DIR, DB_PATH
from core import metadata as meta_sync
from core.parser import SessionMeta, parse_messages_for_search, parse_metadata
from core.scanner import scan_sessions
from chats.titles import generate_title


def _get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _create_tables(conn)
    _migrate(conn)
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
    }
    for col, sql in migrations.items():
        if col not in columns:
            conn.execute(sql)
    conn.commit()


def update_index(force: bool = False, progress_callback=None) -> tuple[int, int]:
    """Scan for new/changed sessions and update the index.

    Returns (new_count, updated_count).
    """
    conn = _get_db()

    existing = {}
    existing_paths = {}
    for row in conn.execute("SELECT session_id, file_size, file_mtime, file_path FROM sessions"):
        existing[row["session_id"]] = (row["file_size"], row["file_mtime"])
        existing_paths[row["session_id"]] = row["file_path"]

    discovered = list(scan_sessions())
    discovered_ids = {fp.stem for _, fp in discovered}

    # Prune zombie rows: indexed sessions whose jsonl no longer exists on disk.
    zombies = []
    for sid, path in existing_paths.items():
        if sid in discovered_ids:
            continue
        try:
            if not Path(path).exists():
                zombies.append(sid)
        except Exception:
            zombies.append(sid)
    for sid in zombies:
        conn.execute("DELETE FROM tags WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM messages_fts WHERE session_id = ?", (sid,))
        existing.pop(sid, None)
    all_meta = meta_sync.get_all_meta()
    new_count = 0
    updated_count = 0
    total = len(discovered)

    for i, (project_dir, file_path) in enumerate(discovered):
        session_id = file_path.stem

        if progress_callback:
            progress_callback(i + 1, total, session_id)

        if not force and session_id in existing:
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

        meta = parse_metadata(file_path, project_dir)
        _upsert_session(conn, meta, all_meta)

    conn.commit()
    conn.close()
    return new_count, updated_count


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
    if updates:
        params.append(session_id)
        conn.execute(f"UPDATE sessions SET {', '.join(updates)} WHERE session_id = ?", params)
    if "tags" in synced:
        for tag in synced["tags"]:
            conn.execute(
                "INSERT OR IGNORE INTO tags (session_id, tag) VALUES (?, ?)",
                (session_id, tag),
            )


def _upsert_session(conn: sqlite3.Connection, meta: SessionMeta, all_meta: dict | None = None):
    title = generate_title(meta.first_message)

    # Get synced metadata (stars, tags, custom_title) from the shared JSON
    if all_meta is not None:
        synced = all_meta.get(meta.session_id, {})
    else:
        synced = meta_sync.get_meta(meta.session_id)

    custom_title = synced.get("custom_title")
    starred = 1 if synced.get("starred") else 0
    synced_tags = synced.get("tags", [])

    conn.execute("""
        INSERT OR REPLACE INTO sessions
        (session_id, project_dir, cwd, last_cwd, device, first_message, title,
         custom_title, starred, first_timestamp, last_timestamp,
         message_count, model, git_branch, slug, file_path, file_size, file_mtime,
         input_tokens, output_tokens, cache_read_tokens, cache_create_tokens)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        meta.session_id, meta.project_dir, meta.cwd, meta.last_cwd, meta.device,
        meta.first_message, title, custom_title, starred,
        meta.first_timestamp.isoformat() if meta.first_timestamp else None,
        meta.last_timestamp.isoformat() if meta.last_timestamp else None,
        meta.message_count, meta.model, meta.git_branch, meta.slug,
        meta.file_path, meta.file_size, meta.file_mtime,
        meta.input_tokens, meta.output_tokens, meta.cache_read_tokens, meta.cache_create_tokens,
    ))

    # Apply synced tags
    if synced_tags:
        for tag in synced_tags:
            conn.execute(
                "INSERT OR IGNORE INTO tags (session_id, tag) VALUES (?, ?)",
                (meta.session_id, tag),
            )


def build_fts(progress_callback=None):
    """Build/rebuild the full-text search index."""
    conn = _get_db()
    conn.execute("DELETE FROM messages_fts")

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

    conn.commit()
    conn.close()


def search_fts(query: str, limit: int = 20) -> list[dict]:
    """Search the FTS index."""
    conn = _get_db()

    count = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
    if count == 0:
        conn.close()
        return []

    results = conn.execute("""
        SELECT session_id, snippet(messages_fts, 0, '>>>', '<<<', '...', 40) as snippet,
               role, timestamp
        FROM messages_fts
        WHERE content MATCH ?
        ORDER BY rank
        LIMIT ?
    """, (query, limit)).fetchall()

    out = []
    for r in results:
        session = conn.execute(
            "SELECT title, custom_title, project_dir, device, first_timestamp, starred FROM sessions WHERE session_id = ?",
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
    conditions = []

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
    conn = _get_db()
    conn.execute("UPDATE sessions SET starred = ? WHERE session_id = ?", (new_val, sid))
    conn.commit()
    conn.close()
    meta_sync.set_starred(sid, bool(new_val))
    return bool(new_val)


def set_title(session_id_prefix: str, title: str):
    """Set a custom title for a session."""
    session = get_session(session_id_prefix)
    if not session:
        raise ValueError(f"No session found with ID '{session_id_prefix}'")

    sid = session["session_id"]
    conn = _get_db()
    conn.execute("UPDATE sessions SET custom_title = ? WHERE session_id = ?", (title, sid))
    conn.commit()
    conn.close()
    meta_sync.set_custom_title(sid, title)


def list_projects() -> list[dict]:
    """List all projects with chat counts."""
    conn = _get_db()
    rows = conn.execute("""
        SELECT project_dir, device,
               COUNT(*) as chat_count,
               MAX(first_timestamp) as latest,
               MIN(first_timestamp) as earliest
        FROM sessions
        GROUP BY project_dir
        ORDER BY latest DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_session(session_id_prefix: str) -> str:
    """Delete a session's .jsonl file and remove it from the index. Returns the file path deleted."""
    session = get_session(session_id_prefix)
    if not session:
        raise ValueError(f"No session found with ID '{session_id_prefix}'")

    sid = session["session_id"]
    file_path = Path(session["file_path"])

    # Remove from DB
    conn = _get_db()
    conn.execute("DELETE FROM tags WHERE session_id = ?", (sid,))
    conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
    conn.execute("DELETE FROM messages_fts WHERE session_id = ?", (sid,))
    conn.commit()
    conn.close()

    # Delete the actual file
    if file_path.exists():
        file_path.unlink()

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
        from memory.store import search_memories
        mem_results = search_memories(query)
        for m in mem_results:
            results.append({
                "source": "memory",
                "snippet": m["content"][:200],
                "memory_id": m["id"],
                "memory_type": m["type"],
            })
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
