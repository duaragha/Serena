"""Shared MCP surface for starting and controlling Serena Fleet runs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from fleet.store import TERMINAL_RUN_STATES
from fleet.supervisor import (
    delete_run,
    get_result,
    get_run,
    handoff_leg,
    inspect_run,
    list_runs,
    retry_run,
    start_run,
    steer_run,
    stop_run,
)

WAIT_POLL_SECONDS = 0.5

mcp = FastMCP(
    "serena-fleet",
    instructions=(
        "Run direct Claude and Codex workers through Serena's durable four-phase "
        "Fleet. Fleet selects one to four native worker chats from the requested "
        "providers and task workstreams. Those chats persist across all four phase turns. "
        "A capacity handoff preserves the logical worker and continues on the other provider."
    ),
)


def _ok_run(run: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "run_id": run["run_id"], "run": run}


def _error(exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "error": str(exc),
        "error_type": type(exc).__name__,
    }


def _call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        value = function(*args, **kwargs)
    except Exception as exc:
        return _error(exc)
    if isinstance(value, dict) and "run_id" in value:
        return _ok_run(value)
    return {"ok": True, "value": value}


@mcp.tool(
    name="fleet_start",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def fleet_start(
    task: str,
    activity: str = "auto",
    provider_mode: str = "auto",
    worker_count: int | None = None,
    cwd: str = "",
    origin_session_id: str = "",
    origin_agent: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Start a 1-4 worker Fleet run, or return its exact plan when dry_run is true."""

    return _call(
        start_run,
        task,
        activity=activity,
        provider_mode=provider_mode,
        worker_count=worker_count,
        cwd=cwd or None,
        origin_session_id=origin_session_id or None,
        origin_agent=origin_agent or None,
        dry_run=dry_run,
    )


@mcp.tool(
    name="fleet_status",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def fleet_status(run_id: str) -> dict[str, Any]:
    """Return the current four-phase state and worker attempts for one run."""

    try:
        run = get_run(run_id)
        if run is None:
            raise KeyError(f"unknown Fleet run {run_id}")
        return _ok_run(run)
    except Exception as exc:
        return _error(exc)


@mcp.tool(
    name="fleet_list",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def fleet_list(limit: int = 20) -> dict[str, Any]:
    """List recent Fleet runs using lightweight summaries."""

    try:
        runs = list_runs(limit)
        return {"ok": True, "runs": runs, "count": len(runs)}
    except Exception as exc:
        return _error(exc)


@mcp.tool(
    name="fleet_wait",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def fleet_wait(run_id: str, timeout_seconds: float = 1800) -> dict[str, Any]:
    """Wait without blocking the MCP server until a run finishes or times out."""

    try:
        timeout = min(86_400.0, max(0.0, float(timeout_seconds)))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            run = get_run(run_id)
            if run is None:
                raise KeyError(f"unknown Fleet run {run_id}")
            if run["state"] in TERMINAL_RUN_STATES:
                result = _ok_run(run)
                result["timed_out"] = False
                return result
            remaining = deadline - loop.time()
            if remaining <= 0:
                result = _ok_run(run)
                result["timed_out"] = True
                return result
            await asyncio.sleep(min(WAIT_POLL_SECONDS, remaining))
    except Exception as exc:
        return _error(exc)


@mcp.tool(
    name="fleet_cancel",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def fleet_cancel(run_id: str, force: bool = False) -> dict[str, Any]:
    """Request cancellation; active worker process groups are stopped cooperatively.

    Set force only for a run whose supervisor is alive but wedged and therefore
    cannot converge the cancel itself. It refuses while any worker process is
    alive or the run has changed in the last ten minutes.
    """

    return _call(stop_run, run_id, force=force)


@mcp.tool(
    name="fleet_delete",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def fleet_delete(run_id: str) -> dict[str, Any]:
    """Delete a terminal Fleet run, its worker chats, and private runtime state."""

    return _call(delete_run, run_id)


@mcp.tool(
    name="fleet_retry",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def fleet_retry(run_id: str) -> dict[str, Any]:
    """Retry only unfinished legs, resuming their native provider sessions when possible."""

    return _call(retry_run, run_id)


@mcp.tool(
    name="fleet_handoff",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def fleet_handoff(run_id: str, leg_id: str, provider: str) -> dict[str, Any]:
    """Move one unfinished worker to Claude or Codex and preserve its Fleet lineage."""

    return _call(handoff_leg, run_id, leg_id, provider)


@mcp.tool(
    name="fleet_inspect",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def fleet_inspect(run_id: str, focus: str = "", event_limit: int = 100) -> dict[str, Any]:
    """Inspect one run's DAG unit or worker, context receipt, and bounded event history."""

    try:
        return {
            "ok": True,
            "inspection": inspect_run(
                run_id,
                focus,
                event_limit=min(100, max(1, int(event_limit))),
            ),
        }
    except Exception as exc:
        return _error(exc)


@mcp.tool(
    name="fleet_result",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def fleet_result(run_id: str) -> dict[str, Any]:
    """Fetch the full final output explicitly, avoiding heavy status polling."""

    try:
        result = get_result(run_id)
        return {"ok": True, **result}
    except Exception as exc:
        return _error(exc)


@mcp.tool(
    name="fleet_steer",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def fleet_steer(run_id: str, message: str) -> dict[str, Any]:
    """Add bounded context for future legs; this never interrupts an active model turn."""

    return _call(steer_run, run_id, message)


def run_mcp_server() -> None:
    """Run the stdio server. Kept as a named entrypoint for ``chats fleet mcp``."""

    mcp.run(transport="stdio")


def main() -> None:
    run_mcp_server()


if __name__ == "__main__":
    main()
