"""Scheduled, flag-only knowledge audit. No semantic rewriting or NLI."""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

from core import config
from core import knowledge_store as store

ACTION = 'serena.knowledge.maintenance'


def run_pass(*, root: Path | None = None, now: datetime | None = None) -> dict:
    root = Path(root or config.KNOWLEDGE_DIR).resolve()
    now = now or datetime.now(timezone.utc)
    report = dict(stale=[], orphans=[], overlap=[], invalid_triggers=[], contradictions=[])
    index = (root / 'INDEX.md').read_text() if (root / 'INDEX.md').exists() else ''
    linked = set(re.findall(r'\]\(\./([^/)]+)', index))
    fingerprints = {}
    shingles_by_file = {}
    for path in sorted(root.glob('*/*.md')):
        if path.parent.name.startswith('.'):
            continue
        try:
            store.note_path(path.parent.name, path.name, root)
            text = path.read_text()
        except (OSError, ValueError):
            continue
        rel = path.relative_to(root).as_posix()
        meta = store.metadata(text)
        trigger = meta.get('trigger')
        if not isinstance(trigger, str) or not trigger.strip() or '\n' in trigger:
            report['invalid_triggers'].append(rel)
        try:
            verified = date.fromisoformat(str(meta.get('last_verified', '1970-01-01')))
        except ValueError:
            verified = date(1970, 1, 1)
        tech = str(meta.get('category', '')).lower() in {'tech', 'technical', 'technology'} or bool(
            re.search(r'\b(api|python|javascript|software|code|repo|sdk|react|linux|docker)\b',
                      path.parent.name.replace('-', ' ') + ' ' + str(trigger or ''), re.I))
        threshold = 60 if tech else 90
        age = (now.date() - verified).days
        if age > threshold:
            report['stale'].append(dict(file=rel, age_days=age, threshold_days=threshold))
        if path.parent.name not in linked:
            report['orphans'].append(rel)
        body = store._FRONT.sub('', text)
        fingerprint = store.digest(' '.join(body.lower().split()))
        if fingerprint in fingerprints:
            report['overlap'].append([fingerprints[fingerprint], rel])
        elif body.strip():
            fingerprints[fingerprint] = rel
        words = re.findall(r'\w+', body.lower())[:10_000]
        shingles = set(zip(words, words[1:], words[2:], strict=False))
        if len(shingles) >= 20:
            for previous, prior in shingles_by_file.items():
                # A conservative lexical signal, not a semantic contradiction
                # claim. Skip impossible size ratios before set intersection.
                if min(len(prior), len(shingles)) / max(len(prior), len(shingles)) < 0.8:
                    continue
                shared = len(prior & shingles)
                if shared / max(1, len(prior) + len(shingles) - shared) >= 0.8:
                    pair = [previous, rel]
                    if pair not in report['overlap']:
                        report['overlap'].append(pair)
            shingles_by_file[rel] = shingles
    report['contradictions'] = [
        {k: p[k] for k in ('id', 'slug', 'file', 'kind', 'state')}
        for p in store.proposals() if p['state'] == 'proposed' and p['kind'] == 'factual_correction']
    return report


def scheduled_pass(payload: dict):
    from core.serena_scheduler import ActionOutcome
    # Metadata migration is idempotent. Unknown verification remains epoch;
    # migration never claims research was verified or changes the note body.
    migration = store.backfill()
    report = run_pass()
    report['backfill'] = migration
    target = Path(config.DATA_DIR) / 'knowledge-maintenance.json'
    store.atomic_text(target, json.dumps(report, indent=2, sort_keys=True) + '\n')
    return ActionOutcome(not migration['errors'], 'knowledge audit complete',
                         output={'report_path': str(target), 'counts': {
                             key: len(value) for key, value in report.items() if isinstance(value, list)}})


def ensure_schedule(scheduler) -> None:
    """Install the reviewed weekly audit once; respect later pause/removal."""
    if any(row['action'] == ACTION for row in scheduler.list()):
        return
    scheduler.add_schedule(action=ACTION, interval_seconds=7 * 86400,
                           actor='knowledge-spec-bootstrap', requires_approval=False,
                           first_run_at=time.time())
