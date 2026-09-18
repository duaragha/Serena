"""Immutable leg inputs and append-only, isolated debugging replays."""
import difflib
import hashlib
import json
import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from fleet.context import redact_text, redact_value
from fleet.workers import WorkerRequest, run_worker, worker_command

ALLOWLIST_VERSION = 'fleet-safe-test-argv-v1'


def _git(root, *args, check=True):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, timeout=30, check=check)


def freeze(store, request, snapshot, leg, *, argv=None):
    """Called during attempt startup, after prompt assembly and before dispatch."""
    with store._connect() as db:
        prior = db.execute('SELECT script_path FROM fleet_attempts WHERE attempt_id=?', (request.attempt_id,)).fetchone()
    if prior is None:
        raise ValueError('unknown attempt')
    if prior['script_path']:
        bundle = load_bundle(store, request.attempt_id)
        return replace(request, frozen_argv=tuple(bundle['argv']), prompt=bundle['request']['prompt'],
                       assigned_session_id=bundle['request'].get('assigned_session_id', ''))
    request = replace(request, prompt=redact_text(request.prompt)[0],
                      assigned_session_id=(request.assigned_session_id
                                           or request.resume_session_id or str(uuid.uuid4())))
    command = list(argv) if argv is not None else worker_command(request, session_id=request.assigned_session_id)
    head = _git(request.cwd, 'rev-parse', 'HEAD', check=False)
    branch = _git(request.cwd, 'symbolic-ref', '--short', '-q', 'HEAD', check=False)
    # A detached private clone below gets both committed and dirty input state.
    patch = _git(request.cwd, 'diff', '--binary', 'HEAD', check=False).stdout
    untracked = _git(request.cwd, 'ls-files', '--others', '--exclude-standard', '-z', check=False).stdout.split('\0')
    import base64
    extra = {}
    unsupported = []
    for name in filter(None, untracked):
        path = Path(request.cwd) / name
        if name.split('/')[0] in {'.venv', 'node_modules', '__pycache__'}:
            continue
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            unsupported.append(name)
            continue
        extra[name] = {'data': base64.b64encode(path.read_bytes()).decode(), 'mode': path.stat().st_mode & 0o777}
    try:
        version = subprocess.run([command[0], '--version'], capture_output=True, text=True, timeout=5).stdout[:2000]
    except (OSError, subprocess.TimeoutExpired):
        version = 'unavailable'
    safe_request = asdict(request)
    safe_request['peer_token'] = ''
    safe_request['frozen_argv'] = []
    from fleet.completion_gate import _dependency_states, _leg_phase
    phase_index, _ = _leg_phase(snapshot, leg)
    bundle = {'schema_version': 1, 'request': safe_request, 'argv': command,
        'prompt_sha256': hashlib.sha256(request.prompt.encode()).hexdigest(),
        'workspace': {'commit': head.stdout.strip() if head.returncode == 0 else '',
                      'branch': branch.stdout.strip(), 'patch': patch, 'untracked': extra,
                      'unsupported_inputs': unsupported},
        'dependency_states': _dependency_states(snapshot, phase_index), 'phase_index': phase_index,
        'test_allowlist_version': ALLOWLIST_VERSION, 'cli_version': version,
        'snapshot': snapshot, 'leg': leg}
    # Bundle metadata and prompt follow Fleet redaction; source bytes remain in
    # the pinned repository, with the private delta only for exact reconstruction.
    bundle['snapshot'] = redact_value(snapshot)[0]
    # Keep bundles alongside the run journal, outside Git's input tree. A
    # checkout-local bundle otherwise recursively captures prior prompts.
    directory = store.path.parent / 'runs' / request.run_id / 'leg-scripts'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f'{request.attempt_id}.json'
    prompt_path = directory / f'{request.attempt_id}.prompt.txt'
    bundle['prompt_ref'] = str(prompt_path)
    _atomic(prompt_path, request.prompt.encode(), exclusive=True)
    data = json.dumps(bundle, sort_keys=True).encode()
    _atomic(path, data, exclusive=True)
    digest = hashlib.sha256(data).hexdigest()
    with store._connect() as db:
        updated = db.execute('UPDATE fleet_attempts SET script_path=?,script_sha256=? WHERE attempt_id=? AND script_path IS NULL',
                             (str(path), digest, request.attempt_id)).rowcount
        if updated != 1:
            raise RuntimeError('attempt bundle already frozen')
    return replace(request, frozen_argv=tuple(command))


def _atomic(path, data, *, exclusive=False):
    descriptor, name = tempfile.mkstemp(prefix='.bundle-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(name, path)
        else:
            os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_bundle(store, attempt_id):
    with store._connect() as db:
        row = db.execute('SELECT script_path,script_sha256 FROM fleet_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
    if row is None or not row['script_path']:
        raise ValueError('attempt has no frozen leg script')
    path = Path(row['script_path'])
    if path.is_symlink():
        raise ValueError('leg script integrity failure')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != row['script_sha256']:
        raise ValueError('leg script integrity failure')
    bundle = json.loads(data)
    if (hashlib.sha256(bundle['request']['prompt'].encode()).hexdigest() != bundle['prompt_sha256']
            or hashlib.sha256(Path(bundle['prompt_ref']).read_bytes()).hexdigest() != bundle['prompt_sha256']):
        raise ValueError('prompt integrity failure')
    return bundle


def replay_leg(store, run_id, leg_id, attempt_number, *, runner=None, gate=None):
    from fleet.completion_gate import evaluate_replay_completion
    with store._connect() as db:
        original = db.execute('SELECT a.* FROM fleet_attempts a JOIN fleet_legs l USING(leg_id) '
            'WHERE l.run_id=? AND l.leg_id=? AND a.attempt_number=?', (run_id, leg_id, int(attempt_number))).fetchone()
    if original is None:
        raise ValueError('unknown run/leg/attempt')
    bundle = load_bundle(store, original['attempt_id'])
    source = Path(bundle['request']['cwd'])
    base = bundle['workspace']['commit']
    if not base or _git(source, 'cat-file', '-e', base + '^{commit}', check=False).returncode:
        raise ValueError('pinned base is unavailable; refusing approximate replay')
    if bundle['test_allowlist_version'] != ALLOWLIST_VERSION:
        raise ValueError('test allowlist version changed; replay refused')
    if bundle['workspace'].get('unsupported_inputs'):
        raise ValueError('frozen workspace has unsupported inputs; refusing approximate replay')
    replay_id = str(uuid.uuid4())
    root = store.path.parent / 'fleet-replays' / replay_id
    root.parent.mkdir(parents=True, exist_ok=True)
    # A private clone has independent Git metadata and cannot touch a live
    # integration journal or collide with the supervisor's worktree/claims.
    subprocess.run(['git', 'clone', '--no-hardlinks', '--no-checkout', '--quiet', str(source), str(root)], check=True, capture_output=True, timeout=60)
    _git(root, 'checkout', '--detach', base)
    patch = bundle['workspace'].get('patch', '')
    if patch:
        subprocess.run(['git', '-C', str(root), 'apply', '--binary', '-'], input=patch, text=True, capture_output=True, check=True, timeout=30)
    import base64
    for name, item in bundle['workspace'].get('untracked', {}).items():
        path = root / name
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('bundle file escapes replay workspace')
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic(path, base64.b64decode(item['data']))
        path.chmod(item['mode'])
    from fleet.integration_journal import IntegrationJournal
    from fleet.isolation import FleetIsolationStore
    isolation = FleetIsolationStore(root.parent / f'{replay_id}.isolation.sqlite3', workspace_root=root.parent)
    workspace = isolation.record_workspace(run_id=run_id, worker_key='replay:' + replay_id,
        path=str(root), branch=base, base_head=base)
    journal = IntegrationJournal(isolation, root, workspace, patch)
    paths = _git(root, 'ls-files', '-z').stdout.split('\0')
    paths += list(bundle['workspace'].get('untracked', {}))
    journal.prepare(root, sorted(set(filter(None, paths))))
    if journal.state() != 'pre':
        raise ValueError('replay input journal does not match the pinned workspace')
    values = dict(bundle['request'])
    if str(values.get('resume_session_id') or ''):
        # Resuming would append to the live conversation the original leg used.
        # No provider CLI here can fork one, so an approximate replay is refused.
        raise ValueError('frozen leg resumed a live provider session; refusing replay')
    old_session = str(values.get('assigned_session_id') or '')
    new_session = str(uuid.uuid4())
    values.update(attempt_id=replay_id, cwd=str(root), peer_token='', fleet_db_path=str(store.path),
                  assigned_session_id=new_session)
    values['prompt'] = values['prompt'].replace(str(source), str(root))
    # Relocate workspace paths and the provider session; all other flags/models
    # remain frozen, so the replay cannot touch the original conversation.
    from fleet.worker_runtime import runtime_directory
    old_runtime = str(runtime_directory(WorkerRequest(**bundle['request'])))
    new_runtime = str(runtime_directory(WorkerRequest(**values)))

    def relocate(arg):
        arg = arg.replace(str(source), str(root)).replace(old_runtime, new_runtime)
        return arg.replace(old_session, new_session) if old_session else arg

    values['frozen_argv'] = tuple(relocate(arg) for arg in bundle['argv'])
    request = WorkerRequest(**values)
    now = time.time()
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        number = db.execute('SELECT COALESCE(MAX(attempt_number),0)+1 FROM fleet_attempts WHERE leg_id=?', (leg_id,)).fetchone()[0]
        db.execute('INSERT INTO fleet_attempts(attempt_id,leg_id,attempt_number,state,requested_provider,requested_model,requested_effort,started_at,created_at,updated_at,replay_of) '
            "VALUES(?,?,?,'replay_running',?,?,?,?,?,?,?)", (replay_id, leg_id, number, request.provider, request.model, request.effort, now, now, now, original['attempt_id']))
    result = None
    try:
        deadline = time.monotonic() + 1800
        def event(kind, payload):
            store.append_event(run_id, 'replay.' + kind, payload, leg_id=leg_id, attempt_id=replay_id)
            if kind == 'process.started':
                with store._connect() as db:
                    db.execute('UPDATE fleet_attempts SET pid=?,event_log_path=? WHERE attempt_id=?',
                               (payload.get('pid'), payload.get('event_log_path'), replay_id))
        result = (runner or run_worker)(request, cancel_requested=lambda: time.monotonic() >= deadline, on_event=event)
        from fleet.dag import frozen_replay_context
        snapshot, frozen_leg = frozen_replay_context(bundle, str(root))
        from fleet.artifacts import FleetArtifacts, artifact_capture
        with artifact_capture(FleetArtifacts(store), run_id, leg_id, replay_id):
            verdict = (gate or evaluate_replay_completion)(snapshot, frozen_leg, result.output_text,
                        workspace=str(root), base=base, event_log_path=result.event_log_path)
        accepted = result.ok and verdict.completion_allowed
        difference = ''.join(difflib.unified_diff((original['output_text'] or '').splitlines(True), result.output_text.splitlines(True), fromfile='original', tofile='replay'))
        receipt = {'attempt_id': replay_id, 'number': number, 'replay_of': original['attempt_id'],
                   'accepted': accepted, 'verdict': verdict.to_dict(), 'diff': redact_text(difference)[0],
                   'workspace': str(root), 'argv': list(request.frozen_argv),
                   'input_journal': str(isolation.path),
                   'workspace_relocation': {str(source): str(root), old_runtime: new_runtime},
                   'session': {'original': old_session, 'replay': new_session},
                   'prompt_sha256': hashlib.sha256(request.prompt.encode()).hexdigest()}
        _atomic(root.parent / f'{replay_id}.result.json', json.dumps(receipt, sort_keys=True).encode())
        with store._connect() as db:
            db.execute('UPDATE fleet_attempts SET state=?,output_text=?,exit_code=?,completed_at=?,updated_at=? WHERE attempt_id=?',
                ('replay_completed' if accepted else 'replay_failed', redact_text(result.output_text)[0], result.exit_code, time.time(), time.time(), replay_id))
        store.append_event(run_id, 'leg.replayed', receipt, leg_id=leg_id, attempt_id=replay_id)
        return receipt
    except Exception as exc:
        with store._connect() as db:
            db.execute("UPDATE fleet_attempts SET state='replay_failed',error=?,updated_at=?,completed_at=? WHERE attempt_id=?",
                (redact_text(str(exc))[0], time.time(), time.time(), replay_id))
        raise
