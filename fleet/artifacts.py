"""Fleet proof pointers over the shared, authenticated artifact registry."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import base64
import hashlib
from io import BytesIO
import time
import uuid

CAPS = {'testlog': 32 * 1024 * 1024, 'screenshot': 16 * 1024 * 1024, 'patch': 32 * 1024 * 1024}
_capture = ContextVar('fleet_artifact_capture', default=None)


class FleetArtifacts:
    def __init__(self, store, registry=None):
        self.store = store
        self._registry = registry

    @property
    def registry(self):
        if self._registry is None:
            from core.artifacts import get_default_artifact_registry
            self._registry = get_default_artifact_registry()
        return self._registry

    def write(self, run_id, leg_id, attempt_id, kind, data, *, max_bytes=None):
        if kind not in CAPS:
            raise ValueError('unsupported Fleet artifact kind')
        data = data.encode() if isinstance(data, str) else bytes(data)
        cap = CAPS[kind] if max_bytes is None else min(CAPS[kind], max_bytes)
        if len(data) > cap or not data:
            raise ValueError(f'{kind} artifact exceeds size cap ({cap} bytes) or is empty')
        with self.store._connect() as db:
            row = db.execute('SELECT l.run_id FROM fleet_attempts a JOIN fleet_legs l USING(leg_id) '
                             'WHERE a.attempt_id=? AND l.leg_id=?', (attempt_id, leg_id)).fetchone()
            if row is None or row['run_id'] != run_id:
                raise ValueError('artifact attempt does not belong to run/leg')
        suffix = {'screenshot': 'png', 'testlog': 'txt', 'patch': 'patch'}[kind]
        name = f'{kind}-{uuid.uuid4().hex}.{suffix}'
        path = self.registry.write_job_artifact(job_id=run_id, name=name, content=data, max_bytes=cap)
        link = self.registry.register(job_id=run_id, name=name, path=path,
                                      fleet_run_id=run_id, max_bytes=cap)
        with self.store._connect() as db:
            db.execute('INSERT INTO fleet_run_artifacts(run_id,leg_id,attempt_id,kind,artifact_id,sha256,bytes,created_at) '
                       'VALUES(?,?,?,?,?,?,?,?)',
                       (run_id, leg_id, attempt_id, kind, link.artifact_id, link.sha256, link.size, time.time()))
        return {'artifact_id': link.artifact_id, 'kind': kind, 'sha256': link.sha256,
                'bytes': link.size, 'url': link.url, 'leg_id': leg_id, 'attempt_id': attempt_id}

    def list(self, run_id, *, leg_id='', attempt_id=''):
        with self.store._connect() as db:
            rows = db.execute('SELECT * FROM fleet_run_artifacts WHERE run_id=? '
                'AND (?="" OR leg_id=?) AND (?="" OR attempt_id=?) ORDER BY id',
                (run_id, leg_id, leg_id, attempt_id, attempt_id)).fetchall()
        return [{**dict(row), 'url': link.url if link and link.expires_at >= time.time() else None}
                for row in rows for link in [self.registry.get_link(row['artifact_id'])]]

    def read(self, artifact_id):
        with self.store._connect() as db:
            row = db.execute('SELECT * FROM fleet_run_artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        link = self.registry.get_link(artifact_id)
        payload = self.registry.read(link.token) if link else None
        if payload is None or len(payload.data) != row['bytes'] or hashlib.sha256(payload.data).hexdigest() != row['sha256']:
            raise ValueError('artifact integrity verification failed or artifact expired')
        return payload.data


@contextmanager
def artifact_capture(adapter, run_id, leg_id, attempt_id):
    token = _capture.set((adapter, run_id, leg_id, attempt_id))
    try:
        yield
    finally:
        _capture.reset(token)


def spill_testlog(stdout, stderr):
    context = _capture.get()
    if context is None:
        return None
    adapter, run, leg, attempt = context
    return adapter.write(run, leg, attempt, 'testlog', 'STDOUT\n' + (stdout or '') + '\nSTDERR\n' + (stderr or ''))


def capture_screenshot(adapter, run, leg, attempt, *, observe=None, session_id=''):
    try:
        if observe is None:
            from core.computer_client import ComputerClient
            if not session_id:
                return None
            observe = lambda: ComputerClient().call('observe', session_id=session_id)
        frame = observe()
        frame = frame.get('frame', frame)
        from PIL import Image
        with Image.open(BytesIO(base64.b64decode(frame['data']))) as source:
            result = BytesIO()
            source.save(result, format='PNG')
    except (OSError, RuntimeError, ValueError, KeyError):
        return None
    return adapter.write(run, leg, attempt, 'screenshot', result.getvalue())


def persist_declared(adapter, run, leg, attempt, output, root):
    from fleet.completion import extract_envelope
    envelope, _, error = extract_envelope(output)
    if not envelope:
        return
    root = Path(root).resolve()
    for unit in envelope.get('units', []):
        for item in unit.get('artifacts', []):
            path = (root / item['path']).resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError('declared artifact escapes workspace')
            kind = item['kind']
            if kind not in CAPS or path.stat().st_size > CAPS[kind]:
                raise ValueError('declared artifact exceeds size cap or has invalid kind')
            adapter.write(run, leg, attempt, kind, path.read_bytes())
