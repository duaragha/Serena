"""Acceptance tests for the explicit, local code corpus."""
import asyncio
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from core import code_index as index
from core import code_scanner as scanner


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    from core import config
    monkeypatch.setattr(config, 'KNOWLEDGE_DIR', tmp_path / 'knowledge')
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setattr(index, "DB_PATH", tmp_path / "data/code-index.db")
    monkeypatch.setattr(index, "REGISTRY_PATH", tmp_path / "config/code-repos.json")
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    return root


def put(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def rows():
    with sqlite3.connect(index.DB_PATH) as db:
        return db.execute("SELECT repo_key, rel_path, rowid, content FROM code_fts ORDER BY rowid").fetchall()


def test_scanner_guards_and_redaction(corpus):
    put(corpus, ".gitignore", "*.log\ncache/\n**/private/*.py\nsrc/**/generated.py\n!ignored.log\n")
    for name in ["ignored.log", "cache/x.py", "a/cache/x.py", "a/private/x.py",
                 "src/generated.py", "src/deep/generated.py", "node_modules/x.js",
                 ".env", ".env.local", "Persona.md", "core/security_policy.py"]:
        put(corpus, name, "DO_NOT_READ")
    put(corpus, "huge.py", "x" * (scanner.MAX_FILE_SIZE + 1))
    put(corpus, "min.js", "x" * 501)
    (corpus / "binary.py").write_bytes(b"x\0hidden")
    put(corpus, "src/ok.py", 'api_key = "sk-' + "a" * 40 + '"\ndef some_function(): pass\n')
    result = scanner.scan_repo(corpus)
    assert set(result.files) == {"src/ok.py"}
    assert "sk-" not in result.files["src/ok.py"].text
    assert "REDACTED" in result.files["src/ok.py"].text
    assert result.skipped["binary"] == 1
    assert result.skipped["minified"] == 1
    assert result.skipped["oversize"] == 1


def test_incremental_and_identifier_recall(corpus, monkeypatch):
    key = index.add_repo(corpus)
    put(corpus, "snake.py", "def some_function(): pass\n")
    put(corpus, "camel.py", "def someFunction(): pass\n")
    index.update_code_index()
    for query in ("some_function", "someFunction", "some-function"):
        hits = index.search_code_fts(query)
        assert {r["rel_path"] for r in hits} == {"snake.py", "camel.py"}
        assert all(r["citation"] == f'{key}:{r["rel_path"]}:1-1' for r in hits)
    before = rows()
    with monkeypatch.context() as m:
        m.setattr(scanner, "read_source", lambda *a: pytest.fail("unchanged content read"))
        stats = index.update_code_index()
    assert stats["read"] == stats["updated"] == stats["new"] == 0
    assert rows() == before
    put(corpus, "snake.py", "def changed_function(): pass\n")
    assert index.update_code_index()["updated"] == 1
    assert [r for r in rows() if r[1] == "camel.py"] == [r for r in before if r[1] == "camel.py"]
    (corpus / "camel.py").unlink()
    assert index.update_code_index()["deleted"] == 1
    assert not index.search_code_fts("someFunction")


def test_hash_skip_and_chunk_merging(corpus):
    index.add_repo(corpus)
    source = put(corpus, "long.py", "needle_identifier\n" * 101)
    index.update_code_index()
    before = rows()
    os.utime(source, (source.stat().st_atime, source.stat().st_mtime + 2))
    stats = index.update_code_index()
    assert stats["read"] == 1 and stats["updated"] == 0
    assert rows() == before
    hits = index.search_code_fts("needleIdentifier")
    assert len(hits) == 1
    assert (hits[0]["start_line"], hits[0]["end_line"]) == (1, 101)


def test_identity_survives_relocation(corpus, tmp_path):
    key = index.add_repo(corpus)
    put(corpus, "source.py", "portable_identifier\n")
    index.update_code_index()
    before = rows()
    moved = tmp_path / "other-os"
    corpus.rename(moved)
    index.REGISTRY_PATH.write_text(json.dumps({key: str(moved)}))
    index.update_code_index()
    assert rows() == before
    hit = index.search_code_fts("portableIdentifier")[0]
    assert hit["file_path"] == str(moved / "source.py")
    assert hit["repo_key"] == key


def test_read_only_surfaces_and_drop(corpus, monkeypatch):
    from core import brain_tools, indexer
    index.add_repo(corpus)
    put(corpus, "source.py", "surface_identifier\n")
    index.update_code_index()
    before = index.DB_PATH.read_bytes()
    result = asyncio.run(brain_tools.recall_code.handler({"query": "surfaceIdentifier"}))
    assert index.search_code_fts("surfaceIdentifier")[0]["citation"] in result["content"][0]["text"]
    assert len(result["content"][0]["text"]) <= 4000
    monkeypatch.setattr(indexer, "search_fts", lambda *a, **k: [{"snippet": "chat"}])
    monkeypatch.setattr(indexer, "search_knowledge_fts", lambda *a, **k: [{"source": "knowledge"}])
    monkeypatch.setattr("memory.v2.MemoryV2Store.authority_is_active", lambda: False)
    monkeypatch.setattr("memory.store.search_memories", lambda *a: [{"id": 1, "content": "memory", "type": "project"}])
    assert [r["source"] for r in indexer.unified_search("surfaceIdentifier")] == ["chat", "knowledge", "memory", "code"]
    assert index.DB_PATH.read_bytes() == before
    assert not Path(str(index.DB_PATH) + "-wal").exists()
    index.drop_code_index()
    assert not index.search_code_fts("surfaceIdentifier")
    assert index.load_registry()


def test_cli_roundtrip_real_git_repo(corpus):
    from cli import main
    subprocess.run(["git", "init", "-q", str(corpus)], check=True)
    put(corpus, "demo.py", "cli_identifier\n")
    runner = CliRunner()
    for args in (["add", str(corpus)], ["refresh"], ["list"], ["status"]):
        result = runner.invoke(main, ["code", *args])
        assert result.exit_code == 0, result.output
    result = runner.invoke(main, ["code", "search", "cliIdentifier"])
    assert result.exit_code == 0 and "demo.py:1-1" in result.output
    key = next(iter(index.load_registry()))
    result = runner.invoke(main, ["code", "remove", key])
    assert result.exit_code == 0, result.output
    assert index.load_registry() == {}
    assert index.search_code_fts("cliIdentifier") == []


def test_missing_search_creates_nothing(corpus):
    assert index.search_code_fts("anything") == []
    assert not index.DB_PATH.exists()
    assert not index.REGISTRY_PATH.exists()


def test_registry_rejects_missing_and_nonrepo(corpus):
    with pytest.raises(ValueError):
        index.add_repo(corpus / "missing")
    with pytest.raises(ValueError):
        index.add_repo(corpus.parent)
    index.REGISTRY_PATH.parent.mkdir(parents=True)
    index.REGISTRY_PATH.write_text('[]')
    with pytest.raises(ValueError):
        index.load_registry()


def test_unchanged_refresh_across_process_cache_has_no_reads_or_writes(corpus, monkeypatch):
    index.add_repo(corpus)
    put(corpus, ".gitignore", "ignored/\n")
    put(corpus, "source.py", "fresh_process_identifier\n")
    (corpus / "binary").write_bytes(b"\0")
    index.update_code_index()
    scanner._IGNORE_CACHE.clear()
    before = index.DB_PATH.read_bytes()
    original = Path.read_text

    def guarded_read(path, *args, **kwargs):
        assert not path.is_relative_to(corpus), f"unchanged file read: {path}"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    monkeypatch.setattr(scanner, "read_source", lambda *a: pytest.fail("unchanged source read"))
    statements = []
    get_db = index._get_db

    def traced_db():
        conn = get_db()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(index, "_get_db", traced_db)
    assert index.update_code_index()["read"] == 0
    assert index.DB_PATH.read_bytes() == before
    assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in statements)


def test_cross_os_identity_and_explicit_clones(corpus, monkeypatch):
    windows = r"C:\Users\raghav\Documents\Projects\sample"
    linux = "/home/raghav/Documents/Projects/sample"
    assert index.portable_key(windows) == index.portable_key(linux)
    key = index.portable_key(windows)
    index.add_repo(corpus, repo_key=key)
    put(corpus, "a.py", "cross_os_identifier\n")
    index.update_code_index()
    before = rows()
    index.REGISTRY_PATH.write_text(json.dumps({key: windows}))
    monkeypatch.setattr(index, "canonical_cwd", lambda value: value)
    monkeypatch.setattr(index, "resolve_session_cwd", lambda value: str(corpus))
    original_key = index.portable_key
    monkeypatch.setattr(index, "portable_key", lambda value: key if str(value) == str(corpus) else original_key(value))
    index.update_code_index()
    assert rows() == before
    assert index.search_code_fts("crossOsIdentifier")[0]["file_path"] == str(corpus / "a.py")
    assert original_key(linux + "-fw43") != original_key(linux)


def test_foreign_root_uses_real_layout_translator(corpus, tmp_path, monkeypatch):
    home = tmp_path / "user-home"
    native = home / "Documents/Projects/sample"
    (native / ".git").mkdir(parents=True)
    put(native, "mapped.py", "mapped_identifier")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    foreign = r"C:\Users\other\Projects\sample"
    key = index.add_repo(foreign)
    index.update_code_index()
    before = rows()
    index.REGISTRY_PATH.write_text(json.dumps({key: foreign}))
    index.update_code_index()
    assert rows() == before
    assert index.search_code_fts("mappedIdentifier")[0]["file_path"] == str(native / "mapped.py")
    with pytest.raises(ValueError):
        index.add_repo(foreign + r"\missing")


def test_nested_personal_projects_layout_translates(corpus, tmp_path, monkeypatch):
    """The flat Windows layout also maps onto the nested personal_projects one."""
    home = tmp_path / "user-home"
    native = home / "Documents/Projects/personal_projects/sample"
    (native / ".git").mkdir(parents=True)
    put(native, "nested.py", "nested_identifier")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    foreign = r"C:\Users\other\Projects\sample"
    assert index.resolve_root(foreign) == native.resolve()
    key = index.add_repo(foreign)
    index.update_code_index()
    assert index.search_code_fts("nestedIdentifier")[0]["file_path"] == str(native / "nested.py")
    assert key == index.portable_key(str(native))
    # A parent-directory or home fallback is still never accepted as a root.
    with pytest.raises(ValueError):
        index.add_repo(r"C:\Users\other\Projects\sample\missing\deeper")


def test_snippets_quote_source_text_only(corpus):
    index.add_repo(corpus)
    put(corpus, "camel.py", "def someFunction():\n    return 42\n")
    index.update_code_index()
    source = (corpus / "camel.py").read_text()
    for query in ("some_function", "someFunction"):
        hit = index.search_code_fts(query)[0]
        text = hit["snippet"].replace(">>>", "").replace("<<<", "")
        assert text.strip(". \n") in source, text
        assert "some function" not in text.lower()
    # A hit deep inside a chunk is still shown at the match, not at the head.
    put(corpus, "deep.py", "\n".join(["filler"] * 40 + ["def buriedValue(): pass"]))
    index.update_code_index()
    hit = next(h for h in index.search_code_fts("buried_value") if h["rel_path"] == "deep.py")
    assert hit["snippet"].startswith("filler\nfiller\ndef buriedValue")
    assert (hit["start_line"], hit["end_line"]) == (1, 41)


def test_multiline_credential_redaction_keeps_line_offsets(corpus):
    assert scanner.redact("Bearer\nplaceholder\nneedle_identifier\n").count("\n") == 3
    index.add_repo(corpus)
    lines = ["ordinary"] * 101
    lines[2:4] = ["Authorization: Bearer", "tokenvalue1234567890"]
    lines[50] = "offset_identifier"
    put(corpus, "creds.py", "\n".join(lines))
    index.update_code_index()
    stored = rows()
    assert "tokenvalue1234567890" not in "\n".join(row[3] for row in stored)
    assert stored[0][3].count("\n") == 49
    hit = index.search_code_fts("offsetIdentifier")[0]
    assert (hit["start_line"], hit["end_line"]) == (51, 100)


def test_v1_corpus_is_rebuilt_without_token_pollution(corpus):
    index.add_repo(corpus)
    put(corpus, "camel.py", "def someFunction(): pass\n")
    index.update_code_index()
    index.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(index.DB_PATH) as db:  # forge the shipped v1 layout
        db.executescript("""
            DROP TABLE code_fts;
            CREATE VIRTUAL TABLE code_fts USING fts5 (
                content, repo_key UNINDEXED, rel_path UNINDEXED, chunk_ordinal UNINDEXED,
                start_line UNINDEXED, end_line UNINDEXED, lang UNINDEXED, tokenize='unicode61');
            INSERT INTO code_fts VALUES
                ('def someFunction(): pass\ndef some function pass', 'k', 'camel.py', 0, 1, 1, 'python');
            PRAGMA user_version=1;""")
    assert index.search_code_fts("some_function") == []  # superseded corpus is not served
    assert index.update_code_index()["new"] == 1
    hit = index.search_code_fts("some_function")[0]
    assert "some function" not in hit["snippet"]
    assert hit["citation"].endswith("camel.py:1-1")


def test_ignored_and_protected_files_never_opened(corpus, monkeypatch):
    put(corpus, ".gitignore", "ignored/\n*.secret\n")
    for name in ("ignored/file.py", "password.secret", "Persona.md", ".env", "node_modules/a.js"):
        put(corpus, name, "sensitive")
    put(corpus, "ok.py", "safe_identifier")
    opened = []
    read = scanner.read_source

    def tracked(path):
        opened.append(path.relative_to(corpus).as_posix())
        return read(path)

    monkeypatch.setattr(scanner, "read_source", tracked)
    scanner.scan_repo(corpus)
    assert opened == ["ok.py"]


def test_new_ignore_prunes_and_missing_root_does_not(corpus):
    index.add_repo(corpus)
    put(corpus, "a.py", "preserved_identifier")
    index.update_code_index()
    before = rows()
    corpus.rename(corpus.with_name("unavailable"))
    with pytest.raises((ValueError, OSError)):
        index.update_code_index()
    assert rows() == before
    corpus.with_name("unavailable").rename(corpus)
    put(corpus, ".gitignore", "a.py\n")
    assert index.update_code_index()["deleted"] == 1
    assert rows() == []


def test_independent_lock_skip_and_chat_drop_isolation(corpus, monkeypatch):
    import threading

    from core import indexer
    index.add_repo(corpus)
    put(corpus, "safe.py", "separate_identifier")
    monkeypatch.setattr(indexer, "_get_db", lambda: pytest.fail("chat DB opened"))
    monkeypatch.setattr(indexer, "DATA_DIR", index.DB_PATH.parent)
    monkeypatch.setattr(indexer, "_INDEX_LOCK_PATH", index.DB_PATH.parent / "chat-update.lock")
    with indexer._index_update_lock():
        index.update_code_index()
        assert index.search_code_fts("separateIdentifier")
    results = []
    with index._update_lock():
        thread = threading.Thread(target=lambda: results.append(index.update_code_index(skip_if_running=True)))
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert results[0]["skipped"] is True
    index.drop_code_index()
    assert index.load_registry()


def test_disjoint_chunks_secret_offsets_and_literal_query(corpus):
    index.add_repo(corpus)
    content = ["ordinary"] * 101
    content[0] = "boundary_identifier"
    content[100] = "boundaryIdentifier"
    content[2:5] = ["-----BEGIN PRIVATE KEY-----", "hidden_key_material", "-----END PRIVATE KEY-----"]
    put(corpus, "offsets.py", "\n".join(content))
    index.update_code_index()
    stored = "\n".join(row[3] for row in rows())
    assert "hidden_key_material" not in stored
    assert "REDACTED" in stored
    hits = index.search_code_fts('"boundary_identifier"')
    assert {(h["start_line"], h["end_line"]) for h in hits} == {(1, 50), (101, 101)}
    assert index.search_code_fts('"():*') == []
    assert index.update_code_index(force=True)["updated"] == 0
