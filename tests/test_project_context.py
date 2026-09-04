import sqlite3
from pathlib import Path
from core import project_context


def test_find_project_root_finds_git_parent(tmp_path):
    repo = tmp_path / "my_repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "apps" / "web"
    sub.mkdir(parents=True)

    assert project_context.find_project_root(sub) == repo
    assert project_context.find_project_root(repo) == repo


def test_format_project_context_with_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            agent TEXT,
            title TEXT,
            custom_title TEXT,
            first_message TEXT,
            last_timestamp TEXT,
            git_branch TEXT,
            cwd TEXT,
            project_dir TEXT
        )
    """)
    repo = tmp_path / "test-repo"
    (repo / ".git").mkdir(parents=True)
    repo_str = str(repo)

    conn.execute("""
        INSERT INTO sessions (session_id, agent, title, custom_title, first_message, last_timestamp, git_branch, cwd, project_dir)
        VALUES
        ('11111111-1111', 'claude', 'First Chat', NULL, 'hi', '2026-09-01T12:00:00Z', 'main', ?, 'test-repo'),
        ('22222222-2222', 'gemini', 'Second Chat', 'Custom Title', 'hello', '2026-09-02T12:00:00Z', 'feat-x', ?, 'test-repo')
    """, (repo_str, repo_str))
    conn.commit()
    conn.close()

    monkeypatch.setattr(project_context, "DB_PATH", db_path)

    out = project_context.format_project_context(repo)
    assert "[PROJECT CONTEXT: test-repo]" in out
    assert "Recent sessions in this repository" in out
    assert "Custom Title" in out
    assert "First Chat" in out
    assert "[feat-x]" in out
    assert "22222222" in out
    assert "11111111" in out


def test_format_project_context_exclude_sid(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            agent TEXT,
            title TEXT,
            custom_title TEXT,
            first_message TEXT,
            last_timestamp TEXT,
            git_branch TEXT,
            cwd TEXT,
            project_dir TEXT
        )
    """)
    repo = tmp_path / "test-repo"
    repo_str = str(repo)

    conn.execute("""
        INSERT INTO sessions (session_id, agent, title, custom_title, first_message, last_timestamp, git_branch, cwd, project_dir)
        VALUES
        ('curr-session-id', 'claude', 'Current Chat', NULL, 'hi', '2026-09-03T12:00:00Z', 'main', ?, 'test-repo'),
        ('prev-session-id', 'codex', 'Previous Chat', NULL, 'hey', '2026-09-02T12:00:00Z', 'main', ?, 'test-repo')
    """, (repo_str, repo_str))
    conn.commit()
    conn.close()

    monkeypatch.setattr(project_context, "DB_PATH", db_path)

    out = project_context.format_project_context(repo, exclude_sid="curr-session-id")
    assert "Previous Chat" in out
    assert "Current Chat" not in out
