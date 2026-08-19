from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from core import fleet_mcp

EXPECTED_TOOLS = {
    "fleet_start",
    "fleet_status",
    "fleet_list",
    "fleet_wait",
    "fleet_cancel",
    "fleet_delete",
    "fleet_retry",
    "fleet_handoff",
    "fleet_inspect",
    "fleet_result",
    "fleet_steer",
}


def _environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENA_FLEET_DB_PATH", str(tmp_path / "fleet.sqlite3"))
    monkeypatch.setenv("SERENA_FLEET_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SERENA_FLEET_NO_AUTOSTART", "1")


def test_mcp_instructions_describe_the_adaptive_persistent_roster() -> None:
    assert "one to four native worker chats" in fleet_mcp.mcp.instructions
    assert "persist across all four phase turns" in fleet_mcp.mcp.instructions


def test_mcp_start_forwards_provider_and_worker_constraints(monkeypatch, tmp_path) -> None:
    captured = {}

    def fake_start(task, **kwargs):
        captured.update({"task": task, **kwargs})
        return {"run_id": "provider-run", "state": "queued"}

    monkeypatch.setattr(fleet_mcp, "start_run", fake_start)
    result = fleet_mcp.fleet_start(
        "tasks:\n- auth\n- settings\n- migration",
        activity="coding",
        provider_mode="codex",
        worker_count=3,
        cwd=str(tmp_path),
        origin_agent="claude",
    )

    assert result["ok"] is True
    assert captured["provider_mode"] == "codex"
    assert captured["worker_count"] == 3
    assert captured["cwd"] == str(tmp_path)


def test_mcp_callables_cover_start_status_list_wait_cancel_retry_and_result(
    tmp_path,
    monkeypatch,
):
    _environment(tmp_path, monkeypatch)
    started = fleet_mcp.fleet_start(
        "research the controlled question",
        activity="research",
        cwd=str(tmp_path),
    )
    assert started["ok"] is True
    run_id = started["run_id"]
    assert fleet_mcp.fleet_status(run_id)["run"]["state"] == "queued"
    assert fleet_mcp.fleet_list()["runs"][0]["run_id"] == run_id
    steered = fleet_mcp.fleet_steer(run_id, "keep this\n\nformat")
    assert steered["ok"] is True

    waited = asyncio.run(fleet_mcp.fleet_wait(run_id, timeout_seconds=0))
    assert waited["ok"] is True
    assert waited["timed_out"] is True
    assert fleet_mcp.fleet_cancel(run_id)["run"]["state"] == "cancelled"
    assert fleet_mcp.fleet_retry(run_id)["run"]["state"] == "queued"
    result = fleet_mcp.fleet_result(run_id)
    assert result == {
        "ok": True,
        "run_id": run_id,
        "state": "queued",
        "result_text": None,
        "error": None,
        "completed_at": None,
    }


def test_mcp_handoff_forwards_the_worker_and_target_provider(monkeypatch) -> None:
    captured = {}

    def fake_handoff(run_id, leg_id, provider):
        captured.update(run_id=run_id, leg_id=leg_id, provider=provider)
        return {"run_id": run_id, "state": "queued"}

    monkeypatch.setattr(fleet_mcp, "handoff_leg", fake_handoff)

    result = fleet_mcp.fleet_handoff("fleet-1", "leg-2", "codex")

    assert result["ok"] is True
    assert captured == {"run_id": "fleet-1", "leg_id": "leg-2", "provider": "codex"}


def test_mcp_delete_is_destructive_and_forwards_the_run(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(
        fleet_mcp,
        "delete_run",
        lambda run_id: captured.append(run_id) or {"run_id": run_id, "state": "deleted"},
    )

    result = fleet_mcp.fleet_delete("fleet-1")

    assert result["ok"] is True
    assert result["run"]["state"] == "deleted"
    assert captured == ["fleet-1"]


def test_mcp_inspect_forwards_focus_and_bounds_events(monkeypatch) -> None:
    captured = {}

    def fake_inspect(run_id, focus="", *, event_limit=100):
        captured.update(run_id=run_id, focus=focus, event_limit=event_limit)
        return {"run_id": run_id, "focus": focus, "events": []}

    monkeypatch.setattr(fleet_mcp, "inspect_run", fake_inspect)
    result = fleet_mcp.fleet_inspect("fleet-1", "ws-2", event_limit=500)

    assert result["ok"] is True
    assert result["inspection"]["focus"] == "ws-2"
    assert captured == {"run_id": "fleet-1", "focus": "ws-2", "event_limit": 100}


def test_mcp_errors_are_structured(tmp_path, monkeypatch):
    _environment(tmp_path, monkeypatch)
    result = fleet_mcp.fleet_status("missing")
    assert result["ok"] is False
    assert result["error_type"] == "KeyError"


def test_mcp_wait_cancels_without_leaving_a_polling_thread(tmp_path, monkeypatch):
    _environment(tmp_path, monkeypatch)
    started = fleet_mcp.fleet_start(
        "research the cancellation path",
        activity="research",
        cwd=str(tmp_path),
    )

    async def cancel_wait() -> None:
        task = asyncio.create_task(
            fleet_mcp.fleet_wait(started["run_id"], timeout_seconds=60)
        )
        await asyncio.sleep(0)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert task.cancelled()

    asyncio.run(cancel_wait())


def test_chats_fleet_mcp_stdio_handshake_lists_all_tools(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["SERENA_FLEET_DB_PATH"] = str(tmp_path / "fleet.sqlite3")
    environment["SERENA_FLEET_NO_AUTOSTART"] = "1"

    async def handshake() -> set[str]:
        parameters = StdioServerParameters(
            command=str(repo / ".venv" / "bin" / "python"),
            args=[str(repo / "cli.py"), "fleet", "mcp"],
            env=environment,
            cwd=repo,
        )
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            return {tool.name for tool in tools.tools}

    assert asyncio.run(handshake()) == EXPECTED_TOOLS
