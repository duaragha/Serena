"""Bounded, read-only provider-capacity checks for Serena Fleet.

Capacity is deliberately separate from authentication and runtime health.  A
provider can be installed and logged in while its subscription window is
exhausted.  Unknown or stale observations never disable a provider; the worker
itself remains the final authority.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.billing import strip_metered_auth_env
from core.codex_usage_reader import CodexUsageReader
from core.config import DATA_DIR

PROVIDERS = ("codex", "claude")
STATUSES = frozenset({"available", "unavailable", "unknown"})
DEFAULT_CLAUDE_FRESH_SECONDS = 30.0
DEFAULT_CODEX_FRESH_SECONDS = 120.0
DEFAULT_CODEX_RPC_TIMEOUT_SECONDS = 4.0


@dataclass(frozen=True, slots=True)
class ProviderCapacity:
    """One provider's current capacity decision.

    ``usable`` is false only for a positive, unexpired exhaustion signal.
    ``unknown`` therefore remains usable by design.
    """

    provider: str
    status: str
    usable: bool
    source: str
    reason: str
    observed_at: float | None = None
    used_percent: float | None = None
    resets_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "usable": self.usable,
            "source": self.source,
            "reason": self.reason,
            "observed_at": self.observed_at,
            "used_percent": self.used_percent,
            "resets_at": self.resets_at,
        }


def read_fleet_capacity(
    *,
    now: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, ProviderCapacity]:
    """Return current capacity states for both native Fleet providers.

    Tests and emergency operations can supply a complete deterministic snapshot
    through ``SERENA_FLEET_CAPACITY_JSON``.  Otherwise Claude is read from the
    statusline materialization and Codex is queried through its local app-server
    account endpoint, with bounded rollout telemetry as a fallback.
    """

    current = time.time() if now is None else float(now)
    env = dict(os.environ if environ is None else environ)
    override = str(env.get("SERENA_FLEET_CAPACITY_JSON") or "").strip()
    if override:
        return _override_capacity(override)

    return {
        "codex": _read_codex_capacity(current, env),
        "claude": _read_claude_capacity(current, env),
    }


def _override_capacity(value: str) -> dict[str, ProviderCapacity]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("SERENA_FLEET_CAPACITY_JSON must be valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("SERENA_FLEET_CAPACITY_JSON must be an object")

    result: dict[str, ProviderCapacity] = {}
    for provider in PROVIDERS:
        item = decoded.get(provider)
        if not isinstance(item, dict):
            raise ValueError(f"SERENA_FLEET_CAPACITY_JSON requires a {provider} object")
        raw_status = str(item.get("status") or "").strip().lower()
        if not raw_status and "usable" in item:
            raw_status = "available" if bool(item.get("usable")) else "unavailable"
        if raw_status not in STATUSES:
            raise ValueError(
                f"SERENA_FLEET_CAPACITY_JSON {provider}.status must be "
                "available, unavailable, or unknown"
            )
        result[provider] = ProviderCapacity(
            provider=provider,
            status=raw_status,
            usable=raw_status != "unavailable",
            source=str(item.get("source") or "environment-override"),
            reason=str(item.get("reason") or f"{provider} capacity override"),
            observed_at=_number(item.get("observed_at")),
            used_percent=_number(item.get("used_percent", item.get("used_percentage"))),
            resets_at=_number(item.get("resets_at")),
        )
    return result


def _read_claude_capacity(
    now: float,
    environ: Mapping[str, str],
) -> ProviderCapacity:
    configured = str(environ.get("SERENA_FLEET_LIVE_USAGE_PATH") or "").strip()
    path = Path(configured).expanduser() if configured else DATA_DIR / "live-usage.json"
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _unknown("claude", "claude-statusline", "Claude usage is unavailable")
    service = decoded.get("claude") if isinstance(decoded, dict) else None
    if not isinstance(service, dict):
        return _unknown("claude", "claude-statusline", "Claude usage is unavailable")

    windows = _usage_windows(service)
    observed_at = _latest_observation(service, decoded, windows)
    freshness = _positive_setting(
        environ,
        "SERENA_FLEET_CLAUDE_FRESH_SECONDS",
        DEFAULT_CLAUDE_FRESH_SECONDS,
    )
    if observed_at is None or now - observed_at > freshness:
        return _unknown(
            "claude",
            "claude-statusline",
            "Claude usage is stale",
            observed_at=observed_at,
        )
    return _capacity_from_windows(
        "claude",
        "claude-statusline",
        windows,
        now=now,
        observed_at=observed_at,
    )


def _read_codex_capacity(
    now: float,
    environ: Mapping[str, str],
) -> ProviderCapacity:
    limits = _read_codex_app_server(environ)
    if isinstance(limits, dict):
        reached_type = limits.get("rateLimitReachedType")
        spend_reached = limits.get("spendControlReached") is True
        windows = _codex_rpc_windows(limits)
        if reached_type or spend_reached:
            used, reset = _highest_window(windows)
            reason = (
                f"Codex reported {reached_type}"
                if reached_type
                else "Codex spend control is reached"
            )
            return ProviderCapacity(
                provider="codex",
                status="unavailable",
                usable=False,
                source="codex-app-server",
                reason=reason,
                observed_at=now,
                used_percent=used,
                resets_at=reset,
            )
        return _capacity_from_windows(
            "codex",
            "codex-app-server",
            windows,
            now=now,
            observed_at=now,
        )

    configured = str(environ.get("SERENA_FLEET_CODEX_SESSIONS_DIR") or "").strip()
    sessions = Path(configured).expanduser() if configured else Path.home() / ".codex/sessions"
    try:
        service = CodexUsageReader().read(sessions, model="codex", now=now)
    except Exception:
        service = {}
    if not service.get("available"):
        return _unknown("codex", "codex-jsonl", "Codex usage is unavailable")
    windows = _usage_windows(service)
    observed_at = _latest_observation(service, service, windows)
    freshness = _positive_setting(
        environ,
        "SERENA_FLEET_CODEX_FRESH_SECONDS",
        DEFAULT_CODEX_FRESH_SECONDS,
    )
    if observed_at is None or now - observed_at > freshness:
        return _unknown(
            "codex",
            "codex-jsonl",
            "Codex usage is stale",
            observed_at=observed_at,
        )
    return _capacity_from_windows(
        "codex",
        "codex-jsonl",
        windows,
        now=now,
        observed_at=observed_at,
    )


def _read_codex_app_server(environ: Mapping[str, str]) -> dict[str, Any] | None:
    """Read effective Codex rate limits without starting a model turn."""

    configured = str(environ.get("SERENA_FLEET_CODEX_BIN") or "").strip()
    binary = str(Path(configured).expanduser()) if configured else shutil.which("codex")
    if not binary:
        return None
    timeout = _positive_setting(
        environ,
        "SERENA_FLEET_CAPACITY_RPC_TIMEOUT_SECONDS",
        DEFAULT_CODEX_RPC_TIMEOUT_SECONDS,
    )
    timeout = min(10.0, max(0.5, timeout))
    command = [binary, "app-server", "--listen", "stdio://"]
    environment = strip_metered_auth_env(dict(environ))
    process: subprocess.Popen[str] | None = None
    reader: threading.Thread | None = None
    responses: queue.Queue[str | None] = queue.Queue()
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=environment,
            start_new_session=os.name != "nt",
        )
        assert process.stdin is not None and process.stdout is not None

        def drain() -> None:
            try:
                for line in iter(process.stdout.readline, ""):
                    responses.put(line)
            finally:
                responses.put(None)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        messages = (
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "serena-fleet-capacity", "version": "1"}},
            },
            {"method": "initialized", "params": {}},
            {"id": 2, "method": "account/rateLimits/read", "params": {}},
        )
        for message in messages:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = responses.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                break
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(response, dict) or response.get("id") != 2:
                continue
            result = response.get("result")
            if not isinstance(result, dict):
                return None
            limits = result.get("rateLimits")
            if not isinstance(limits, dict):
                by_id = result.get("rateLimitsByLimitId")
                limits = by_id.get("codex") if isinstance(by_id, dict) else None
            return limits if isinstance(limits, dict) else None
    except (OSError, BrokenPipeError, ValueError):
        return None
    finally:
        if process is not None:
            if process.stdin is not None:
                with suppress(OSError, BrokenPipeError):
                    process.stdin.close()
            if process.poll() is None:
                with suppress(OSError):
                    process.terminate()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)
            if process.poll() is None:
                with suppress(OSError):
                    process.kill()
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=1)
            if process.stdout is not None:
                with suppress(OSError):
                    process.stdout.close()
        if reader is not None:
            reader.join(timeout=0.2)
    return None


def _codex_rpc_windows(limits: Mapping[str, Any]) -> list[dict[str, float | None]]:
    windows: list[dict[str, float | None]] = []
    for name in ("primary", "secondary", "individualLimit"):
        value = limits.get(name)
        if not isinstance(value, dict):
            continue
        windows.append(
            {
                "used_percent": _number(value.get("usedPercent", value.get("used_percentage"))),
                "resets_at": _number(value.get("resetsAt", value.get("resets_at"))),
                "observed_at": None,
            }
        )
    return windows


def _usage_windows(service: Mapping[str, Any]) -> list[dict[str, float | None]]:
    windows: list[dict[str, float | None]] = []
    for name in ("five_hour", "seven_day", "primary", "secondary"):
        value = service.get(name)
        if not isinstance(value, dict):
            continue
        windows.append(
            {
                "used_percent": _number(value.get("used_percentage", value.get("used_percent"))),
                "resets_at": _number(value.get("resets_at", value.get("resetsAt"))),
                "observed_at": _number(value.get("observed_at")),
            }
        )
    return windows


def _latest_observation(
    service: Mapping[str, Any],
    root: Mapping[str, Any],
    windows: list[dict[str, float | None]],
) -> float | None:
    window_updates = [
        window.get("observed_at") for window in windows if window.get("observed_at") is not None
    ]
    if window_updates:
        return max(window_updates)
    return _number(service.get("updated_at")) or _number(root.get("updated_at"))


def _capacity_from_windows(
    provider: str,
    source: str,
    windows: list[dict[str, float | None]],
    *,
    now: float,
    observed_at: float,
) -> ProviderCapacity:
    measured = [window for window in windows if window.get("used_percent") is not None]
    if not measured:
        return _unknown(
            provider,
            source,
            f"{provider.title()} usage has no measured window",
            observed_at=observed_at,
        )
    exhausted = [
        window
        for window in measured
        if float(window["used_percent"]) >= 100.0
        and window.get("resets_at") is not None
        and float(window["resets_at"]) > now
    ]
    if exhausted:
        window = max(exhausted, key=lambda item: float(item["used_percent"] or 0.0))
        return ProviderCapacity(
            provider=provider,
            status="unavailable",
            usable=False,
            source=source,
            reason=f"{provider.title()} usage is exhausted until reset",
            observed_at=observed_at,
            used_percent=float(window["used_percent"]),
            resets_at=float(window["resets_at"]),
        )
    unresolved = [
        window
        for window in measured
        if float(window["used_percent"]) >= 100.0 and window.get("resets_at") is None
    ]
    used, reset = _highest_window(measured)
    if unresolved:
        return _unknown(
            provider,
            source,
            f"{provider.title()} usage reached 100% without a reset time",
            observed_at=observed_at,
            used_percent=used,
        )
    return ProviderCapacity(
        provider=provider,
        status="available",
        usable=True,
        source=source,
        reason=f"{provider.title()} usage is below its active limit",
        observed_at=observed_at,
        used_percent=used,
        resets_at=reset,
    )


def _highest_window(
    windows: list[dict[str, float | None]],
) -> tuple[float | None, float | None]:
    measured = [window for window in windows if window.get("used_percent") is not None]
    if not measured:
        return None, None
    window = max(measured, key=lambda item: float(item["used_percent"] or 0.0))
    return _number(window.get("used_percent")), _number(window.get("resets_at"))


def _unknown(
    provider: str,
    source: str,
    reason: str,
    *,
    observed_at: float | None = None,
    used_percent: float | None = None,
) -> ProviderCapacity:
    return ProviderCapacity(
        provider=provider,
        status="unknown",
        usable=True,
        source=source,
        reason=reason,
        observed_at=observed_at,
        used_percent=used_percent,
    )


def _positive_setting(
    environ: Mapping[str, str],
    name: str,
    default: float,
) -> float:
    value = _number(environ.get(name))
    return float(value) if value is not None and value > 0 else default


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number != number or number in {float("inf"), float("-inf")}:
        return None
    return number
