import json
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.workspace_catalog import register_fork, remove_deleted_codex_target


def test_native_delete_catalog_cleanup_requires_missing_source_and_preserves_siblings(tmp_path, monkeypatch):
    from core import indexer, metadata

    monkeypatch.setattr(indexer, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(indexer, "DB_PATH", tmp_path / "data" / "index.db")
    monkeypatch.setattr(indexer, "_schema_ready", False)
    monkeypatch.setattr(indexer, "_INDEX_LOCK_PATH", tmp_path / "data" / "index.lock")
    monkeypatch.setattr(metadata, "METADATA_DIR", tmp_path / "metadata")
    monkeypatch.setattr(metadata, "METADATA_PATH", tmp_path / "legacy.json")
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    sid, sibling = str(uuid4()), str(uuid4())
    path = home / "sessions" / f"rollout-2026-09-10T00-00-00-{sid}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": str(tmp_path)}}) + "\n",
        encoding="utf-8",
    )
    metadata._save_one(sid, {"custom_title": "Delete me"})
    metadata._save_one(sibling, {"custom_title": "Keep me"})
    register_fork({"session_id": sid, "provider": "codex", "cwd": str(tmp_path)})
    target = {"session_id": sid, "provider": "codex", "path": str(path)}
    with pytest.raises(RuntimeError, match="still exists"):
        remove_deleted_codex_target(target)
    assert indexer.get_session(sid) is not None and metadata.get_meta(sid)["custom_title"] == "Delete me"
    path.unlink()
    assert remove_deleted_codex_target(target) == {"session_id": sid, "removed": True}
    assert remove_deleted_codex_target(target) == {"session_id": sid, "removed": True}
    assert indexer.get_session(sid) is None and metadata.get_meta(sid) == {}
    assert metadata.get_meta(sibling) == {"custom_title": "Keep me"}
    assert not (indexer.DATA_DIR / "deleted-sessions").exists()


def test_native_archive_index_preserves_custom_metadata_and_restores_exact_row(tmp_path, monkeypatch):
    from core import indexer, metadata
    from core.workspace_catalog import list_saved_sessions

    monkeypatch.setattr(indexer, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(indexer, 'DB_PATH', tmp_path / 'index.db')
    monkeypatch.setattr(indexer, '_schema_ready', False)
    monkeypatch.setattr(indexer, '_INDEX_LOCK_PATH', tmp_path / 'index.lock')
    monkeypatch.setattr(metadata, 'METADATA_DIR', tmp_path / 'metadata')
    monkeypatch.setattr(metadata, 'METADATA_PATH', tmp_path / 'legacy.json')
    home = tmp_path / 'codex'
    monkeypatch.setenv('CODEX_HOME', str(home))
    sid, sibling = str(uuid4()), str(uuid4())
    path = home / 'sessions' / f'rollout-2026-09-10T00-00-00-{sid}.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': sid, 'cwd': str(tmp_path)}}) + '\n')
    target = {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)}
    metadata._save_one(sid, {'custom_title': 'Keep my title', 'group': 'linked-group', 'starred': True,
                             'done': True, 'done_at': '2099-01-01T00:00:00+00:00'})
    metadata._save_one(sibling, {'custom_title': 'Sibling untouched', 'group': 'linked-group'})
    register_fork(target)
    before = metadata.get_meta(sid)
    sibling_before = metadata.get_meta(sibling)
    assert indexer.list_sessions()[0]['session_id'] == sid
    archived = home / 'archived_sessions' / path.name
    archived.parent.mkdir()
    path.rename(archived)
    register_fork(target)
    assert not indexer.list_sessions()
    assert not list_saved_sessions('codex')['data']
    rows = indexer.list_sessions(archived=True)
    assert len(rows) == 1 and rows[0]['session_id'] == sid and rows[0]['is_done'] == 1
    assert rows[0]['display_title'] == 'Keep my title'
    assert list_saved_sessions('codex', archived=True)['data'][0]['session_id'] == sid
    assert indexer.get_session(sid)['file_path'] == str(archived)
    assert metadata.get_meta(sid) == before and metadata.get_meta(sibling) == sibling_before
    archived.rename(path)
    register_fork(target)
    assert indexer.list_sessions()[0]['session_id'] == sid
    assert not indexer.list_sessions(archived=True)
    assert not list_saved_sessions('codex', archived=True)['data']
    assert metadata.get_meta(sid) == before and metadata.get_meta(sibling) == sibling_before


def test_archive_schema_migration_preserves_existing_rows(tmp_path):
    import sqlite3

    from core.indexer import _migrate

    conn = sqlite3.connect(tmp_path / 'old.db')
    try:
        conn.execute('CREATE TABLE sessions (session_id TEXT PRIMARY KEY, custom_title TEXT, is_done INTEGER, file_path TEXT, agent TEXT)')
        conn.execute("INSERT INTO sessions VALUES ('exact','Custom title',1,'/sessions/file.jsonl','codex')")
        conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", ('archived', 'Archived title', 0, r'C:\Users\name\.codex\archived_sessions\rollout.jsonl', 'codex'))
        conn.execute("INSERT INTO sessions VALUES ('claude','Claude title',0,'/project/archived_sessions/file.jsonl','claude')")
        _migrate(conn)
        _migrate(conn)
        assert conn.execute("SELECT session_id,custom_title,is_done,is_archived FROM sessions WHERE session_id='exact'").fetchone() == ('exact', 'Custom title', 1, 0)
        assert conn.execute("SELECT is_archived FROM sessions WHERE session_id='archived'").fetchone() == (1,)
        assert conn.execute("SELECT is_archived FROM sessions WHERE session_id='claude'").fetchone() == (0,)
    finally:
        conn.close()


def test_claude_native_title_reindexes_without_overwriting_custom_title(tmp_path, monkeypatch):
    from core import indexer
    from core.parser import parse_metadata
    from core.workspace_catalog import list_saved_sessions

    monkeypatch.setattr(indexer, "DATA_DIR", tmp_path)
    monkeypatch.setattr(indexer, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(indexer, "_schema_ready", False)
    sid = str(uuid4())
    path = tmp_path / f"{sid}.jsonl"
    records = [
        {"type": "user", "cwd": str(tmp_path), "timestamp": "2026-09-10T12:00:00Z",
         "message": {"role": "user", "content": "Original request"}},
        {"type": "custom-title", "sessionId": sid, "customTitle": "First rename"},
        {"type": "custom-title", "sessionId": sid, "customTitle": "Native rename"},
        {"type": "custom-title", "sessionId": str(uuid4()), "customTitle": "Wrong session"},
    ]
    for invalid in (None, {}, "", "  ", "bad\nname", "x" * 1001):
        records.append({"type": "custom-title", "sessionId": sid, "customTitle": invalid})
    path.write_text("\n".join(map(json.dumps, records)) + "\n{partial\n")
    meta = parse_metadata(path, "project")
    assert meta.native_title == "Native rename"
    assert meta.message_count == 1
    conn = indexer._get_db()
    try:
        indexer._upsert_session(conn, meta, all_meta={}, agent="claude")
        conn.commit()
        assert list_saved_sessions("claude")["data"][0]["title"] == "Native rename"
        indexer._upsert_session(conn, meta, all_meta={sid: {"custom_title": "My explicit title"}}, agent="claude")
        conn.commit()
        assert list_saved_sessions("claude")["data"][0]["title"] == "My explicit title"
    finally:
        conn.close()


def test_saved_session_search_uses_full_provider_catalog_and_literal_query(tmp_path, monkeypatch):
    from core import indexer
    from core.workspace_catalog import list_saved_sessions

    monkeypatch.setattr(indexer, "DATA_DIR", tmp_path)
    monkeypatch.setattr(indexer, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(indexer, "_schema_ready", False)
    conn = indexer._get_db()
    for i in range(53):
        conn.execute("INSERT INTO sessions (session_id,project_dir,file_path,agent,title,custom_title,last_timestamp,is_teammate) VALUES (?,?,?,?,?,?,?,?)",
                     (str(uuid4()), "project", "missing.jsonl", "claude" if i < 52 else "codex", "original", "100%_custom" if i == 0 else str(i), str(i).zfill(3), 1 if i == 51 else 0))
    conn.commit()
    before = conn.execute("SELECT * FROM sessions ORDER BY session_id").fetchall()
    first = list_saved_sessions("claude")
    second = list_saved_sessions("claude", offset=first["nextOffset"])
    assert len(first["data"]) == 50 and len(second["data"]) == 1
    assert second["nextOffset"] is None
    assert len(list_saved_sessions("codex")["data"]) == 1
    assert [row["title"] for row in list_saved_sessions("claude", "%_")["data"]] == ["100%_custom"]
    assert not list_saved_sessions("claude", "' OR 1=1 --")["data"]
    assert before == conn.execute("SELECT * FROM sessions ORDER BY session_id").fetchall()
    conn.close()


@pytest.mark.parametrize("provider,query,offset", [("gemini", "", 0), ("claude", "x" * 201, 0), ("codex", "", -1)])
def test_saved_session_search_rejects_invalid_arguments(provider, query, offset):
    from core.workspace_catalog import list_saved_sessions

    with pytest.raises(ValueError):
        list_saved_sessions(provider, query, offset)


@pytest.mark.parametrize('archive_query,expected', [('', False), ('&archived=false', False), ('&archived=true', True)])
def test_saved_session_route_requires_auth_without_calling_owner(monkeypatch, archive_query, expected):
    from flask import Flask

    from ui.workspace_web import workspace_blueprint

    calls = []
    monkeypatch.setattr("core.workspace_catalog.list_saved_sessions", lambda *args, **kwargs: calls.append((args, kwargs)) or {"data": [], "nextOffset": None})
    app = Flask(__name__)
    app.register_blueprint(workspace_blueprint(SimpleNamespace(decorate_archive_restores=lambda page: page), token="s" * 40))
    client = app.test_client()
    path = "/api/workspace/exact/sessions?provider=codex&q=custom&offset=50" + archive_query
    assert client.get(path).status_code == 403
    assert client.get(path, headers={"X-Serena-Workspace-Token": "s" * 40, "Origin": "https://other.test"}).status_code == 403
    assert not calls
    assert client.get(path, headers={"X-Serena-Workspace-Token": "s" * 40}).json == {"data": [], "nextOffset": None}
    assert calls == [(("codex", "custom", 50), {"archived": expected})]


@pytest.mark.parametrize('query', ['archived=', 'archived=1', 'archived=True', 'archived=null',
                                  'archived=true&archived=false', 'archived=true&archived=true'])
def test_saved_session_route_rejects_ambiguous_archive_filter(monkeypatch, query):
    from flask import Flask

    from ui.workspace_web import workspace_blueprint

    def unexpected(*args, **kwargs):
        pytest.fail('Invalid archive filter reached the catalog')
    monkeypatch.setattr('core.workspace_catalog.list_saved_sessions', unexpected)
    app = Flask(__name__)
    app.register_blueprint(workspace_blueprint(object(), token='s' * 40))
    response = app.test_client().get('/api/workspace/exact/sessions?provider=codex&' + query,
                                     headers={'X-Serena-Workspace-Token': 's' * 40})
    assert response.status_code == 400
    assert response.json == {'ok': False, 'error': 'Expected one boolean archived filter'}


def test_explicit_handoff_uses_exact_owner_and_refuses_failed_attachment():
    from flask import Flask

    from ui.workspace_web import workspace_blueprint

    calls = []
    host = SimpleNamespace(
        attach=lambda sid: calls.append(("attach", sid)) or {"ok": True},
        bridge=lambda *args, **kwargs: calls.append(("bridge", args, kwargs)) or {"ok": True, "queued": True},
    )
    app = Flask(__name__)
    app.register_blueprint(workspace_blueprint(host, token="s" * 40))
    client = app.test_client()
    payload = {"provider": "codex", "prompt": "Briefing", "request_id": "stable"}
    url = "/api/workspace/exact/handoff"
    headers = {"X-Serena-Workspace-Token": "s" * 40}
    assert client.post(url, json=payload).status_code == 403
    assert client.post(url, json={**payload, "prompt": ""}, headers=headers).status_code == 400
    assert not calls
    assert client.post(url, json=payload, headers=headers).json == {"ok": True, "queued": True}
    assert calls == [("attach", "exact"), ("bridge", ("exact", "codex", "Briefing", "stable"), {"timeout": 1})]
    calls.clear()
    host.attach = lambda sid: {"ok": False, "error": "Already owned elsewhere"}
    assert client.post(url, json=payload, headers=headers).json["ok"] is False
    assert not calls


@pytest.mark.parametrize("field", ["uuid", "promptId"])
def test_claude_registration_waits_for_exact_completed_prompt(tmp_path, monkeypatch, field):
    from core.workspace_catalog import NativeTranscriptPending

    sid, prompt = str(uuid4()), str(uuid4())
    config = tmp_path / "config"
    path = config / "projects" / "project" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"type": "user", field: "old-prompt"}) + "\n{partial\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setattr("core.parser.parse_metadata", lambda *args: SimpleNamespace(session_id=sid, cwd=str(tmp_path)))
    calls = []
    monkeypatch.setattr("core.indexer._index_update_lock", nullcontext)
    monkeypatch.setattr("core.indexer._get_db", lambda: SimpleNamespace(commit=lambda: None, close=lambda: None))
    monkeypatch.setattr("core.indexer._upsert_session", lambda *args, **kwargs: calls.append("indexed"))
    target = {"session_id": sid, "provider": "claude", "cwd": str(tmp_path), "prompt_id": prompt}
    with pytest.raises(NativeTranscriptPending):
        register_fork(target)
    assert not calls
    path.write_text(json.dumps({"type": "assistant", field: prompt}) + "\n")
    with pytest.raises(NativeTranscriptPending):
        register_fork(target)
    path.write_text(json.dumps({"type": "user", field: prompt}) + "\n")
    register_fork(target)
    assert calls == ["indexed"]


def test_codex_registration_marks_owned_before_upsert(tmp_path, monkeypatch):
    sid = str(uuid4())
    home = tmp_path / "codex"
    path = home / "sessions" / f"rollout-2026-09-09T00-00-00-{sid}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": str(tmp_path)}}) + "\n")
    calls = []
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr("core.metadata.set_resident_work", lambda target: calls.append(("owned", target)))
    monkeypatch.setattr("core.indexer._index_update_lock", nullcontext)
    monkeypatch.setattr("core.indexer._get_db", lambda: SimpleNamespace(commit=lambda: calls.append("commit"), close=lambda: calls.append("close")))
    monkeypatch.setattr("core.indexer._upsert_session", lambda conn, meta, agent: calls.append(("index", meta.session_id, agent)))
    register_fork({"session_id": sid, "provider": "codex", "cwd": str(tmp_path)})
    assert calls == [("owned", sid), ("index", sid, "codex"), "commit", "close"]


@pytest.mark.parametrize('confirmation', ['live', 'passive', 'missing', 'stale'])
def test_claude_explicit_rename_replaces_only_after_native_confirmation(tmp_path, monkeypatch, confirmation):
    from core import indexer, metadata
    from core.workspace_catalog import list_saved_sessions

    monkeypatch.setattr(indexer, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(indexer, 'DB_PATH', tmp_path / 'index.db')
    monkeypatch.setattr(indexer, '_schema_ready', False)
    monkeypatch.setattr(metadata, 'METADATA_DIR', tmp_path / 'metadata')
    monkeypatch.setattr(metadata, 'METADATA_PATH', tmp_path / 'legacy.json')
    home = tmp_path / 'claude'
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(home))
    sid, sibling = str(uuid4()), str(uuid4())
    path = home / 'projects' / 'project' / f'{sid}.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text('\n'.join(map(json.dumps, [
        {'type': 'user', 'cwd': str(tmp_path), 'timestamp': '2026-09-10T12:00:00Z',
         'message': {'role': 'user', 'content': 'Original request'}},
        {'type': 'custom-title', 'sessionId': sid, 'customTitle': 'Native replacement'},
    ])) + '\n')
    metadata.set_custom_title(sid, 'Previous explicit title')
    metadata.set_custom_title(sibling, 'Sibling title')
    target = {'session_id': sid, 'provider': 'claude', 'cwd': str(tmp_path)}
    register_fork(target)
    if confirmation != 'passive':
        target['confirmed_native_name'] = 'Native replacement'
    if confirmation in {'live', 'stale'}:
        target['expected_native_title'] = 'Native replacement' if confirmation == 'live' else 'Older title'
    if confirmation in {'missing', 'stale'}:
        with pytest.raises(ValueError):
            register_fork(target)
    else:
        result = register_fork(target)
        if confirmation == 'live':
            assert result == {'display_title': 'Native replacement', 'native_rename': True}
    expected = 'Native replacement' if confirmation == 'live' else 'Previous explicit title'
    assert metadata.get_meta(sid)['custom_title'] == expected
    assert list_saved_sessions('claude')['data'][0]['title'] == expected
    assert metadata.get_meta(sibling)['custom_title'] == 'Sibling title'


def test_confirmed_native_codex_rename_updates_only_exact_metadata_and_index(tmp_path, monkeypatch):
    from core import indexer, metadata
    from core.workspace_catalog import list_saved_sessions

    monkeypatch.setattr(indexer, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(indexer, 'DB_PATH', tmp_path / 'index.db')
    monkeypatch.setattr(indexer, '_schema_ready', False)
    monkeypatch.setattr(metadata, 'METADATA_DIR', tmp_path / 'metadata')
    monkeypatch.setattr(metadata, 'METADATA_PATH', tmp_path / 'legacy.json')
    sid, other = str(uuid4()), str(uuid4())
    home = tmp_path / 'codex'
    path = home / 'sessions' / f'rollout-2026-09-10T00-00-00-{sid}.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': sid, 'cwd': str(tmp_path)}}) + '\n')
    monkeypatch.setenv('CODEX_HOME', str(home))
    metadata.set_custom_title(sid, 'Previous explicit title')
    metadata.set_custom_title(other, 'Unrelated title')
    result = register_fork({'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path), 'confirmed_native_name': 'Native replacement'})
    assert result == {'display_title': 'Native replacement'}
    assert metadata.get_meta(sid)['custom_title'] == 'Native replacement'
    assert metadata.get_meta(other)['custom_title'] == 'Unrelated title'
    assert list_saved_sessions('codex')['data'][0]['title'] == 'Native replacement'


@pytest.mark.parametrize("case", ["missing", "ambiguous", "wrong-project", "wrong-id", "outside", "missing-history"])
def test_invalid_codex_fork_never_writes_catalog(tmp_path, monkeypatch, case):
    sid = str(uuid4())
    home = tmp_path / "codex"
    path = home / "sessions" / "2026" / f"rollout-2026-09-09T00-00-00-{sid}.jsonl"
    path.parent.mkdir(parents=True)
    payload = {"id": str(uuid4()) if case == "wrong-id" else sid,
               "cwd": str(tmp_path / "wrong") if case == "wrong-project" else str(tmp_path)}
    if case == "missing-history":
        payload["history_base"] = {"thread_id": str(uuid4()), "end_byte_offset": 1, "end_ordinal_exclusive": 1}
    if case != "missing":
        path.write_text(json.dumps({"type": "session_meta", "payload": payload}) + "\n")
    if case == "ambiguous":
        other = home / "archived_sessions" / path.name
        other.parent.mkdir()
        other.write_bytes(path.read_bytes())
    if case == "outside":
        outside = tmp_path / "outside.jsonl"
        path.rename(outside)
        path.symlink_to(outside)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr("core.indexer._get_db", lambda: pytest.fail("Invalid fork reached catalog write"))
    with pytest.raises(ValueError):
        register_fork({"session_id": sid, "provider": "codex", "cwd": str(tmp_path)})


@pytest.mark.parametrize("case", ["missing", "ambiguous", "wrong-project", "wrong-id", "outside"])
def test_invalid_native_fork_never_writes_catalog(tmp_path, monkeypatch, case):
    sid = str(uuid4())
    config = tmp_path / "config"
    projects = config / "projects"
    path = projects / "project" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True)
    if case != "missing":
        path.write_text("{}\n")
    if case == "ambiguous":
        other = projects / "other" / path.name
        other.parent.mkdir()
        other.write_text("{}\n")
    if case == "outside":
        outside = tmp_path / "external.jsonl"
        outside.write_text("{}\n")
        path.unlink()
        path.symlink_to(outside)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setattr("core.parser.parse_metadata", lambda *args: SimpleNamespace(
        session_id=str(uuid4()) if case == "wrong-id" else sid,
        cwd=str(tmp_path / "wrong") if case == "wrong-project" else str(tmp_path),
    ))
    monkeypatch.setattr("core.indexer._get_db", lambda: pytest.fail("Invalid fork reached catalog write"))
    with pytest.raises(ValueError):
        register_fork({"session_id": sid, "provider": "claude", "cwd": str(tmp_path)})
