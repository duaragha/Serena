import sqlite3

from core.desktop_profile import seed_dev_index


def test_dev_takes_an_independent_consistent_wal_snapshot_once(tmp_path):
    source = tmp_path / '.local/share/chats/index.db'
    source.parent.mkdir(parents=True)
    target = tmp_path / '.local/share/chats-dev/index.db'
    with sqlite3.connect(source) as stable:
        stable.execute('PRAGMA journal_mode=WAL')
        stable.execute('CREATE TABLE proof(value TEXT)')
        stable.execute("INSERT INTO proof VALUES ('stable')")
        stable.commit()
        assert seed_dev_index(home=tmp_path, environ={'SERENA_DESKTOP_CHANNEL': 'stable'}) is False
        assert not target.exists()
        assert seed_dev_index(home=tmp_path, environ={'SERENA_DESKTOP_CHANNEL': 'dev'}) is True
        with sqlite3.connect(target) as dev:
            assert dev.execute('SELECT value FROM proof').fetchone()[0] == 'stable'
            dev.execute("UPDATE proof SET value='dev'")
            dev.commit()
        assert stable.execute('SELECT value FROM proof').fetchone()[0] == 'stable'
        assert seed_dev_index(home=tmp_path, environ={'SERENA_DESKTOP_CHANNEL': 'dev'}) is False
    with sqlite3.connect(target) as dev:
        assert dev.execute('SELECT value FROM proof').fetchone()[0] == 'dev'
    assert not list(target.parent.glob('.index-seed-*'))


def test_missing_or_invalid_cache_is_optional(tmp_path):
    env = {'SERENA_DESKTOP_CHANNEL': 'dev'}
    assert seed_dev_index(home=tmp_path, environ=env) is False
    source = tmp_path / '.local/share/chats/index.db'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'not a database')
    assert seed_dev_index(home=tmp_path, environ=env) is False
    assert not (tmp_path / '.local/share/chats-dev/index.db').exists()
