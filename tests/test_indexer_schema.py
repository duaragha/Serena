"""Schema initialization follows the selected index database."""

from contextlib import closing

import pytest

from core import indexer


@pytest.fixture
def index_db(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer, "DATA_DIR", tmp_path)
    monkeypatch.setattr(indexer, "DB_PATH", tmp_path / "first.db")
    monkeypatch.setattr(indexer, "_schema_ready", False)
    return tmp_path


def test_switching_database_initializes_its_schema(index_db, monkeypatch):
    with closing(indexer._get_db()) as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, project_dir, file_path) "
            "VALUES ('first', 'project', 'first.jsonl')"
        )
        conn.commit()

    monkeypatch.setattr(indexer, "DB_PATH", index_db / "second.db")
    with closing(indexer._get_db()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert "agent" in {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}

    monkeypatch.setattr(indexer, "DB_PATH", index_db / "first.db")
    with closing(indexer._get_db()) as conn:
        assert conn.execute("SELECT session_id FROM sessions").fetchone()[0] == "first"


def test_same_database_does_not_repeat_schema_writes(index_db, monkeypatch):
    with closing(indexer._get_db()):
        pass

    def unexpected_ddl(conn):
        pytest.fail("an initialized database should not repeat schema writes")

    monkeypatch.setattr(indexer, "_create_tables", unexpected_ddl)
    monkeypatch.setattr(indexer, "_migrate", unexpected_ddl)
    with closing(indexer._get_db()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
