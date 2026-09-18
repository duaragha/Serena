import asyncio
import hashlib

import pytest

from core import code_index


@pytest.fixture
def repo(tmp_path, monkeypatch):
    from core import config
    monkeypatch.setattr(config, 'KNOWLEDGE_DIR', tmp_path / 'knowledge')
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setattr(code_index, 'DB_PATH', tmp_path / 'code.db')
    monkeypatch.setattr(code_index, 'REGISTRY_PATH', tmp_path / 'repos.json')
    root = tmp_path / 'repo'
    root.mkdir()
    (root / '.git').mkdir()
    (root / 'main.py').write_text('import sqlite3\ndef main(): pass\n')
    (root / 'README.md').write_text('# Example\nRun python main.py\n')
    (root / '.env').write_text('password=NEVER_DELIVER')
    code_index.add_repo(root, 'example')
    return root


def test_index_creates_guarded_brief_and_hash(repo):
    from core import repo_brief
    code_index.update_code_index()
    text, receipt = repo_brief.read_brief('example')
    for section in ('Layout', 'Entry points', 'Key modules', 'Data stores', 'Build commands', 'Test commands'):
        assert '## ' + section in text
    assert 'NEVER_DELIVER' not in text
    assert receipt['brief_sha256'] == hashlib.sha256(text.encode()).hexdigest()
    assert not repo_brief.is_stale('example')


def test_drift_is_durable_before_refresh_and_head_move(repo, monkeypatch):
    from core import repo_brief
    code_index.update_code_index()
    original = repo_brief.read_brief('example')[1]['brief_sha256']
    real_refresh = repo_brief.refresh_if_needed
    monkeypatch.setattr(repo_brief, 'refresh_if_needed', lambda key: None)
    (repo / 'main.py').write_text('def changed_entry(): pass\n')
    code_index.update_code_index()
    assert repo_brief.is_stale('example')
    real_refresh('example')
    assert repo_brief.read_brief('example')[1]['brief_sha256'] != original
    monkeypatch.setattr(repo_brief, 'head_sha', lambda root: 'new-head')
    code_index.update_code_index()
    assert repo_brief.is_stale('example')


def test_atomic_failure_keeps_complete_previous_brief(repo, monkeypatch):
    from core import repo_brief
    code_index.update_code_index()
    before = repo_brief.read_brief('example')[0]
    real_replace = repo_brief.os.replace
    def fail(source, target):
        if str(target).endswith('brief.md'):
            assert repo_brief.read_brief('example')[0] == before
            raise OSError('injected replace failure')
        return real_replace(source, target)
    monkeypatch.setattr(repo_brief.os, 'replace', fail)
    with pytest.raises(OSError):
        repo_brief.generate('example')
    assert repo_brief.read_brief('example')[0] == before


def test_fallback_and_exact_registry_identity(repo):
    from core import brain_tools, repo_brief
    code_index.update_code_index()
    assert repo_brief.for_cwd(str(repo))[0]
    assert not repo_brief.for_cwd(str(repo.parent))[0]
    result = asyncio.run(brain_tools.recall_code.handler({'query': 'example architecture zzz'}))
    assert 'brief.md' in result['content'][0]['text']
    assert repo_brief.slug_for_key('../../example') != repo_brief.slug_for_key('example')


def test_small_drift_accumulates_and_cli_show_is_read_only(repo):
    from click.testing import CliRunner

    from cli import main
    from core import repo_brief
    for index in range(10):
        (repo / f'module{index}.py').write_text(f'x = {index}\n')
    code_index.update_code_index()
    digest = repo_brief.read_brief('example')[1]['brief_sha256']
    (repo / 'module0.py').write_text('x = 99\n')
    stats = code_index.update_code_index()
    assert not stats['briefs']['example']['stale']
    assert repo_brief.read_brief('example')[1]['brief_sha256'] == digest
    result = CliRunner().invoke(main, ['code', 'brief', 'example'])
    assert result.exit_code == 0 and '## Layout' in result.output
    for index in range(1, 5):
        (repo / f'module{index}.py').write_text('x = 100\n')
    stats = code_index.update_code_index()
    assert stats['briefs']['example']['changed_file_ratio'] > repo_brief.DRIFT_RATIO
    assert not repo_brief.is_stale('example')


def test_fixture_build_test_commands_and_unknown_repository(repo):
    from core import repo_brief
    (repo / 'package.json').write_text('{"scripts":{"build":"tsc", "test":"vitest"}}')
    code_index.update_code_index()
    text, _ = repo_brief.read_brief('example')
    assert 'npm run build' in text and 'npm run test' in text
    with pytest.raises(ValueError, match='Unknown repository'):
        repo_brief.generate('missing')


def test_commands_survive_a_manifest_larger_than_one_index_chunk(repo):
    """A real manifest spans several 50-line chunks; chunk zero is not JSON."""
    import json

    from core import repo_brief
    manifest = {'name': 'example', 'version': '1.0.0',
                'dependencies': {f'dep-{index}': '1.0.0' for index in range(60)},
                'scripts': {'build': 'tsc', 'test': 'vitest'}}
    (repo / 'package.json').write_text(json.dumps(manifest, indent=2))
    (repo / 'Makefile').write_text(''.join(f'task{index}:\n\t@true\n' for index in range(40))
                                   + 'build:\n\t@true\ncheck:\n\t@true\n')
    code_index.update_code_index()
    text, _ = repo_brief.read_brief('example')
    assert len((repo / 'package.json').read_text().splitlines()) > 50
    assert 'npm run build' in text and 'npm run test' in text
    assert 'make build' in text and 'make check' in text
