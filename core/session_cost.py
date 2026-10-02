"""What one chat has cost so far, for the cost chips in the terminal pane bar.

Claude reports its own bill: Claude Code puts ``cost.total_cost_usd`` in the
status line payload, so the status line drops it into ``session-costs/<sid>.json``
on every refresh (``record_claude_cost`` here, a jq one-liner in the bash
status line) and this module only reads it back.

Codex reports no dollars, only tokens. Each ``token_count`` event in a rollout
carries ``last_token_usage`` for one request, so the cost is rebuilt request by
request at the model's published rates. That matters for the long-context
tier, which OpenAI bills per request above 272K input tokens. Rollouts run past
100 MB, so each file is read incrementally from where the last call stopped.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from core.config import DATA_DIR

COST_DIR = Path(DATA_DIR) / "session-costs"

# USD per 1M tokens: (input, cached input, output, long input, long cached, long output).
# Long-context rates apply to a request whose input exceeds LONG_CONTEXT_INPUT.
# Source: https://developers.openai.com/api/docs/pricing, read 2026-10-02.
LONG_CONTEXT_INPUT = 272_000
CODEX_RATES: dict[str, tuple[float, float, float, float, float, float]] = {
    "gpt-6.1-sol": (2.00, 0.10, 10.00, 4.00, 0.20, 15.00),
    "gpt-6-astra": (10.00, 1.00, 50.00, 20.00, 2.00, 75.00),
    "gpt-6-sol": (2.00, 0.20, 10.00, 4.00, 0.40, 15.00),
    "gpt-6-luna": (0.10, 0.01, 0.50, 0.20, 0.02, 0.75),
    "gpt-5.6-sol": (4.00, 0.40, 20.00, 8.00, 0.80, 30.00),
    "gpt-5.6-terra": (2.00, 0.20, 12.00, 4.00, 0.40, 18.00),
    "gpt-5.6-luna": (0.20, 0.02, 1.20, 0.40, 0.04, 1.80),
    "gpt-5.5": (5.00, 0.50, 30.00, 10.00, 1.00, 45.00),
}

# Per call, so a 100 MB rollout catches up over a few polls instead of one long one.
_MAX_READ_BYTES = 48 * 1024 * 1024
_SAFE_SID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

_lock = threading.Lock()
_codex_state: dict[str, dict[str, Any]] = {}
_path_cache: dict[str, str] = {}


# ── Claude ────────────────────────────────────────────────────

def record_claude_cost(payload: Any, cost_dir: Path | str | None = None) -> bool:
    """Persist the cost from one status line render. Never raises."""
    try:
        sid = str(payload.get("session_id") or "")
        cost = (payload.get("cost") or {}).get("total_cost_usd")
        if not _SAFE_SID.match(sid) or not isinstance(cost, (int, float)):
            return False
        directory = Path(cost_dir) if cost_dir else COST_DIR
        directory.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"session_id": sid, "cost_usd": float(cost), "observed_at": int(time.time())})
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{sid}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, directory / f"{sid}.json")
        return True
    except Exception:
        return False


def claude_cost(sid: str, cost_dir: Path | str | None = None) -> float | None:
    if not _SAFE_SID.match(sid):
        return None
    try:
        data = json.loads((Path(cost_dir) if cost_dir else COST_DIR).joinpath(f"{sid}.json").read_text("utf-8"))
        return float(data["cost_usd"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ── Codex ─────────────────────────────────────────────────────

def _rates_for(model: str | None):
    if not model:
        return None
    if model in CODEX_RATES:
        return CODEX_RATES[model]
    # "gpt-6.1-sol-fast" and similar suffixed slugs price like their base model.
    for slug in sorted(CODEX_RATES, key=len, reverse=True):
        if model.startswith(slug):
            return CODEX_RATES[slug]
    return None


def request_cost_usd(model: str | None, usage: dict) -> float | None:
    """Cost of one request from a Codex ``last_token_usage`` block."""
    rates = _rates_for(model)
    if rates is None:
        return None
    total_in = int(usage.get("input_tokens") or 0)
    cached = min(int(usage.get("cached_input_tokens") or 0), total_in)
    out = int(usage.get("output_tokens") or 0)  # reasoning tokens are already inside it
    r_in, r_cached, r_out = rates[3:6] if total_in > LONG_CONTEXT_INPUT else rates[0:3]
    return ((total_in - cached) * r_in + cached * r_cached + out * r_out) / 1_000_000.0


def _advance(state: dict[str, Any], path: Path) -> None:
    size = path.stat().st_size
    if size < state["offset"]:
        state.update(offset=0, cost=0.0, last_total=-1, model=None, unpriced=0)
    if size == state["offset"]:
        return
    with path.open("rb") as fh:
        fh.seek(state["offset"])
        chunk = fh.read(min(size - state["offset"], _MAX_READ_BYTES))
    end = chunk.rfind(b"\n")
    if end < 0:
        return  # a half-written line; the rest arrives on the next poll
    state["offset"] += end + 1
    for line in chunk[: end + 1].splitlines():
        if b'"token_count"' not in line and b'"turn_context"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        payload = obj.get("payload") or {}
        if obj.get("type") == "turn_context":
            state["model"] = payload.get("model") or state["model"]
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        total = (info.get("total_token_usage") or {}).get("total_tokens")
        last = info.get("last_token_usage")
        # Codex repeats the same snapshot on rate-limit-only updates; only a
        # grown running total is a new request.
        if not isinstance(total, int) or not isinstance(last, dict) or total <= state["last_total"]:
            continue
        state["last_total"] = total
        cost = request_cost_usd(state["model"], last)
        if cost is None:
            state["unpriced"] += 1
        else:
            state["cost"] += cost


def codex_cost(path: Path | str) -> tuple[float, bool] | None:
    """(running cost in USD, every request priced) for one rollout."""
    path = Path(path)
    with _lock:
        state = _codex_state.setdefault(
            str(path), {"offset": 0, "cost": 0.0, "last_total": -1, "model": None, "unpriced": 0}
        )
        try:
            _advance(state, path)
        except OSError:
            return None
        return state["cost"], state["unpriced"] == 0


def _codex_path(sid: str) -> str | None:
    cached = _path_cache.get(sid)
    if cached and os.path.exists(cached):
        return cached
    try:
        from core.indexer import get_session

        row = get_session(sid)
    except Exception:
        return None
    path = (row or {}).get("file_path")
    if path and os.path.exists(path):
        _path_cache[sid] = path
        return path
    return None


# ── Both ──────────────────────────────────────────────────────

def session_costs(sids: list[str], agents: dict[str, str] | None = None) -> dict[str, dict]:
    """``{sid: {agent, cost_usd, estimated}}``; ``cost_usd`` is None when unknown yet."""
    result: dict[str, dict] = {}
    for sid in dict.fromkeys(sids):
        agent = ((agents or {}).get(sid) or "").lower()
        if not agent:
            try:
                from core.indexer import get_session

                agent = str((get_session(sid) or {}).get("agent") or "").lower()
            except Exception:
                agent = ""
        if agent == "claude":
            result[sid] = {"agent": agent, "cost_usd": claude_cost(sid), "estimated": False}
        elif agent == "codex":
            path = _codex_path(sid)
            got = codex_cost(path) if path else None
            result[sid] = {
                "agent": agent,
                "cost_usd": got[0] if got else None,
                "estimated": True,
                "complete": bool(got and got[1]),
            }
        else:
            result[sid] = {"agent": agent, "cost_usd": None, "estimated": False}
    return result
