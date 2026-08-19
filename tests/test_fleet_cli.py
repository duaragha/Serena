from __future__ import annotations

import json

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
