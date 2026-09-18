import fcntl
import json
import sqlite3
from pathlib import Path

from voice.desktop.live_session_terminal import (
    interactive_command,
    run_provider_runtime,
    run_terminal,
)

ITEM_ID = "b62f3779-9bc6-4d19-b1d9-6384fc4743e9"
SESSION_ID = "019fc3b5-2acb-7492-8d76-21f3007f8bdb"


class FakeProcess:
    def __init__(self) -> None:
        self.waited = False
        self.pid = 4242

    def poll(self):
        return None

    def wait(self):
        self.waited = True
        return 0


def _target(project: Path, provider: str = "codex") -> dict:
    return {
        "item_id": ITEM_ID,
        "state": "completed",
        "session_id": SESSION_ID,
        "provider": provider,
        "project_root": str(project),
    }


def _argument(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_interactive_commands_resume_only_the_exact_persisted_session(tmp_path: Path) -> None:
    binaries = {"codex": "/opt/bin/codex", "claude": "/opt/bin/claude"}
    which = binaries.get

    assert interactive_command(_target(tmp_path), which=which) == [
        "/opt/bin/codex",
        "resume",
        SESSION_ID,
    ]
    assert interactive_command(_target(tmp_path, "claude"), which=which) == [
        "/opt/bin/claude",
        "--dangerously-skip-permissions",
        "-r",
        SESSION_ID,
    ]


def test_broker_claims_exact_session_and_launches_real_terminal(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    database = tmp_path / "voice.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE voice_inbox(item_id TEXT PRIMARY KEY, state TEXT)")
    connection.execute("CREATE TABLE voice_work(item_id TEXT PRIMARY KEY, state TEXT)")
    connection.execute("INSERT INTO voice_inbox VALUES (?, 'delivered')", (ITEM_ID,))
    connection.execute("INSERT INTO voice_work VALUES (?, 'completed')", (ITEM_ID,))
    connection.commit()
    connection.close()
    emitted = []
    claims = []
    releases = []
    launches = []
    process = FakeProcess()

    def resolver(_database, item_id):
        assert item_id == ITEM_ID
        return _target(project), ""

    def popen(argv, **options):
        launches.append((argv, options))
        token = _argument(argv, "--ready-token")
        lock_dir = Path(_argument(argv, "--lock-dir"))
        ready_path = lock_dir / f"{SESSION_ID}.{token}.ready"
        ready_path.write_text(
            json.dumps(
                {
                    "token": token,
                    "session_id": SESSION_ID,
                    "provider": "codex",
                    "pid": 5252,
                }
            ),
            encoding="utf-8",
        )
        return process

    result = run_terminal(
        ITEM_ID,
        database=database,
        terminal_executable="/usr/bin/gnome-terminal",
        lock_dir=tmp_path / "locks",
        resolver=resolver,
        runtime_active=lambda _sid: False,
        claim_runtime=lambda sid, **kwargs: claims.append((sid, kwargs)) or {},
        release_runtime=lambda sid, **kwargs: releases.append((sid, kwargs)) or True,
        popen=popen,
        which=lambda provider: f"/opt/bin/{provider}",
        emit=emitted.append,
    )

    assert result == 0
    assert process.waited is True
    assert claims[0][0] == SESSION_ID
    assert claims[0][1]["kind"] == "coding-terminal"
    assert releases == [(SESSION_ID, {"pid": claims[0][1]["pid"]})]
    assert emitted == [{"ok": True, "session_id": SESSION_ID, "provider": "codex"}]
    argv, options = launches[0]
    assert argv[:7] == [
        "/usr/bin/gnome-terminal",
        "--wait",
        "--title",
        f"Serena coding {ITEM_ID[:8]}",
        "--working-directory",
        str(project),
        "--",
    ]
    assert argv[8:11] == ["-m", "voice.desktop.live_session_terminal", "--provider-runtime"]
    assert _argument(argv, "--provider") == "codex"
    assert _argument(argv, "--session-id") == SESSION_ID
    assert _argument(argv, "--project-root") == str(project)
    assert len(_argument(argv, "--ready-token")) == 32
    assert _argument(argv, "--lock-dir") == str(tmp_path / "locks")
    assert options["cwd"] == str(project)
    assert options["start_new_session"] is True

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT state FROM voice_inbox WHERE item_id=?", (ITEM_ID,)
        ).fetchone()[0] == "delivered"
        assert connection.execute(
            "SELECT state FROM voice_work WHERE item_id=?", (ITEM_ID,)
        ).fetchone()[0] == "completed"
    finally:
        connection.close()


def test_provider_readiness_is_written_only_after_real_cli_process_starts(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    token = "a" * 32
    launches = []
    process = FakeProcess()

    def popen(argv, **options):
        launches.append((argv, options))
        return process

    result = run_provider_runtime(
        provider="codex",
        session_id=SESSION_ID,
        project_root=project,
        ready_token=token,
        lock_dir=lock_dir,
        which=lambda provider: f"/opt/bin/{provider}",
        popen=popen,
        isatty=lambda _descriptor: True,
    )

    assert result == 0
    assert process.waited is True
    assert launches[0][0] == [
        "/opt/bin/codex",
        "resume",
        SESSION_ID,
    ]
    assert launches[0][1]["cwd"] == str(project)
    ready = json.loads((lock_dir / f"{SESSION_ID}.{token}.ready").read_text())
    assert ready == {
        "token": token,
        "session_id": SESSION_ID,
        "provider": "codex",
        "pid": process.pid,
    }


def test_provider_runtime_rejects_a_non_terminal_without_starting_cli(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    launches = []

    result = run_provider_runtime(
        provider="codex",
        session_id=SESSION_ID,
        project_root=project,
        ready_token="b" * 32,
        lock_dir=tmp_path,
        popen=lambda *args, **kwargs: launches.append((args, kwargs)),
        isatty=lambda _descriptor: False,
    )

    assert result == 2
    assert launches == []


def test_broker_rejects_busy_or_unresolvable_session_without_launch(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    launches = []
    emitted = []

    result = run_terminal(
        ITEM_ID,
        database=tmp_path / "voice.sqlite3",
        terminal_executable="/usr/bin/gnome-terminal",
        lock_dir=tmp_path / "locks-a",
        resolver=lambda *_args: (_target(project), ""),
        runtime_active=lambda _sid: True,
        popen=lambda *args, **kwargs: launches.append((args, kwargs)),
        emit=emitted.append,
    )
    assert result == 3
    assert launches == []
    assert emitted[-1] == {
        "ok": False,
        "error": "persisted session already has an active runtime",
    }

    result = run_terminal(
        ITEM_ID,
        database=tmp_path / "voice.sqlite3",
        terminal_executable="/usr/bin/gnome-terminal",
        lock_dir=tmp_path / "locks-b",
        resolver=lambda *_args: (None, "background coding session is still active"),
        popen=lambda *args, **kwargs: launches.append((args, kwargs)),
        emit=emitted.append,
    )
    assert result == 2
    assert launches == []
    assert emitted[-1] == {
        "ok": False,
        "error": "background coding session is still active",
    }


def test_broker_lock_prevents_a_second_interactive_writer(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    emitted = []
    launches = []

    with (lock_dir / f"{SESSION_ID}.lock").open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run_terminal(
            ITEM_ID,
            database=tmp_path / "voice.sqlite3",
            terminal_executable="/usr/bin/gnome-terminal",
            lock_dir=lock_dir,
            resolver=lambda *_args: (_target(project), ""),
            popen=lambda *args, **kwargs: launches.append((args, kwargs)),
            emit=emitted.append,
        )

    assert result == 3
    assert launches == []
    assert emitted == [{"ok": False, "error": "interactive terminal is already open"}]
