"""Derived, versioned repository briefs. Readers never generate or refresh."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from contextlib import closing, suppress
from datetime import datetime, timezone
from pathlib import Path

from core import code_index, config
from core.computer_knowledge import _redact

DRIFT_RATIO = 0.20
MAX_BRIEF_CHARS = 12_000
MAX_MANIFEST_CHARS = 400_000
MANIFEST_NAMES = {'package.json', 'pyproject.toml', 'Makefile', 'Cargo.toml', 'go.mod', 'README.md'}


def slug_for_key(key: str) -> str:
    # Registry keys may be portable paths. Preserve simple keys, hash all others
    # so distinct explicit clones cannot collapse onto one knowledge directory.
    if re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', key):
        return 'repo-' + key
    name = re.sub(r'[^a-z0-9]+', '-', key.lower()).strip('-')[-60:] or 'repository'
    return 'repo-' + name + '-' + hashlib.sha256(key.encode()).hexdigest()[:16]


def directory(key: str) -> Path:
    root = Path(config.KNOWLEDGE_DIR).resolve()
    path = root / slug_for_key(key)
    if path.is_symlink():
        raise ValueError('Brief directory must not be a symlink')
    return path


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.brief-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def head_sha(root: Path) -> str:
    try:
        result = subprocess.run(['git', '--no-optional-locks', '-C', str(root),
                                 'rev-parse', '--verify', 'HEAD'], capture_output=True,
                                text=True, timeout=5, check=False)
        value = result.stdout.strip()
        return value if result.returncode == 0 and re.fullmatch(r'[a-f0-9]{40,64}', value) else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def snapshot(key: str) -> dict:
    if not Path(code_index.DB_PATH).exists():
        raise ValueError('Repository is not indexed; run chats code refresh first')
    with closing(code_index._read_db()) as db:
        repo = db.execute('SELECT head_sha,indexed_at FROM code_repos WHERE repo_key=?', (key,)).fetchone()
        hashes = {row['rel_path']: row['content_hash'] for row in db.execute(
            'SELECT rel_path,content_hash FROM code_files WHERE repo_key=? ORDER BY rel_path', (key,))}
    return dict(repo_key=key, head_sha=repo['head_sha'] or '' if repo else '',
                indexed_at=repo['indexed_at'] if repo else '', file_hashes=hashes)


def _state(key: str) -> dict:
    try:
        value = json.loads((directory(key) / 'drift.json').read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def mark_drift(key: str) -> dict:
    current, old = snapshot(key), _state(key)
    before, after = old.get('file_hashes', {}), current['file_hashes']
    names = set(before) | set(after)
    ratio = sum(before.get(n) != after.get(n) for n in names) / max(1, len(names))
    moved = old.get('head_sha', '') != current['head_sha']
    reason = 'missing brief' if not (directory(key) / 'brief.md').is_file() else (
        'HEAD moved' if moved else 'changed-file ratio' if ratio > DRIFT_RATIO else '')
    stale = bool(old.get('stale') or reason)
    if stale:
        # Keep the previous baseline until regeneration succeeds. This is also
        # the durable retry queue consumed on subsequent index refreshes.
        state = {**old, 'repo_key': key, 'stale': True,
                 'stale_reason': reason or old.get('stale_reason', ''), 'changed_file_ratio': ratio}
        _atomic(directory(key) / 'drift.json', json.dumps(state, sort_keys=True, indent=2) + '\n')
    return dict(stale=stale, changed_file_ratio=ratio, head_moved=moved)


def is_stale(key: str) -> bool:
    return not (directory(key) / 'brief.md').is_file() or bool(_state(key).get('stale', True))


def generate(repo_key: str) -> str:
    with code_index._update_lock():
        registry = code_index.load_registry()
        if repo_key not in registry:
            raise ValueError(f'Unknown repository: {repo_key}')
        code_index.resolve_root(registry[repo_key])
        state = snapshot(repo_key)
        paths = sorted(state['file_hashes'])
        manifests = [p for p in paths if Path(p).name in MANIFEST_NAMES]
        # Stored chunks have already passed scanner path/ignore/binary/secret
        # guards. Never read raw files outside that approved corpus.
        with closing(code_index._read_db()) as db:
            rows = db.execute('SELECT rel_path,content FROM code_fts WHERE repo_key=? '
                              'AND chunk_ordinal=0 ORDER BY rel_path', (repo_key,)).fetchall()
            # A manifest longer than one 50-line chunk is not parseable from its
            # first chunk, so reassemble those few files in chunk order.
            complete = {name: '\n'.join(row['content'] for row in db.execute(
                'SELECT content FROM code_fts WHERE repo_key=? AND rel_path=? ORDER BY chunk_ordinal',
                (repo_key, name)))[:MAX_MANIFEST_CHARS] for name in manifests[:12]}
        content = {r['rel_path']: r['content'] for r in rows}
        layout = sorted({p.split('/')[0] + ('/' if '/' in p else '') for p in paths})
        entries = [p for p in paths if Path(p).stem in {'main', '__main__', 'cli', 'app', 'server', 'index'}]
        modules = [p for p in paths if Path(p).suffix in {'.py', '.ts', '.js', '.go', '.rs', '.swift'}]
        stores = [p for p, text in content.items() if re.search(r'\b(sqlite|sqlite3|postgres|redis|CREATE TABLE|database)\b', text, re.I)]
        commands = '\n'.join(f'### {p}\n```text\n{complete.get(p, content.get(p, ""))[:900]}\n```' for p in manifests[:6])
        build_commands, test_commands = [], []
        for name, source in complete.items():
            if Path(name).name == 'package.json':
                try:
                    package = json.loads(source)
                    for script in package.get('scripts', {}):
                        if re.fullmatch(r'[\w:-]+', script):
                            if 'test' in script or 'check' in script:
                                test_commands.append(f'`npm run {script}` (from {name})')
                            elif 'build' in script:
                                build_commands.append(f'`npm run {script}` (from {name})')
                except (ValueError, AttributeError):
                    pass
            if Path(name).name == 'Makefile':
                for target in re.findall(r'^([\w-]+):', source, re.M):
                    if 'test' in target or 'check' in target:
                        test_commands.append(f'`make {target}` (from {name})')
                    elif 'build' in target or target == 'all':
                        build_commands.append(f'`make {target}` (from {name})')
        if any(Path(p).name.startswith('test_') and p.endswith('.py') for p in paths):
            test_commands.append('`python -m pytest` (inferred from Python test files)')
        if 'Cargo.toml' in paths:
            build_commands.append('`cargo build` (Cargo.toml)')
            test_commands.append('`cargo test` (Cargo.toml)')
        if 'go.mod' in paths:
            build_commands.append('`go build ./...` (go.mod)')
            test_commands.append('`go test ./...` (go.mod)')
        def listing(items):
            return '\n'.join('- ' + p for p in items[:35]) or 'No evidence found in the indexed corpus.'
        now = datetime.now(timezone.utc).date().isoformat()
        text = (f'---\ntrigger: Architecture and code navigation for {slug_for_key(repo_key)}\nlast_verified: {now}\n'
                f'---\n# Repository brief: {repo_key}\n\nGenerated from the guarded code index; verify commands before running.\n\n'
                f'## Layout\n{listing(layout)}\n\n## Entry points\n{listing(entries)}\n\n'
                f'## Key modules\n{listing(modules)}\n\n## Data stores\n{listing(stores)}\n\n'
                f'## Build commands\n{listing(build_commands)}\n\n{commands}\n\n'
                f'## Test commands\n{listing(test_commands)}\n'
                f'\nTest files:\n{listing([p for p in paths if "test" in p.lower()])}\n')
        text = _redact(text)
        from fleet.context import redact_text
        text = redact_text(text)[0]
        from core.knowledge_store import save_note
        save_note(slug_for_key(repo_key), 'brief.md', text)
        state.update(brief_sha256=hashlib.sha256(text.encode()).hexdigest(), stale=False, stale_reason='')
        _atomic(directory(repo_key) / 'drift.json', json.dumps(state, indent=2, sort_keys=True) + '\n')
        return text


def refresh_if_needed(repo_key: str) -> str | None:
    return generate(repo_key) if is_stale(repo_key) else None


def read_brief(key: str, *, surface: str = '', caller: str = '', query: str = '') -> tuple[str, dict]:
    try:
        path = directory(key) / 'brief.md'
        if path.is_symlink():
            return '', {}
        text = path.read_text(encoding='utf-8')
        if surface:
            from core.knowledge_store import record_hit
            record_hit(slug_for_key(key), 'brief.md', query=query, surface=surface, caller=caller, content=text)
        # Hash the exact version read, never trust separately replaced drift.json.
        return text, dict(repo_key=key, path=f'{slug_for_key(key)}/brief.md',
                          brief_sha256=hashlib.sha256(text.encode()).hexdigest())
    except (OSError, ValueError):
        return '', {}


def for_cwd(cwd: str) -> tuple[str, dict]:
    for key, root in sorted(code_index.load_registry().items()):
        try:
            if Path(cwd).resolve() == code_index.resolve_root(root):
                return read_brief(key, surface='fleet', caller='worker_prompt')
        except (ValueError, OSError):
            continue
    return '', {}


def fallback(query: str) -> str:
    terms = set(re.findall(r'\w+', query.lower()))
    found = []
    for key in sorted(code_index.load_registry()):
        text, receipt = read_brief(key)
        score = len(terms & set(re.findall(r'\w+', (key + ' ' + text).lower())))
        if text:
            found.append((score, receipt['path'], text))
    selected = sorted(found, reverse=True)[:2]
    from core.knowledge_store import record_hit
    for _, path, text in selected:
        slug, filename = path.split('/')
        # A brief deleted since it was read earns no retrieval receipt.
        with suppress(OSError, ValueError):
            record_hit(slug, filename, query=query, surface='brain', caller='recall_code', content=text)
            pass
    return '\n\n'.join(f'{path}\n{text[:3000]}' for _, path, text in selected)
