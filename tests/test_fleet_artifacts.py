import hashlib
import pytest

from core.artifacts import ArtifactRegistry
from fleet.artifacts import FleetArtifacts, artifact_capture
from fleet.isolation import run_test_gate
from fleet.policy import build_policy, builtin_config
from fleet.store import FleetStore


@pytest.fixture
def proof(tmp_path):
    store = FleetStore(tmp_path / 'fleet.db')
    run = store.create_run(task='proof', activity='coding', cwd=str(tmp_path),
        origin_session_id=None, origin_agent='codex', dry_run=False,
        policy=build_policy('coding', 'proof', config=builtin_config(), worker_count=1).to_dict())
    leg = run['phases'][0]['legs'][0]
    attempt = store.begin_attempt(leg['leg_id'])
    registry = ArtifactRegistry(root=tmp_path / 'proof', db_path=tmp_path / 'artifacts.db', key_path=tmp_path / 'key')
    return FleetArtifacts(store, registry), run['run_id'], leg['leg_id'], attempt['attempt_id']


@pytest.mark.parametrize('kind', ['testlog', 'screenshot', 'patch'])
def test_roundtrip_caps_integrity(proof, kind):
    adapter, run, leg, attempt = proof
    item = adapter.write(run, leg, attempt, kind, b'proof')
    assert adapter.read(item['artifact_id']) == b'proof'
    assert item['sha256'] == hashlib.sha256(b'proof').hexdigest()
    assert item['bytes'] == 5
    assert adapter.list(run, leg_id=leg, attempt_id=attempt)[0]['url'].startswith('/artifacts/')
    with pytest.raises(ValueError, match='size cap'):
        adapter.write(run, leg, attempt, kind, b'x', max_bytes=0)
    link = adapter.registry.search(fleet_run_id=run)[0]
    link.path.write_bytes(b'other')
    with pytest.raises(ValueError, match='integrity'):
        adapter.read(item['artifact_id'])


def test_gate_full_log(proof, tmp_path):
    import sys
    adapter, run, leg, attempt = proof
    with artifact_capture(adapter, run, leg, attempt):
        result = run_test_gate(tmp_path, [sys.executable, '-c', 'print("x" * 6000)'])
    assert len(result['output_tail']) == 2000
    assert b'x' * 6000 in adapter.read(result['artifact']['artifact_id'])


def test_screenshot_and_no_display(proof):
    import base64
    from io import BytesIO
    from PIL import Image
    from fleet.artifacts import capture_screenshot
    adapter, run, leg, attempt = proof
    stream = BytesIO()
    Image.new('RGB', (2, 2)).save(stream, format='JPEG')
    item = capture_screenshot(adapter, run, leg, attempt,
        observe=lambda: {'data': base64.b64encode(stream.getvalue()).decode()})
    assert adapter.read(item['artifact_id']).startswith(b'\x89PNG')
    def absent():
        raise RuntimeError('no display')
    assert capture_screenshot(adapter, run, leg, attempt, observe=absent) is None


def test_large_log_and_report_links(proof, monkeypatch):
    adapter, run, leg, attempt = proof
    import core.artifacts
    monkeypatch.setattr(core.artifacts, 'get_default_artifact_registry', lambda: adapter.registry)
    data = b'x' * (600 * 1024)
    item = adapter.write(run, leg, attempt, 'testlog', data)
    assert adapter.read(item['artifact_id']) == data
    adapter.store.fail_run(run, 'fixture terminal state')
    adapter.store.save_report(run, {'score': {'score': 100, 'size_class': 'S'}, 'timeline': [], 'knowledge': {}, 'generator': 'test'})
    assert adapter.store.get_report(run)['artifacts'][0]['url'] == item['url']


def test_declared_patch(proof, tmp_path):
    import json
    from fleet.artifacts import persist_declared
    adapter, run, leg, attempt = proof
    (tmp_path / 'proof.patch').write_text('patch content')
    output = '<serena-evidence>' + json.dumps({'units': [{'artifacts': [{'path': 'proof.patch', 'kind': 'patch'}]}]}) + '</serena-evidence>'
    persist_declared(adapter, run, leg, attempt, output, tmp_path)
    assert adapter.list(run)[0]['kind'] == 'patch'


def test_operator_lists_attempt_proof(proof, monkeypatch):
    from flask import Flask
    from ui.operator_web import operator_bp
    import fleet.store
    import core.artifacts
    adapter, run, leg, attempt = proof
    monkeypatch.setattr(fleet.store, 'FleetStore', lambda: adapter.store)
    monkeypatch.setattr(core.artifacts, 'get_default_artifact_registry', lambda: adapter.registry)
    item = adapter.write(run, leg, attempt, 'testlog', b'full proof')
    app = Flask(__name__)
    app.register_blueprint(operator_bp)
    response = app.test_client().get('/api/operator/artifacts', query_string={'fleet_run_id': run, 'leg_id': leg, 'attempt_id': attempt})
    assert response.status_code == 200
    assert response.json['artifacts'][0]['url'] == item['url']


def test_timeout_keeps_partial_output(proof, tmp_path, monkeypatch):
    import subprocess
    adapter, run, leg, attempt = proof
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1, output=b'partial output', stderr=b'error')
    monkeypatch.setattr('fleet.isolation.subprocess.run', timeout)
    with artifact_capture(adapter, run, leg, attempt):
        result = run_test_gate(tmp_path, ['pytest'], timeout=1)
    assert result['exit_code'] == 124
    assert b'partial output' in adapter.read(result['artifact']['artifact_id'])


def test_run_delete_cascades_proof_pointers(proof):
    adapter, run, leg, attempt = proof
    adapter.write(run, leg, attempt, 'patch', b'patch')
    with adapter.store._connect() as db:
        db.execute('DELETE FROM fleet_runs WHERE run_id=?', (run,))
        assert db.execute('SELECT COUNT(*) FROM fleet_run_artifacts').fetchone()[0] == 0


def test_log_cap_is_a_failed_gate_so_integration_can_roll_back(proof, tmp_path, monkeypatch):
    import sys
    import fleet.artifacts
    adapter, run, leg, attempt = proof
    monkeypatch.setitem(fleet.artifacts.CAPS, 'testlog', 10)
    with artifact_capture(adapter, run, leg, attempt):
        result = run_test_gate(tmp_path, [sys.executable, '-c', 'print("oversize proof")'])
    assert result['ran'] is True
    assert result['exit_code'] == 0
    assert result['ok'] is False
    assert 'size cap' in result['artifact_error']
