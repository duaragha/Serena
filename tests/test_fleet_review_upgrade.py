import subprocess

import pytest

from fleet.completion import severity_histogram
from fleet.policy import build_policy, builtin_config, policy_from_snapshot
from fleet.security_pass import security_pass
from fleet.supervisor import _findings_block


def test_order_histogram_policy():
    findings = [{'severity': s, 'unit_id': 'ws-1', 'summary': s, 'evidence': 'file:1'}
                for s in ['minor', 'blocker', 'major', 'major']]
    assert severity_histogram(findings) == {'blocker': 1, 'major': 2, 'minor': 1}
    text = _findings_block(findings)
    assert text.index('blocker') < text.index('major') < text.index('minor')
    config = builtin_config()
    assert build_policy('coding', 'test', config=config).blocker_gates_run is False
    config['defaults']['blocker_gates_run'] = True
    policy = build_policy('coding', 'test', config=config)
    assert policy_from_snapshot(policy.to_dict()).blocker_gates_run is True


def test_security_secret_and_clean(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'safe.txt').write_text('normal data', encoding="utf-8")
    assert security_pass(tmp_path, ['ws-1'])['findings'] == []
    (tmp_path / 'unsafe.txt').write_text('key=AKIA' + 'A' * 16, encoding="utf-8")
    result = security_pass(tmp_path, ['ws-1'])
    assert result['findings'][0]['category'] == 'security'
    assert result['findings'][0]['severity'] == 'blocker'
    assert 'AKIA' + 'A' * 16 not in str(result)


def test_loop_converges_and_exhausts():
    from fleet.review import review_decision
    blocker = [{'unit_id': 'ws-1', 'severity': 'blocker'}]
    assert review_decision(blocker, 0, 2, False) == ('retry', {'ws-1'})
    assert review_decision([], 1, 2, True) == ('clean', set())
    assert review_decision(blocker, 2, 2, False) == ('unresolved', {'ws-1'})
    assert review_decision(blocker, 2, 2, True) == ('failed', {'ws-1'})


@pytest.mark.parametrize('clean,gated', [(True, True), (False, False), (False, True)])
def test_durable_review_rounds(tmp_path, clean, gated):
    import json

    from fleet.review import advance_review
    from fleet.store import FleetStore
    config = builtin_config()
    config['defaults']['blocker_gates_run'] = gated
    policy = build_policy('coding', 'fix', config=config, worker_count=1)
    store = FleetStore(tmp_path / 'fleet.db')
    run = store.create_run(task='fix', activity='coding', cwd=str(tmp_path), origin_session_id=None,
                          origin_agent='codex', dry_run=False, policy=policy.to_dict())
    finding = {'unit_id': 'ws-1', 'severity': 'blocker', 'summary': 'bug', 'evidence': 'x:1'}
    def finish_cycle(clean=False):
        for phase in store.get_run(run['run_id'])['phases']:
            for leg in phase['legs']:
                if leg['state'] == 'completed':
                    continue
                attempt = store.begin_attempt(leg['leg_id'])
                output = '<serena-evidence>' + json.dumps({'units': [{'id': 'ws-1', 'findings': [] if clean else [finding]}]}) + '</serena-evidence>'
                store.finish_attempt(attempt['attempt_id'], state='completed', output_text=output)
    finish_cycle()
    assert advance_review(store, run['run_id'], policy) == 'retry'
    finish_cycle(clean=clean)
    if clean:
        assert advance_review(store, run['run_id'], policy) == 'clean'
        assert len([e for e in store.events(run['run_id']) if e['type'] == 'run.review.round']) == 1
    else:
        assert advance_review(store, run['run_id'], policy) == 'retry'
        finish_cycle()
        assert advance_review(store, run['run_id'], policy) == ('failed' if gated else 'unresolved')
        assert store.has_event(run['run_id'], 'run.review.unresolved')
        assert store.review_report(run['run_id'])['severity_histogram'] == {'blocker': 1, 'major': 0, 'minor': 0}


def test_security_merge():
    import json

    from fleet.completion import extract_envelope
    from fleet.security_pass import merge_security_findings
    output = '<serena-evidence>' + json.dumps({'units': [{'id': 'ws-1', 'findings': []}]}) + '</serena-evidence>'
    finding = {'unit_id': 'ws-1', 'severity': 'blocker', 'category': 'security'}
    payload, _, _ = extract_envelope(merge_security_findings(output, {'findings': [finding], 'checks': []}))
    assert payload['units'][0]['findings'] == [finding]


def _git_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    return root


def _write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


# Split so this file's own fixtures are not themselves scanner matches.
REALISTIC_TOKEN = 'AKIA' + '3KZ7Q1PLMWXV9RTD'
KEY_BODY_TEXT = 'MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC7VJTUt9Us8cKj'


def test_security_excludes_dependency_and_generated_trees(tmp_path):
    root = _git_repo(tmp_path / 'repo')
    # A vendored copy nested below the root, the shape the old pathspec missed.
    _write(root, 'apps/desktop/node_modules/dotenv/README.md',
           f'Example:\n```\nKEY="-----BEGIN PRIVATE KEY-----\\n{KEY_BODY_TEXT}\\n-----END"\n```\n')
    _write(root, 'apps/desktop/node_modules/postject/dist/api.js', f'const k = "{REALISTIC_TOKEN}";\n')
    _write(root, 'build/sidecar/_internal/jose/import.js',
           f'const k = "{REALISTIC_TOKEN}";\n')
    result = security_pass(root, ['ws-1'])
    assert REALISTIC_TOKEN not in str(result)
    assert KEY_BODY_TEXT not in str(result)
    for finding in result['findings']:
        assert finding['severity'] == 'minor', finding['file']
        assert 'not this run source' in finding['classification']
    assert all(finding['category'] == 'security' for finding in result['findings'])


def test_security_gates_unexplained_material_on_every_path_alike(tmp_path):
    """A test or documentation path is not on its own a reason to downgrade."""
    root = _git_repo(tmp_path / 'repo')
    for relative in ('core/settings.py', 'tests/settings.py', 'tests/test_settings.py',
                     'core/conftest.py', 'docs/config.md'):
        _write(root, relative, f'AWS_ACCESS_KEY_ID = "{REALISTIC_TOKEN}"\n')
    result = security_pass(root, ['ws-1'])
    assert REALISTIC_TOKEN not in str(result)
    by_file = {finding['file']: finding for finding in result['findings']}
    assert set(by_file) == {'core/settings.py', 'tests/settings.py', 'tests/test_settings.py',
                            'core/conftest.py', 'docs/config.md'}
    for path, finding in by_file.items():
        assert finding['severity'] == 'blocker', path
        assert finding['classification'] == 'first-party source', path


def test_security_treats_escaped_and_multiline_key_material_alike(tmp_path):
    """Escaped-newline key bytes are the same exposure as physical lines."""
    root = _git_repo(tmp_path / 'repo')
    _write(root, 'core/multiline.pem',
           f'-----BEGIN PRIVATE KEY-----\n{KEY_BODY_TEXT}\n-----END PRIVATE KEY-----\n')
    _write(root, 'core/escaped.py',
           f'KEY = "-----BEGIN PRIVATE KEY-----\\n{KEY_BODY_TEXT}\\n-----END PRIVATE KEY-----"\n')
    _write(root, 'core/crlf.js',
           f'const k = "-----BEGIN RSA PRIVATE KEY-----\\r\\n{KEY_BODY_TEXT}\\r\\n-----END";\n')
    _write(root, 'core/joined.py',
           f'KEY = "-----BEGIN PRIVATE KEY-----{KEY_BODY_TEXT}"\n')
    result = security_pass(root, ['ws-1'])
    assert KEY_BODY_TEXT not in str(result)
    by_file = {finding['file']: finding for finding in result['findings']}
    for path in ('core/multiline.pem', 'core/escaped.py', 'core/crlf.js', 'core/joined.py'):
        assert by_file[path]['severity'] == 'blocker', path
        assert by_file[path]['classification'] == 'first-party source', path


def test_security_separates_key_header_from_key_material(tmp_path):
    root = _git_repo(tmp_path / 'repo')
    _write(root, 'core/verify.py',
           "PEM = '-----BEGIN PRIVATE KEY-----'\n\n\ndef check(text):\n    return text.startswith(PEM)\n")
    _write(root, 'core/abbreviated.md',
           'Set `KEY="-----BEGIN PRIVATE KEY-----\\nabc\\n-----END"` in your env.\n')
    result = security_pass(root, ['ws-1'])
    by_file = {finding['file']: finding for finding in result['findings']}
    for path in ('core/verify.py', 'core/abbreviated.md'):
        assert by_file[path]['severity'] == 'minor', path
        assert by_file[path]['classification'] == 'private-key header without key material'


def test_security_suppresses_only_verified_synthetic_fixtures(tmp_path):
    root = _git_repo(tmp_path / 'repo')
    # Verified three ways: asserted absent, degenerate filler, adjacent marker.
    _write(root, 'tests/test_redaction.py',
           f'SOURCE = "{REALISTIC_TOKEN}"\n\n\ndef test_it(): assert "{REALISTIC_TOKEN}" not in render(SOURCE)\n')
    _write(root, 'tests/test_filler.py', 'TOKEN = "AKIA' + 'A' * 16 + '"\n')
    _write(root, 'tests/test_marked.py',
           f'# synthetic material, never a live credential\nKEY = "-----BEGIN PRIVATE KEY-----\\n{KEY_BODY_TEXT}"\n')
    _write(root, 'tests/test_aws_example.py', 'TOKEN = "AKIAIOSFODNN7EXAMPLE"\n')
    result = security_pass(root, ['ws-1'])
    assert REALISTIC_TOKEN not in str(result)
    by_file = {finding['file']: finding for finding in result['findings']}
    for path in ('tests/test_redaction.py', 'tests/test_filler.py',
                 'tests/test_marked.py', 'tests/test_aws_example.py'):
        assert by_file[path]['severity'] == 'minor', path
        assert by_file[path]['classification'] == 'verified synthetic fixture', path


def test_security_keeps_the_repository_own_fixtures_non_gating():
    """The four files this run's reviewer reported must not gate any run."""
    result = security_pass('.', ['ws-1'])
    reported = [finding for finding in result['findings']
                if finding['file'].startswith('tests/')]
    assert reported, 'expected the known synthetic fixtures to still be scanned'
    for finding in reported:
        assert finding['severity'] == 'minor', finding['file']
    assert not [f for f in result['findings'] if f['severity'] == 'blocker']
