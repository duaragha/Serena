from __future__ import annotations

import json
from types import SimpleNamespace

from click.testing import CliRunner

import cli


def test_fleet_worker_environment_blocks_all_serena_delegation_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("SERENA_FLEET_WORKER", "1")
    monkeypatch.setenv("SERENA_FLEET_DB_PATH", str(tmp_path / "fleet.sqlite3"))

    nested = CliRunner().invoke(
        cli.main,
        ["fleet", "start", "--dry-run", "research recursion"],
    )
    codex = CliRunner().invoke(cli.main, ["codex-exec", "nested job"])
    ask_claude = CliRunner().invoke(cli.main, ["ask-claude", "nested bridge"])
    ask_codex = CliRunner().invoke(cli.main, ["ask-codex", "nested bridge"])

    assert nested.exit_code != 0
    assert "nested Fleet runs are disabled" in nested.output
    assert json.loads(codex.output)["error"] == (
        "nested Codex jobs are disabled inside Fleet workers"
    )
    assert ask_claude.exit_code != 0
    assert "linked-agent bridges are disabled" in ask_claude.output
    assert ask_codex.exit_code != 0
    assert "linked-agent bridges are disabled" in ask_codex.output


def test_human_fleet_cli_prints_store_state(monkeypatch, tmp_path):
    monkeypatch.delenv("SERENA_FLEET_WORKER", raising=False)
    monkeypatch.setenv("SERENA_FLEET_DB_PATH", str(tmp_path / "fleet.sqlite3"))
    monkeypatch.setenv("SERENA_FLEET_NO_AUTOSTART", "1")

    result = CliRunner().invoke(
        cli.main,
        [
            "fleet",
            "start",
            "--dry-run",
            "--activity",
            "research",
            "--cwd",
            str(tmp_path),
            "controlled question",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "planned" in result.output


def test_fleet_cli_forwards_provider_and_worker_flags(monkeypatch, tmp_path):
    from core import fleet_supervisor

    captured = {}

    def fake_start(task, **kwargs):
        captured.update({"task": task, **kwargs})
        return {"run_id": "cli-provider-run", "state": "planned", "task": task}

    monkeypatch.setattr(fleet_supervisor, "start_run", fake_start)
    result = CliRunner().invoke(
        cli.main,
        [
            "fleet",
            "start",
            "--provider",
            "claude",
            "--workers",
            "1",
            "--dry-run",
            "--cwd",
            str(tmp_path),
            "research with claude only",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["provider_mode"] == "claude"
    assert captured["worker_count"] == 1
    assert captured["dry_run"] is True


def test_fleet_cli_handoff_forwards_the_exact_worker(monkeypatch):
    from core import fleet_supervisor

    captured = {}

    def fake_handoff(run_id, leg_id, provider):
        captured.update(run_id=run_id, leg_id=leg_id, provider=provider)
        return {"run_id": run_id, "state": "queued", "task": "pickup"}

    monkeypatch.setattr(fleet_supervisor, "handoff_leg", fake_handoff)
    result = CliRunner().invoke(
        cli.main,
        ["fleet", "handoff", "fleet-1", "leg-2", "codex"],
    )

    assert result.exit_code == 0, result.output
    assert captured == {"run_id": "fleet-1", "leg_id": "leg-2", "provider": "codex"}


def test_fleet_cli_delete_forwards_the_exact_run(monkeypatch):
    from core import fleet_supervisor

    captured = []
    monkeypatch.setattr(
        fleet_supervisor,
        "delete_run",
        lambda run_id: captured.append(run_id)
        or {"run_id": run_id, "state": "deleted", "task": "old Fleet"},
    )

    result = CliRunner().invoke(cli.main, ["fleet", "delete", "fleet-1"])

    assert result.exit_code == 0, result.output
    assert captured == ["fleet-1"]
    assert "deleted" in result.output


def test_fleet_cli_inspect_forwards_focus_and_event_limit(monkeypatch):
    from core import fleet_supervisor

    captured = {}

    def fake_inspect(run_id, focus="", *, event_limit=100):
        captured.update(run_id=run_id, focus=focus, event_limit=event_limit)
        return {"run_id": run_id, "focus": focus, "events": []}

    monkeypatch.setattr(fleet_supervisor, "inspect_run", fake_inspect)
    result = CliRunner().invoke(
        cli.main,
        ["fleet", "inspect", "fleet-1", "--focus", "ws-2", "--events", "12"],
    )

    assert result.exit_code == 0, result.output
    assert '"focus": "ws-2"' in result.output
    assert captured == {"run_id": "fleet-1", "focus": "ws-2", "event_limit": 12}


# ---- the supervisor check must not cry wolf on the machine Fleet runs on ----


def test_the_service_check_asks_the_manager_this_machine_actually_has(monkeypatch):
    """The PC has no systemd, so asking systemctl called a running Fleet down.

    `fleet doctor` reported service ok=False on the one machine that actually
    runs Fleet, every single time it was read, because _service_active shelled
    out to systemctl and Windows has none.
    """

    from fleet import supervisor

    monkeypatch.setattr(supervisor.os, "name", "nt")
    monkeypatch.setattr(supervisor.shutil, "which",
                        lambda name: "C:\\powershell.exe" if "powershell" in name else None)
    monkeypatch.setattr(
        supervisor.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="Running\n", stderr=""),
    )

    state = supervisor._service_state()

    assert state["manager"] == "scheduled-task"
    assert state["ok"] is True and state["active"] is True
    assert state["task"] == supervisor.WINDOWS_FLEET_TASK


def test_a_stopped_windows_task_is_reported_as_down(monkeypatch):
    from fleet import supervisor

    monkeypatch.setattr(supervisor.os, "name", "nt")
    monkeypatch.setattr(supervisor.shutil, "which", lambda name: "C:\\powershell.exe")
    monkeypatch.setattr(
        supervisor.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="Ready\n", stderr=""),
    )

    state = supervisor._service_state()

    assert state["ok"] is False and state["active"] is False
    assert state["state"] == "Ready"


def test_a_machine_with_no_service_manager_is_not_reported_as_broken(monkeypatch):
    """Absence of a manager is not a supervisor being down."""

    from fleet import supervisor

    monkeypatch.setattr(supervisor.os, "name", "posix")
    monkeypatch.setattr(supervisor.shutil, "which", lambda _name: None)

    state = supervisor._service_state()

    assert state["ok"] is True
    assert state["active"] is None
    assert state["manager"] == "none"
