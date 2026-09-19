"""Knowledge metadata, transactional file writes, receipts and review proposals.

Markdown remains canonical. A small write-ahead journal recovers paired note /
INDEX replacements before cooperating readers proceed. Telemetry contains hashes,
never raw queries or feedback speech. Proposal candidates are explicit review data.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import yaml

from core import config

MAX_HITS = 10_000
_LOCK = threading.RLock()
_FRONT = re.compile(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)', re.S)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def metadata(text: str) -> dict:
    match = _FRONT.match(text)
    if not match:
        return {}
    try:
        value = yaml.safe_load(match[1])
        return value if isinstance(value, dict) else {}
    except yaml.YAMLError:
        # Legacy notes sometimes have unquoted colons in unrelated titles.
        # Read routing scalars without rewriting their private header or body.
        result = {}
        for name in ('trigger', 'last_verified', 'title', 'category'):
            scalar = re.search(r'^' + name + r':\s*([^\n]*)', match[1], re.M)
            if scalar:
                result[name] = scalar[1].strip().strip('\"\'')
        return result


def with_metadata(text: str, slug: str, filename: str) -> str:
    """Add missing routing metadata without rewriting existing YAML or body."""
    meta = metadata(text)
    fields = []
    if not isinstance(meta.get('trigger'), str) or not meta['trigger'].strip():
        title = meta.get('title') or next((line[2:] for line in text.splitlines() if line.startswith('# ')), filename)
        trigger = ' '.join(f'Use for {slug.replace("-", " ")}: {title}'.split())[:500]
        fields.append('trigger: ' + json.dumps(trigger, ensure_ascii=False))
    if not meta.get('last_verified'):
        known = re.search(r'<!--\s*last_verified:\s*(\d{4}-\d{2}-\d{2})\s*-->', text)
        fields.append('last_verified: ' + (known[1] if known else '1970-01-01'))
    if not fields:
        return text
    match = _FRONT.match(text)
    if match:
        # Replace only the missing/invalid routing fields; preserve other text.
        header = match[1]
        for name in ('trigger', 'last_verified'):
            if any(line.startswith(name + ':') for line in fields):
                header = re.sub(r'^' + name + r':[^\n]*(?:\n|$)', '', header, flags=re.M)
        return '---\n' + header.rstrip() + '\n' + '\n'.join(fields) + '\n---\n' + text[match.end():]
    return '---\n' + '\n'.join(fields) + '\n---\n' + text


def note_path(slug: str, filename: str, root: Path | None = None) -> Path:
    root = Path(root or config.KNOWLEDGE_DIR).resolve()
    if not re.fullmatch(r'[\w-]+', slug) or not filename.endswith('.md') or Path(filename).name != filename or '\\' in filename:
        raise ValueError('Expected a topic slug and plain Markdown filename')
    path = root / slug / filename
    if path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('Knowledge paths must stay inside the knowledge root')
    return path


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.knowledge-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _recover(root: Path) -> None:
    journal = root / '.knowledge-write.json'
    if not journal.exists():
        return
    value = json.loads(journal.read_text(encoding="utf-8"))
    path = note_path(value['slug'], value['file'], root)
    # Roll forward after a process interruption. Both new values were durably
    # prepared before either canonical path was changed.
    atomic_text(path, value['note'])
    atomic_text(root / 'INDEX.md', value['index'])
    journal.unlink()


@contextmanager
def locked(root: Path | None = None):
    root = Path(root or config.KNOWLEDGE_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with _LOCK, (root / '.knowledge.lock').open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            if stream.seek(0, 2) == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            _recover(root)
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def index_upsert(text: str, slug: str, title: str, trigger: str) -> str:
    line = f'- [{title.replace(chr(10), " ").replace("]", "")[:120]}](./{slug}/) - {" ".join(trigger.split())[:500]}\n'
    pattern = re.compile(r'^- \[[^\]]+\]\(\./' + re.escape(slug) + r'/?\)[^\n]*(?:\n|$)', re.M)
    if pattern.search(text):
        return pattern.sub(lambda _: line, text, count=1)
    return text + ('' if not text or text.endswith('\n') else '\n') + line


def _save_note(slug: str, filename: str, text: str, root: Path) -> str:
    path = note_path(slug, filename, root)
    text = with_metadata(text.rstrip() + '\n', slug, filename)
    meta = metadata(text)
    index = root / 'INDEX.md'
    if index.is_symlink():
        raise ValueError('INDEX must not be a symlink')
    old_note = path.read_text(encoding="utf-8") if path.exists() else None
    old_index = index.read_text(encoding="utf-8") if index.exists() else None
    new_index = index_upsert(old_index or '# Knowledge Base\n\n## Topics\n\n', slug,
                             str(meta.get('title') or slug), str(meta['trigger']))
    journal = root / '.knowledge-write.json'
    atomic_text(journal, json.dumps(dict(slug=slug, file=filename, note=text, index=new_index)))
    try:
        atomic_text(path, text)
        atomic_text(index, new_index)
    except OSError:
        # Restore only the note; failed atomic INDEX replacement left its old
        # bytes intact. Keep journal on rollback failure for deterministic recovery.
        if old_note is None:
            path.unlink(missing_ok=True)
        else:
            atomic_text(path, old_note)
        journal.unlink()
        raise
    journal.unlink()
    return str(path)


def save_note(slug: str, filename: str, text: str, *, root: Path | None = None) -> str:
    root = Path(root or config.KNOWLEDGE_DIR).resolve()
    note_path(slug, filename, root)
    with locked(root):
        return _save_note(slug, filename, text, root)


def backfill(root: Path | None = None) -> dict:
    root = Path(root or config.KNOWLEDGE_DIR).resolve()
    result = {'updated': 0, 'checked': 0, 'errors': []}
    if not root.exists():
        return result
    with locked(root):
        for path in sorted(root.glob('*/*.md')):
            if path.parent.name.startswith('.'):
                continue
            try:
                note_path(path.parent.name, path.name, root)
                original = path.read_text(encoding="utf-8")
                updated = with_metadata(original, path.parent.name, path.name)
                result['checked'] += 1
                if updated != original:
                    atomic_text(path, updated)
                    result['updated'] += 1
            except (OSError, ValueError) as exc:
                result['errors'].append({'file': str(path.relative_to(root)), 'error': str(exc)})
    return result


@contextmanager
def database():
    path = Path(config.DATA_DIR) / 'knowledge.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS knowledge_hits(
                id TEXT PRIMARY KEY, ts REAL, slug TEXT, file TEXT,
                query_sha256 TEXT, surface TEXT, caller TEXT, content_sha256 TEXT);
            CREATE INDEX IF NOT EXISTS knowledge_hits_time ON knowledge_hits(ts);
            CREATE TABLE IF NOT EXISTS knowledge_proposals(
                id TEXT PRIMARY KEY, ts REAL, slug TEXT, file TEXT, kind TEXT,
                receipt_id TEXT, reason_sha256 TEXT, before_sha256 TEXT,
                candidate TEXT, state TEXT, reviewed_at REAL);
        ''')
        with db:
            yield db
    finally:
        db.close()


def record_hit(slug: str, filename: str, *, query: str = '', surface: str, caller: str,
               content: str | None = None, root: Path | None = None) -> str:
    path = note_path(slug, filename, root)
    if content is None:
        text = path.read_text(encoding="utf-8")
    else:
        # Cached index content is not proof of a present file. A receipt for a
        # deleted note would be a false retrieval record, and callers rely on
        # this refusal to drop stale FTS rows.
        if not path.is_file():
            raise ValueError(f'Knowledge file no longer exists: {slug}/{filename}')
        text = content
    identity = uuid.uuid4().hex
    with database() as db:
        db.execute('INSERT INTO knowledge_hits VALUES (?,?,?,?,?,?,?,?)',
                   (identity, time.time(), slug, filename, digest(query), surface[:80], caller[:120], digest(text)))
        db.execute('DELETE FROM knowledge_hits WHERE id IN '
                   '(SELECT id FROM knowledge_hits ORDER BY ts DESC LIMIT -1 OFFSET ?)', (MAX_HITS,))
    return identity


def hits() -> list[dict]:
    with database() as db:
        return [dict(r) for r in db.execute('SELECT * FROM knowledge_hits ORDER BY ts DESC')]


def propose_feedback(slug: str, filename: str, kind: str, *, receipt_id: str,
                     corrected_content: str = '', reason: str = '') -> dict:
    if kind not in {'relevance', 'factual_correction'}:
        raise ValueError('Expected relevance or factual_correction')
    if kind == 'factual_correction' and not corrected_content.strip():
        raise ValueError('Factual correction requires complete corrected content')
    with database() as db:
        hit = db.execute('SELECT * FROM knowledge_hits WHERE id=? AND slug=? AND file=?',
                         (receipt_id, slug, filename)).fetchone()
        if hit is None:
            raise ValueError('Feedback target was not returned by this receipt')
        identity = uuid.uuid4().hex
        db.execute('INSERT INTO knowledge_proposals VALUES (?,?,?,?,?,?,?,?,?,?,NULL)',
                   (identity, time.time(), slug, filename, kind, receipt_id, digest(reason),
                    hit['content_sha256'], corrected_content if kind == 'factual_correction' else '', 'proposed'))
        return dict(db.execute('SELECT * FROM knowledge_proposals WHERE id=?', (identity,)).fetchone())


def proposals() -> list[dict]:
    with database() as db:
        return [dict(r) for r in db.execute('SELECT * FROM knowledge_proposals ORDER BY ts DESC LIMIT 1000')]


def review_proposal(identity: str, action: str) -> dict:
    if action not in {'approve', 'reject'}:
        raise ValueError('Expected approve or reject')
    root = Path(config.KNOWLEDGE_DIR).resolve()
    with locked(root), database() as db:
        db.execute('BEGIN IMMEDIATE')
        proposal = db.execute('SELECT * FROM knowledge_proposals WHERE id=?', (identity,)).fetchone()
        if proposal is None or proposal['state'] != 'proposed':
            raise ValueError('No pending knowledge proposal')
        if action == 'approve' and proposal['kind'] == 'factual_correction':
            path = note_path(proposal['slug'], proposal['file'], root)
            if digest(path.read_text(encoding="utf-8")) != proposal['before_sha256']:
                raise ValueError('Canonical knowledge changed since retrieval; review a fresh proposal')
            _save_note(proposal['slug'], proposal['file'], proposal['candidate'], root)
        db.execute('UPDATE knowledge_proposals SET state=?,reviewed_at=? WHERE id=?',
                   ('approved' if action == 'approve' else 'rejected', time.time(), identity))
        return dict(db.execute('SELECT * FROM knowledge_proposals WHERE id=?', (identity,)).fetchone())


def read_note(slug: str, filename: str, *, query: str = '', surface: str = 'knowledge',
              caller: str = 'reader', root: Path | None = None, include_receipt: bool = False) -> str:
    root = Path(root or config.KNOWLEDGE_DIR).resolve()
    path = note_path(slug, filename, root)
    with locked(root):
        text = path.read_text(encoding="utf-8")
        receipt = record_hit(slug, filename, query=query, surface=surface, caller=caller, content=text, root=root)
        return f'retrieval receipt: {receipt} {slug}/{filename}\n{text}' if include_receipt else text
