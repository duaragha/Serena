from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp import types

from core import brain_tools


def test_repo_resolution_cannot_escape_projects_root(
    monkeypatch, tmp_path: Path
) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    repo = projects / "serena"
    (repo / ".git").mkdir(parents=True)
    nested = projects / "personal_projects" / "atrium"
    (nested / ".git").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / ".git").mkdir(parents=True)
    (projects / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(brain_tools, "_PROJECTS", projects.resolve())

    assert brain_tools._resolve_repo("serena") == str(repo.resolve())
    assert brain_tools._resolve_repo("atrium") == str(nested.resolve())
    assert brain_tools._resolve_repo("../outside") is None
    assert brain_tools._resolve_repo("escape") is None


def test_git_status_disables_index_refresh_and_fsmonitor(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)

    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook.chmod(0o700)
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.fsmonitor", str(hook)],
        check=True,
    )
    index = repo / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    os.utime(tracked, None)

    output = asyncio.run(
        brain_tools._run_ro(
            [
                *brain_tools._GIT_READ_ONLY_PREFIX,
                "status",
                "--short",
                "--branch",
            ],
            cwd=str(repo),
        )
    )

    assert "No commits yet" in output
    assert not marker.exists()
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


def test_read_only_subprocess_timeout_terminates_and_reaps_child(monkeypatch) -> None:
    created: list[asyncio.subprocess.Process] = []
    real_create = asyncio.create_subprocess_exec

    async def create(*args, **kwargs):
        process = await real_create(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    result = asyncio.run(
        brain_tools._run_ro(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            timeout=0.05,
        )
    )

    assert result == "(timed out after 0.05s)"
    assert len(created) == 1
    assert created[0].returncode is not None


def test_read_only_tool_subprocess_does_not_inherit_ambient_credentials(monkeypatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-reach-tool")

    result = asyncio.run(
        brain_tools._run_ro(
            [
                sys.executable,
                "-c",
                "import os; print(os.environ.get('AWS_SECRET_ACCESS_KEY', 'filtered'))",
            ]
        )
    )

    assert result == "filtered"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group assertion")
def test_read_only_subprocess_timeout_kills_descendant_group(tmp_path: Path) -> None:
    child_pid = tmp_path / "child.pid"
    child = (
        "import os,time; from pathlib import Path; "
        f"Path({str(child_pid)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "time.sleep(60)"
    )

    result = asyncio.run(
        brain_tools._run_ro([sys.executable, "-c", parent], timeout=0.5)
    )

    assert result == "(timed out after 0.5s)"
    assert child_pid.is_file()
    pid = int(child_pid.read_text())
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, 9)
        pytest.fail(f"descendant process {pid} survived read-only timeout")


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group assertion")
def test_read_only_subprocess_cancellation_kills_descendant_group(
    tmp_path: Path,
) -> None:
    child_pid = tmp_path / "cancelled-child.pid"
    child = (
        "import os,time; from pathlib import Path; "
        f"Path({str(child_pid)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "time.sleep(60)"
    )

    async def cancel_running_tree() -> None:
        task = asyncio.create_task(
            brain_tools._run_ro([sys.executable, "-c", parent], timeout=60)
        )
        for _ in range(100):
            if child_pid.is_file():
                break
            await asyncio.sleep(0.01)
        assert child_pid.is_file()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_running_tree())

    pid = int(child_pid.read_text())
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, 9)
        pytest.fail(f"descendant process {pid} survived read-only cancellation")


def test_recall_chats_queries_existing_index_without_writes(
    monkeypatch, tmp_path: Path
) -> None:
    db = tmp_path / "index.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            title TEXT,
            custom_title TEXT,
            first_timestamp TEXT,
            last_timestamp TEXT,
            agent TEXT,
            first_message TEXT
        );
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content,
            session_id UNINDEXED,
            role UNINDEXED,
            timestamp UNINDEXED
        );
        INSERT INTO sessions VALUES (
            '12345678-abcd', 'Voice work', NULL,
            '2026-07-16T10:00:00Z', '2026-07-16T11:00:00Z',
            'codex', 'we fixed the voice queue'
        );
        INSERT INTO sessions VALUES (
            '87654321-abcd', 'Orbital Handoff', NULL,
            '2026-07-16T12:00:00Z', '2026-07-16T13:00:00Z',
            'claude', 'a title-only result'
        );
        INSERT INTO messages_fts VALUES (
            'the blocked vad race is fixed', '12345678-abcd',
            'assistant', '2026-07-16T10:30:00Z'
        );
        """
    )
    conn.commit()
    conn.close()
    db.chmod(0o400)
    before = (db.read_bytes(), db.stat().st_mtime_ns)
    monkeypatch.setattr(brain_tools, "_CHAT_DB_PATH", db)

    result = asyncio.run(brain_tools.recall_chats.handler({"query": "vad"}))

    text = result["content"][0]["text"]
    assert "12345678" in text
    assert "blocked >>>vad<<< race" in text

    title_result = asyncio.run(
        brain_tools.recall_chats.handler({"query": "Orbital"})
    )
    title_text = title_result["content"][0]["text"]
    assert "87654321" in title_text
    assert "Orbital Handoff (title)" in title_text
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before
    assert not Path(f"{db}-journal").exists()
    assert not Path(f"{db}-wal").exists()
    assert not Path(f"{db}-shm").exists()


def test_recall_chats_fails_closed_for_missing_or_invalid_index(
    monkeypatch, tmp_path: Path
) -> None:
    missing = tmp_path / "missing.db"
    monkeypatch.setattr(brain_tools, "_CHAT_DB_PATH", missing)
    assert brain_tools._recall_chat_index("anything") == "(chat index unavailable)"
    assert not missing.exists()

    invalid = tmp_path / "invalid.db"
    invalid.write_text("not sqlite", encoding="utf-8")
    invalid.chmod(0o400)
    before = (invalid.read_bytes(), invalid.stat().st_mtime_ns)
    monkeypatch.setattr(brain_tools, "_CHAT_DB_PATH", invalid)
    result = brain_tools._recall_chat_index("anything")
    assert result.startswith("(chat recall unavailable:")
    assert (invalid.read_bytes(), invalid.stat().st_mtime_ns) == before
    assert not Path(f"{invalid}-journal").exists()
    assert not Path(f"{invalid}-wal").exists()
    assert not Path(f"{invalid}-shm").exists()


def test_github_activity_runs_only_bounded_read_queries(monkeypatch) -> None:
    commands: list[tuple[list[str], str | None]] = []

    async def run_ro(command, cwd=None, timeout=20):
        commands.append((command, cwd))
        return "clean"

    monkeypatch.setattr(brain_tools, "_resolve_repo", lambda repo: "/repo/serena")
    monkeypatch.setattr(brain_tools, "_GH_BINARY", "/usr/bin/gh")
    monkeypatch.setattr(brain_tools, "_run_ro", run_ro)

    result = asyncio.run(brain_tools.github_activity.handler({"repo": "serena"}))

    assert result["content"][0]["text"] == (
        "open PRs:\nclean\n\nrecent issues:\nclean"
    )
    assert commands == [
        (["/usr/bin/gh", "pr", "list", "--limit", "5"], "/repo/serena"),
        (["/usr/bin/gh", "issue", "list", "--limit", "5"], "/repo/serena"),
    ]


def test_git_latest_runs_only_bounded_read_queries(monkeypatch) -> None:
    commands: list[tuple[list[str], str | None]] = []

    async def run_ro(command, cwd=None, timeout=20):
        commands.append((command, cwd))
        return "clean"

    monkeypatch.setattr(brain_tools, "_resolve_repo", lambda repo: "/repo/serena")
    trusted_prefix = [
        "/usr/bin/git",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
    ]
    monkeypatch.setattr(brain_tools, "_GIT_BINARY", "/usr/bin/git")
    monkeypatch.setattr(brain_tools, "_GIT_READ_ONLY_PREFIX", trusted_prefix)
    monkeypatch.setattr(brain_tools, "_run_ro", run_ro)

    result = asyncio.run(brain_tools.git_latest.handler({"repo": "serena"}))

    assert result["content"][0]["text"] == (
        "[/repo/serena]\n\nbranch/status:\nclean\n\nrecent commits:\nclean"
    )
    assert commands == [
        (
            [
                *brain_tools._GIT_READ_ONLY_PREFIX,
                "log",
                "--oneline",
                "-10",
                "--decorate",
            ],
            "/repo/serena",
        ),
        (
            [
                *brain_tools._GIT_READ_ONLY_PREFIX,
                "status",
                "--short",
                "--branch",
            ],
            "/repo/serena",
        ),
    ]


def test_read_only_cli_resolution_rejects_user_owned_path_shims(tmp_path: Path) -> None:
    shim = tmp_path / "git"
    shim.write_text("#!/bin/sh\ntouch mutated\n", encoding="utf-8")
    shim.chmod(0o755)

    assert brain_tools._trusted_system_executable((shim,)) is None
    assert Path(brain_tools._GIT_READ_ONLY_PREFIX[0]).is_absolute()


def test_read_ledger_filters_without_mutating_store(monkeypatch) -> None:
    from memory import store

    rows = [
        {
            "type": "ledger",
            "ledger_key": "gideon",
            "goal": "build the brain",
            "facts": "voice is local",
            "snoozed": False,
        },
        {
            "type": "ledger",
            "ledger_key": "hidden",
            "goal": "do not show",
            "snoozed": True,
        },
        {"type": "task", "ledger_key": "not-a-ledger", "goal": "skip"},
    ]
    monkeypatch.setattr(store, "LEDGER_FIELDS", ("goal", "facts"))
    monkeypatch.setattr(store, "_scan_all", lambda: rows)
    monkeypatch.setattr(store, "_is_snoozed", lambda row: row.get("snoozed", False))

    result = asyncio.run(brain_tools.read_ledger.handler({"name": "gideon"}))

    assert result["content"][0]["text"] == (
        "## gideon\ngoal: build the brain\nfacts: voice is local\n"
    )
    assert rows[0]["facts"] == "voice is local"


def test_brain_server_exposes_exact_annotated_read_only_surface() -> None:
    server = brain_tools.brain_tools_server()
    instance = server["instance"]
    response = asyncio.run(
        instance.request_handlers[types.ListToolsRequest](
            types.ListToolsRequest(method="tools/list")
        )
    )
    exposed = response.root.tools
    names = [item.name for item in exposed]

    assert server["name"] == "serena-ro"
    assert names == [
        "git_latest",
        "github_activity",
        "recall_chats",
        "read_ledger",
        # Read-only recall over everything she knows, added 2026-08-01: only
        # active tasks/loops/ledgers are injected, the rest was unreachable.
        "search_memory",
        "search_knowledge",
        "read_knowledge",
    ]
    assert [f"mcp__serena-ro__{name}" for name in names] == brain_tools.BRAIN_TOOL_NAMES
    assert all(item.annotations.readOnlyHint is True for item in exposed)
    assert all(item.annotations.destructiveHint is False for item in exposed)
    assert all(item.annotations.idempotentHint is True for item in exposed)
    assert next(
        item for item in exposed if item.name == "github_activity"
    ).annotations.openWorldHint is True
    assert all(
        item.annotations.openWorldHint is False
        for item in exposed
        if item.name != "github_activity"
    )
