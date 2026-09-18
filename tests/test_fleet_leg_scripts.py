from dataclasses import replace
import hashlib
import subprocess
import pytest
from fleet.policy import build_policy, builtin_config
from fleet.store import FleetStore
from fleet.workers import WorkerRequest, WorkerResult
from fleet.leg_scripts import freeze, load_bundle, replay_leg


def _open_leg(tmp_path, *, argv=('/bin/echo', 'fixture'), **overrides):
    """Freeze one attempt and leave it running, the way a live leg actually is."""
    root = tmp_path / 'repo'
    if not root.exists():
        root.mkdir()
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c', 'user.email=t@example.org', 'commit', '--allow-empty', '-qm', 'base'], check=True)
    store = FleetStore(tmp_path / 'fleet.db')
    policy = build_policy('coding', 'test', config=builtin_config(), worker_count=1).to_dict()
    run = store.create_run(task='test', activity='coding', cwd=str(root), origin_session_id=None,
                          origin_agent='codex', dry_run=False, policy=policy)
    leg = run['phases'][0]['legs'][0]
    attempt = store.begin_attempt(leg['leg_id'])
    request = WorkerRequest(run['run_id'], leg['leg_id'], attempt['attempt_id'], 'test', 'coding',
                            'discover', leg['role'], 'codex', leg['model'], leg['effort'], 'read', str(root), 'hello token=hidden')
    request = replace(request, **overrides) if overrides else request
    freeze(store, request, run, leg, argv=list(argv))
    return store, request, run, leg


@pytest.fixture
def frozen(tmp_path):
    store, request, run, leg = _open_leg(tmp_path)
    store.finish_attempt(request.attempt_id, state='completed', output_text='original')
    return store, request, run, leg


def test_bundle_roundtrip(frozen):
    store, request, run, leg = frozen
    bundle = load_bundle(store, request.attempt_id)
    assert bundle['prompt_sha256'] == hashlib.sha256(bundle['request']['prompt'].encode()).hexdigest()
    assert 'hidden' not in str(bundle)
    assert bundle['workspace']['commit']
    assert bundle['argv'] == ['/bin/echo', 'fixture']
    assert bundle['test_allowlist_version']
    assert 'dependency_states' in bundle
    assert 'cli_version' in bundle


def test_replay_isolated_append_only_and_diff(frozen):
    store, request, run, leg = frozen
    before = store.get_run(run['run_id'])
    def runner(req, **kwargs):
        from pathlib import Path
        assert req.cwd != request.cwd
        (Path(req.cwd) / 'replay-only').write_text('x')
        return WorkerResult(True, 'different', None, req.model, req.effort, 0)
    from fleet.completion import CompletionVerdict
    result = replay_leg(store, run['run_id'], leg['leg_id'], 1, runner=runner,
                       gate=lambda *a, **kw: CompletionVerdict(True, False, 'fixture', False))
    assert result['number'] == 2
    assert 'different' in result['diff']
    assert result['accepted'] is True
    from pathlib import Path
    assert not (Path(request.cwd) / 'replay-only').exists()
    after = store.get_run(run['run_id'])
    assert before['state'] == after['state']
    assert before['phases'][0]['legs'][0]['state'] == after['phases'][0]['legs'][0]['state']
    with store._connect() as db:
        original = db.execute('SELECT output_text,state FROM fleet_attempts WHERE attempt_id=?', (request.attempt_id,)).fetchone()
    assert tuple(original) == ('original', 'completed')


def test_missing_base_refuses(frozen):
    store, request, run, leg = frozen
    from unittest.mock import patch
    with patch('fleet.leg_scripts.load_bundle', return_value={**load_bundle(store, request.attempt_id), 'workspace': {'commit': '0' * 40}}):
        with pytest.raises(ValueError, match='pinned base'):
            replay_leg(store, run['run_id'], leg['leg_id'], 1)


def test_identical_real_gate_verdict(frozen):
    from fleet.completion_gate import evaluate_replay_completion
    store, request, run, leg = frozen
    bundle = load_bundle(store, request.attempt_id)
    original = evaluate_replay_completion(bundle['snapshot'], bundle['leg'], 'original',
        workspace=request.cwd, base=bundle['workspace']['commit'])
    def runner(req, **kwargs):
        assert list(req.frozen_argv) == bundle['argv']
        return WorkerResult(True, 'original', None, req.model, req.effort, 0)
    result = replay_leg(store, run['run_id'], leg['leg_id'], 1, runner=runner)
    assert result['verdict'] == original.to_dict()
    assert result['diff'] == ''


def test_bundle_tamper_refused(frozen):
    from pathlib import Path
    store, request, run, leg = frozen
    with store._connect() as db:
        path = db.execute('SELECT script_path FROM fleet_attempts WHERE attempt_id=?', (request.attempt_id,)).fetchone()[0]
    Path(path).write_text('{}')
    with pytest.raises(ValueError, match='integrity'):
        load_bundle(store, request.attempt_id)


def test_real_process_replay_accepts_identical_evidence(frozen, tmp_path, monkeypatch):
    import json
    import sys
    from fleet.completion_gate import evaluate_replay_completion
    store, old_request, run, _ = frozen
    monkeypatch.setenv('SERENA_FLEET_STATE_DIR', str(tmp_path / 'state'))
    snapshot = store.get_run(run['run_id'])
    leg = dict(snapshot['phases'][1]['legs'][0], access_mode='read_only')
    attempt = store.begin_attempt(leg['leg_id'])
    request = replace(old_request, leg_id=leg['leg_id'], attempt_id=attempt['attempt_id'],
                      phase='execute', access_mode='read_only', prompt='fixture',
                      model=leg['model'], effort=leg['effort'])
    criteria = run['policy']['work_units'][0]['completion_contract']['acceptance_criteria']
    output = 'The fixture was checked against the persisted contract and its replay produced the same observed result.\n<serena-evidence>' + json.dumps({'schema_version': 1, 'units': [{
        'id': 'ws-1', 'status': 'completed', 'acceptance': [{'criterion': c, 'met': True, 'evidence': 'fixture checked'} for c in criteria],
        'delivery': [], 'constraints_respected': True, 'changed_paths': [], 'tests': [], 'stop_condition': ''}]}) + '</serena-evidence>'
    events = [{'type': 'thread.settings', 'model': request.model, 'effort': request.effort},
              {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': output}}]
    program = 'import sys; sys.stdin.read(); print(' + repr('\n'.join(json.dumps(e) for e in events)) + ')'
    command = [sys.executable, '-c', program]
    freeze(store, request, snapshot, leg, argv=command)
    store.finish_attempt(attempt['attempt_id'], state='completed', output_text=output)
    bundle = load_bundle(store, attempt['attempt_id'])
    original = evaluate_replay_completion(snapshot, leg, output, workspace=request.cwd, base=bundle['workspace']['commit'])
    assert original.completion_allowed, original.summary()
    replay = replay_leg(store, run['run_id'], leg['leg_id'], 1)
    assert replay['accepted']
    assert replay['verdict'] == original.to_dict()
    assert replay['diff'] == ''
    assert replay['argv'] == command


def _accepting_gate():
    from fleet.completion import CompletionVerdict
    return lambda *a, **kw: CompletionVerdict(True, False, 'fixture', False)


def test_replay_does_not_fence_out_a_live_attempt(tmp_path):
    store, request, run, leg = _open_leg(tmp_path)
    def runner(req, **kwargs):
        return WorkerResult(True, 'replay output', None, req.model, req.effort, 0)
    replay = replay_leg(store, run['run_id'], leg['leg_id'], 1, runner=runner, gate=_accepting_gate())
    assert replay['replay_of'] == request.attempt_id
    # The live worker finishes after the replay row already exists.
    store.finish_attempt(request.attempt_id, state='completed', output_text='live result')
    with store._connect() as db:
        live = db.execute('SELECT state,output_text FROM fleet_attempts WHERE attempt_id=?',
                          (request.attempt_id,)).fetchone()
        ignored = db.execute("SELECT COUNT(*) FROM fleet_events WHERE type='attempt.late_result_ignored' "
                             'AND attempt_id=?', (request.attempt_id,)).fetchone()[0]
    assert tuple(live) == ('completed', 'live result')
    assert ignored == 0
    after = store.get_run(run['run_id'])
    assert after['phases'][0]['legs'][0]['state'] == 'completed'


def test_replay_excluded_from_evidence_and_error_history(tmp_path):
    store, request, run, leg = _open_leg(tmp_path)
    def runner(req, **kwargs):
        return WorkerResult(True, 'replay output', None, req.model, req.effort, 0)
    replay = replay_leg(store, run['run_id'], leg['leg_id'], 1, runner=runner, gate=_accepting_gate())
    with store._connect() as db:
        db.execute("UPDATE fleet_attempts SET error='replay noise', event_log_path=? WHERE attempt_id=?",
                   (str(tmp_path / 'replay.log'), replay['attempt_id']))
    store.finish_attempt(request.attempt_id, state='failed', error='live failure')
    assert store.previous_attempt_error(leg['leg_id'], before_attempt_number=99) == 'live failure'
    assert str(tmp_path / 'replay.log') not in store.attempt_event_logs(run['run_id'], leg['leg_id'])


def test_replay_mints_a_fresh_provider_session(tmp_path):
    store, request, run, leg = _open_leg(
        tmp_path, argv=['/bin/claude', '--session-id', 'live-session'],
        provider='claude', assigned_session_id='live-session')
    bundle = load_bundle(store, request.attempt_id)
    assert bundle['request']['assigned_session_id'] == 'live-session'
    seen = {}
    def runner(req, **kwargs):
        seen['argv'] = list(req.frozen_argv)
        seen['assigned'] = req.assigned_session_id
        seen['resume'] = req.resume_session_id
        return WorkerResult(True, 'replay output', None, req.model, req.effort, 0)
    replay = replay_leg(store, run['run_id'], leg['leg_id'], 1, runner=runner, gate=_accepting_gate())
    assert 'live-session' not in seen['argv']
    assert seen['assigned'] and seen['assigned'] != 'live-session'
    assert seen['argv'] == ['/bin/claude', '--session-id', seen['assigned']]
    assert not seen['resume']
    assert replay['session']['original'] == 'live-session'
    assert replay['session']['replay'] == seen['assigned']
    assert replay['argv'] == seen['argv']


def test_replay_refuses_a_resumed_provider_session(tmp_path):
    store, request, run, leg = _open_leg(
        tmp_path, argv=['/bin/claude', '--resume', 'live-session'],
        provider='claude', resume_session_id='live-session')
    with pytest.raises(ValueError, match='resumed a live provider session'):
        replay_leg(store, run['run_id'], leg['leg_id'], 1, runner=lambda *a, **kw: None)


def _replay_context(frozen, command):
    """A write-access replay leg that declares exactly one check."""
    import json
    store, request, run, leg = frozen
    bundle = load_bundle(store, request.attempt_id)
    snapshot = dict(bundle['snapshot'])
    write_leg = dict(bundle['leg'], access_mode='write')
    criteria = run['policy']['work_units'][0]['completion_contract']['acceptance_criteria']
    output = 'The declared check was rerun against the pinned replay workspace.\n<serena-evidence>' + json.dumps(
        {'schema_version': 1, 'units': [{'id': 'ws-1', 'status': 'completed',
            'acceptance': [{'criterion': c, 'met': True, 'evidence': 'fixture'} for c in criteria],
            'delivery': [], 'constraints_respected': True, 'changed_paths': [],
            'tests': [{'command': command, 'exit_code': 0}], 'stop_condition': ''}]}) + '</serena-evidence>'
    return store, request, snapshot, write_leg, bundle['workspace']['commit'], output


def _receipt_log(path, command, exit_code):
    import json
    path.write_text(json.dumps({'line': json.dumps({'type': 'item.completed', 'item': {
        'type': 'command_execution', 'command': command, 'exit_code': exit_code}})}) + '\n')
    return str(path)


def test_replay_prefers_the_recorded_receipt(frozen, tmp_path, monkeypatch):
    from fleet.completion_gate import evaluate_replay_completion
    import fleet.isolation as isolation
    command = 'python -m pytest tests/test_fixture.py -q'
    store, request, snapshot, leg, base, output = _replay_context(frozen, command)
    calls = []
    monkeypatch.setattr(isolation, 'run_test_gate', lambda *a, **kw: calls.append(a) or {})
    seen = {}
    import fleet.completion_gate as gate
    real = gate.evaluate_completion
    monkeypatch.setattr(gate, 'evaluate_completion',
                        lambda **kw: seen.update(kw) or real(**kw))
    evaluate_replay_completion(snapshot, leg, output, workspace=request.cwd, base=base,
                              event_log_path=_receipt_log(tmp_path / 'events.jsonl', command, 0))
    assert calls == []
    assert seen['observed_test_results'] == {command: 0}


def test_replay_keeps_declared_test_environment(frozen, monkeypatch):
    import sys
    from fleet.completion_gate import evaluate_replay_completion
    import fleet.isolation as isolation
    command = f'env -u TMPDIR PYTHONDONTWRITEBYTECODE=1 {sys.executable} -m pytest -q'
    store, request, snapshot, leg, base, output = _replay_context(frozen, command)
    monkeypatch.setenv('TMPDIR', '/tmp/fixture')
    seen = {}
    def gate(root, argv, *, timeout=900, env=None):
        seen['env'] = dict(env or {})
        seen['argv'] = list(argv)
        return {'ran': True, 'ok': True, 'exit_code': 0, 'command': list(argv), 'output_tail': ''}
    monkeypatch.setattr(isolation, 'run_test_gate', gate)
    evaluate_replay_completion(snapshot, leg, output, workspace=request.cwd, base=base)
    assert seen['env']['PYTHONDONTWRITEBYTECODE'] == '1'
    assert 'TMPDIR' not in seen['env']
    assert seen['argv'][1:] == ['-m', 'pytest', '-q']


def test_replay_rejects_failed_proof_capture(frozen, monkeypatch):
    import sys
    from fleet.completion_gate import evaluate_replay_completion
    import fleet.isolation as isolation
    command = f'{sys.executable} -m pytest tests/test_fixture.py -q'
    store, request, snapshot, leg, base, output = _replay_context(frozen, command)
    monkeypatch.setattr(isolation, 'run_test_gate', lambda *a, **kw: {
        'ran': True, 'ok': False, 'exit_code': 0, 'command': ['python'],
        'artifact_error': 'testlog persistence refused: cap'})
    verdict = evaluate_replay_completion(snapshot, leg, output, workspace=request.cwd, base=base)
    assert not verdict.completion_allowed
    assert any('proof capture failed' in failure and 'cap' in failure for failure in verdict.failures)


def test_replay_rejects_oversize_test_log(frozen, tmp_path, monkeypatch):
    import sys
    from core.artifacts import ArtifactRegistry
    from fleet.artifacts import FleetArtifacts, artifact_capture
    import fleet.artifacts as artifacts
    from fleet.completion_gate import evaluate_replay_completion
    command = f'{sys.executable} -m pytest --version'
    store, request, snapshot, leg, base, output = _replay_context(frozen, command)
    monkeypatch.setattr(artifacts, 'CAPS', {**artifacts.CAPS, 'testlog': 8})
    registry = ArtifactRegistry(root=tmp_path / 'proof', db_path=tmp_path / 'artifacts.db',
                                key_path=tmp_path / 'key')
    adapter = FleetArtifacts(store, registry)
    with artifact_capture(adapter, request.run_id, request.leg_id, request.attempt_id):
        verdict = evaluate_replay_completion(snapshot, leg, output, workspace=request.cwd, base=base)
    assert not verdict.completion_allowed
    assert any('proof capture failed' in failure for failure in verdict.failures)
