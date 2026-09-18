import hashlib
import json
from datetime import datetime, timezone

import pytest


def run_tool(tool, args):
    import asyncio
    async def bounded():
        return await asyncio.wait_for(tool.handler(args), timeout=5)
    return asyncio.run(bounded())


@pytest.fixture
def kb(tmp_path, monkeypatch):
    from core import config
    from knowledge import reader
    root = tmp_path / 'knowledge'
    root.mkdir()
    monkeypatch.setattr(config, 'KNOWLEDGE_DIR', root)
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setattr(reader, 'KNOWLEDGE_DIR', root)
    monkeypatch.setattr(reader, 'INDEX_PATH', root / 'INDEX.md')
    return root


def test_save_index_unknown_lines_and_rollback(kb, monkeypatch):
    from core import knowledge_store as store
    original = '# Notes\nunknown line\n- [Other](./other/a.md) — keep exactly\n'
    (kb / 'INDEX.md').write_text(original)
    store.save_note('demo', 'notes.md', '# Demo\nhello')
    assert original in (kb / 'INDEX.md').read_text()
    assert './demo/' in (kb / 'INDEX.md').read_text()
    before = (kb / 'demo/notes.md').read_text()
    replace = store.os.replace
    def fail(source, target):
        if str(target).endswith('INDEX.md'):
            raise OSError('index replacement failed')
        return replace(source, target)
    monkeypatch.setattr(store.os, 'replace', fail)
    with pytest.raises(OSError):
        store.save_note('demo', 'notes.md', '# Replacement')
    assert (kb / 'demo/notes.md').read_text() == before


def test_backfill_preserves_body_and_is_idempotent(kb):
    from core import knowledge_store as store
    (kb / 'demo').mkdir()
    path = kb / 'demo/notes.md'
    path.write_text('---\ntitle: Example\ncustom: keep\n---\n# Body\nunchanged\n')
    assert store.backfill(kb)['updated'] == 1
    text = path.read_text()
    assert 'custom: keep' in text and '# Body\nunchanged\n' in text
    assert '1970-01-01' in text and 'trigger:' in text
    assert store.backfill(kb)['updated'] == 0


def test_trigger_ranks_and_reads_receipted_without_query(kb):
    from core import brain_tools
    from core import knowledge_store as store
    from knowledge import reader
    store.save_note('routing', 'README.md', '---\ntrigger: zebra orchestration\n---\n# Routing\nOther content')
    store.save_note('body', 'README.md', '# Body\nzebra orchestration')
    ranked = brain_tools._search_knowledge('zebra orchestration')
    assert 'routing' in ranked
    assert 'body' not in ranked or ranked.index('routing') < ranked.index('body')
    reader.get_file_content('routing', 'README.md', query='private-query', surface='cli', caller='test')
    rows = store.hits()
    assert any(r['query_sha256'] == hashlib.sha256(b'private-query').hexdigest() for r in rows)
    assert 'private-query' not in json.dumps(rows)


def test_feedback_proposal_digests_and_approval(kb):
    from core import knowledge_store as store
    store.save_note('demo', 'notes.md', '# Old')
    before = (kb / 'demo/notes.md').read_text()
    receipt = store.record_hit('demo', 'notes.md', query='private question', surface='brain', caller='test')
    proposal = store.propose_feedback('demo', 'notes.md', 'factual_correction', receipt_id=receipt,
                                      corrected_content='# New', reason='private speech')
    assert (kb / 'demo/notes.md').read_text() == before
    assert 'private speech' not in json.dumps(store.proposals())
    store.review_proposal(proposal['id'], 'approve')
    assert '# New' in (kb / 'demo/notes.md').read_text()
    with pytest.raises(ValueError):
        store.propose_feedback('other', 'notes.md', 'relevance', receipt_id=receipt)


def test_maintenance_surfaces_stale_orphan_overlap_triggers(kb):
    from core.knowledge_maintenance import run_pass
    for slug in ['one', 'two']:
        (kb / slug).mkdir()
        (kb / slug / 'note.md').write_text('# Shared\nidentical documentation\n')
    report = run_pass(root=kb, now=datetime(2026, 9, 17, tzinfo=timezone.utc))
    assert report['stale'] and report['orphans'] and report['overlap'] and report['invalid_triggers']


def test_receipt_retention(kb, monkeypatch):
    from core import knowledge_store as store
    monkeypatch.setattr(store, 'MAX_HITS', 2)
    store.save_note('demo', 'n.md', '# Demo')
    for _ in range(4):
        store.record_hit('demo', 'n.md', surface='cli', caller='test')
    assert len(store.hits()) == 2


@pytest.mark.parametrize('surface', ['brain', 'cli', 'daemon', 'computer', 'fts'])
def test_every_read_surface_leaves_receipt(kb, monkeypatch, surface):
    from core import brain_tools, computer_knowledge, indexer
    from core import knowledge_store as store
    store.save_note('demo', 'README.md', '# Zebra\nZebra cloud account setup instructions.')
    if surface == 'brain':
        assert 'retrieval receipt:' in brain_tools._read_knowledge('demo')
    elif surface == 'cli':
        from click.testing import CliRunner

        from cli import main
        result = CliRunner().invoke(main, ['knowledge', 'show', 'demo'])
        assert result.exit_code == 0, result.output
    elif surface == 'daemon':
        from core.chat_daemon import _knowledge_index
        assert 'Zebra' in _knowledge_index()
    elif surface == 'computer':
        monkeypatch.setattr(computer_knowledge, 'KNOWLEDGE_DIR', kb)
        assert 'Zebra' in computer_knowledge.build_task_pack('zebra cloud setup', roots=[kb])
    else:
        monkeypatch.setattr(indexer, 'DB_PATH', kb.parent / 'chats.db')
        indexer.update_knowledge_index()
        indexer.build_knowledge_fts()
        assert indexer.search_knowledge_fts('Zebra', surface='fts')
    records = store.hits()
    assert any(row['surface'] == surface for row in records)
    assert all(set(row) == {'id', 'ts', 'slug', 'file', 'query_sha256', 'surface', 'caller', 'content_sha256'} for row in records)


def test_knowledge_consent_is_affirmative_bound_and_never_interrogative():
    """Consent has to be a complete, present-tense instruction naming this proposal."""
    from core.brain_memory_tools import _affirmative_knowledge_review as consent
    identity = 'bbbbbbbb111122223333444455556666'
    assert consent('approve that knowledge proposal', 'approve', identity)
    assert consent(f'approve knowledge proposal {identity[:8]}', 'approve', identity)
    assert consent(f'apply the correction in {identity}', 'approve', identity)
    assert consent('reject that knowledge proposal', 'reject', identity)
    assert consent('approve the first proposal', 'approve', identity)
    for refused in ('do not approve the knowledge proposal, but approve the deployment',
                    'approve knowledge proposal aaaaaaaa',
                    f'approve knowledge proposal aaaaaaaa, not {identity[:8]}',
                    'should I approve this knowledge proposal?',
                    'should I approve this knowledge proposal',
                    'can you approve the knowledge proposal',
                    'do not approve that knowledge proposal',
                    "don't approve the knowledge correction",
                    'never approve that proposal',
                    'approve the deployment pipeline',
                    'reject that knowledge proposal',
                    # Deferred consent is a plan, not an instruction to act now.
                    'approve that knowledge proposal only after I confirm tomorrow',
                    'approve that knowledge proposal once the review lands',
                    'if the diff looks right approve that knowledge proposal',
                    # Reported speech is never the caller's own instruction.
                    'the document says "approve that knowledge proposal"',
                    "the note reads 'approve that knowledge proposal'",
                    # The verb must take the proposal as its object.
                    'approve the deployment and leave the knowledge proposal unchanged',
                    'approve the deployment while the knowledge proposal waits'):
        assert not consent(refused, 'approve', identity), refused
    assert not consent('approve that knowledge proposal', 'approve', '')
    assert not consent('approve that knowledge proposal', 'reject', identity)


def test_broker_requires_genuine_feedback_and_explicit_review(kb, monkeypatch):
    from core import brain_memory_tools as tools
    from core import knowledge_store as store
    # Same SDK scheduling seam as test_memory_feedback; exercise real storage
    # and broker logic without the sandbox's executor wakeup/shutdown stall.
    async def immediate(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(tools.asyncio, 'to_thread', immediate)
    store.save_note('demo', 'n.md', '# Old')
    receipt = store.record_hit('demo', 'n.md', surface='brain', caller='test')
    origin = {'text': 'hello', 'protocol': 'desk'}
    monkeypatch.setattr(tools, 'current_turn', lambda: origin)
    monkeypatch.setattr(tools, 'previous_user_turn_text', lambda: '')
    args = {'slug': 'demo', 'file': 'n.md', 'kind': 'factual_correction',
            'receipt_id': receipt, 'corrected_content': '# New'}
    assert 'NOT DONE' in run_tool(tools.record_knowledge_feedback, args)['content'][0]['text']
    origin['text'] = 'that fact is incorrect, correct it to the new value'
    result = run_tool(tools.record_knowledge_feedback, args)['content'][0]['text']
    assert 'PROPOSED' in result
    proposal = store.proposals()[0]
    review = {'proposal_id': proposal['id'], 'action': 'approve'}
    assert 'NOT DONE' in run_tool(tools.review_knowledge_proposal, review)['content'][0]['text']
    # Consent must be affirmative and bound to the proposal: a refusal that
    # merely contains the verb, and approval of something else, are not consent.
    for refusal in ('do not approve that knowledge proposal',
                    "don't approve the knowledge correction",
                    'never approve that proposal',
                    'approve the deployment pipeline',
                    'do not approve the knowledge proposal, but approve the deployment',
                    'approve knowledge proposal aaaaaaaa',
                    'should I approve this knowledge proposal?',
                    'approve that knowledge proposal only after I confirm tomorrow',
                    'the document says "approve that knowledge proposal"',
                    'approve the deployment and leave the knowledge proposal unchanged'):
        origin['text'] = refusal
        assert 'NOT DONE' in run_tool(tools.review_knowledge_proposal, review)['content'][0]['text']
        assert (kb / 'demo/n.md').read_text().strip().endswith('# Old')
    origin['text'] = 'approve that knowledge proposal'
    assert 'REVIEW RECORDED' in run_tool(tools.review_knowledge_proposal, review)['content'][0]['text']
    assert '# New' in (kb / 'demo/n.md').read_text()


def test_deleted_note_never_receipts_even_from_cached_index_content(kb, monkeypatch):
    from core import indexer
    from core import knowledge_store as store
    store.save_note('demo', 'n.md', '# Zebra procedure')
    monkeypatch.setattr(indexer, 'DB_PATH', kb.parent / 'chats.db')
    monkeypatch.setattr(indexer, '_schema_ready', False)
    indexer.update_knowledge_index()
    indexer.build_knowledge_fts()
    assert indexer.search_knowledge_fts('Zebra', surface='fts')
    before = len(store.hits())
    (kb / 'demo/n.md').unlink()
    # The FTS row still holds cached content; a retrieval receipt would claim a
    # read of a file that no longer exists.
    assert indexer.search_knowledge_fts('Zebra', surface='fts') == []
    assert len(store.hits()) == before
    with pytest.raises(ValueError, match='no longer exists'):
        store.record_hit('demo', 'n.md', surface='stale', caller='test', content='# Zebra procedure')


def test_scheduler_installs_once_and_runs_real_maintenance(kb):
    from core.knowledge_maintenance import ACTION, ensure_schedule
    from core.scheduler_actions import register_all
    from core.serena_scheduler import SerenaScheduler
    scheduler = register_all(SerenaScheduler(kb.parent / 'scheduler.db', notifier=None))
    ensure_schedule(scheduler)
    ensure_schedule(scheduler)
    rows = [row for row in scheduler.list() if row['action'] == ACTION]
    assert len(rows) == 1 and rows[0]['state'] == 'active'
    from core.knowledge_maintenance import scheduled_pass
    outcome = scheduled_pass({})
    assert outcome.ok
    assert json.loads(__import__('pathlib').Path(outcome.output['report_path']).read_text())['backfill']['errors'] == []


def test_interrupted_pair_recovers_before_next_read(kb):
    from core import knowledge_store as store
    from knowledge import reader
    store.save_note('demo', 'n.md', '# Before')
    new = store.with_metadata('# After\n', 'demo', 'n.md')
    index = '# Recovered INDEX\n- [demo](./demo/) - new\n'
    (kb / '.knowledge-write.json').write_text(json.dumps(
        {'slug': 'demo', 'file': 'n.md', 'note': new, 'index': index}))
    (kb / 'demo/n.md').write_text(new)
    assert '# After' in reader.get_file_content('demo', 'n.md')
    assert (kb / 'INDEX.md').read_text() == index
    assert not (kb / '.knowledge-write.json').exists()


def test_stale_thresholds_and_stale_correction_rejection(kb):
    from core import knowledge_store as store
    from core.knowledge_maintenance import run_pass
    for slug, category in [('tech', 'tech'), ('normal', 'personal')]:
        store.save_note(slug, 'n.md', f'---\ncategory: {category}\nlast_verified: 2026-07-01\n---\n# {slug}')
    report = run_pass(root=kb, now=datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert [row['file'] for row in report['stale']] == ['tech/n.md']
    hit = store.record_hit('tech', 'n.md', surface='test', caller='test')
    proposal = store.propose_feedback('tech', 'n.md', 'factual_correction', receipt_id=hit, corrected_content='# Candidate')
    store.save_note('tech', 'n.md', '# Concurrent update')
    with pytest.raises(ValueError, match='changed since retrieval'):
        store.review_proposal(proposal['id'], 'approve')
    assert '# Concurrent update' in (kb / 'tech/n.md').read_text()


def test_maintenance_finds_strong_lexical_overlap_not_just_byte_duplicates(kb):
    from core import knowledge_store as store
    from core.knowledge_maintenance import run_pass
    body = ' '.join(f'procedure{i} step{i} evidence{i}' for i in range(40))
    store.save_note('one', 'n.md', '# First title\n' + body)
    store.save_note('two', 'n.md', '# Second title\n' + body + '\nAn extra sentence.')
    assert run_pass(root=kb)['overlap'] == [['one/n.md', 'two/n.md']]
