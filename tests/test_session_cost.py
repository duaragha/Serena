"""Cost chips: Claude's bill is read back from the status line tap, Codex's is
rebuilt request by request from the rollout at OpenAI's published rates."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from core import session_cost as sc

REPO = Path(__file__).resolve().parents[1]
SID = "bc32e6ca-16ed-49d3-aff2-727442cb3a2e"


def test_claude_cost_round_trips_through_the_status_line_tap(tmp_path):
    payload = {"session_id": SID, "cost": {"total_cost_usd": 4.2871}}
    assert sc.record_claude_cost(payload, tmp_path)
    assert sc.claude_cost(SID, tmp_path) == pytest.approx(4.2871)
    sc.record_claude_cost({"session_id": SID, "cost": {"total_cost_usd": 5.0}}, tmp_path)
    assert sc.claude_cost(SID, tmp_path) == 5.0
    assert not list(tmp_path.glob(".*.tmp")), "the atomic write left a temp file behind"


@pytest.mark.parametrize("payload", [
    {"cost": {"total_cost_usd": 1}},
    {"session_id": "../etc/passwd", "cost": {"total_cost_usd": 1}},
    {"session_id": SID, "cost": {}},
    {"session_id": SID},
])
def test_unusable_payloads_write_nothing(tmp_path, payload):
    assert not sc.record_claude_cost(payload, tmp_path)
    assert not list(tmp_path.iterdir())


def test_unknown_claude_session_has_no_cost(tmp_path):
    assert sc.claude_cost(SID, tmp_path) is None
    assert sc.claude_cost("../x", tmp_path) is None


def test_request_cost_uses_cached_rate_and_reasoning_stays_inside_output():
    usage = {"input_tokens": 250_000, "cached_input_tokens": 100_000,
             "output_tokens": 100_000, "reasoning_output_tokens": 60_000}
    # 150k fresh * $2 + 100k cached * $0.10 + 100k out * $10, per million
    assert sc.request_cost_usd("gpt-6.1-sol", usage) == pytest.approx(0.3 + 0.01 + 1.0)


def test_request_over_272k_input_bills_the_long_context_tier():
    usage = {"input_tokens": 300_000, "cached_input_tokens": 100_000, "output_tokens": 1000}
    assert sc.request_cost_usd("gpt-6.1-sol", usage) == pytest.approx(
        (200_000 * 4.0 + 100_000 * 0.20 + 1000 * 15.0) / 1e6)


def test_unknown_model_is_unpriced_not_zero():
    assert sc.request_cost_usd("gpt-9-mystery", {"input_tokens": 5}) is None
    assert sc.request_cost_usd(None, {"input_tokens": 5}) is None


def _rollout(path: Path, events: list[dict]) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def _tc(total, last):
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {"total_tokens": total}, "last_token_usage": last}}}


def test_codex_cost_sums_requests_skips_repeats_and_follows_appends(tmp_path):
    f = tmp_path / "rollout.jsonl"
    one = {"input_tokens": 200_000, "cached_input_tokens": 0, "output_tokens": 0}
    two = {"input_tokens": 100_000, "cached_input_tokens": 0, "output_tokens": 0}
    _rollout(f, [
        {"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}},
        _tc(200_000, one),
        _tc(200_000, one),                        # same snapshot repeated: not a new request
        {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
    ])
    assert sc.codex_cost(f) == (pytest.approx(0.4), True)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_tc(300_000, two)) + "\n")
    assert sc.codex_cost(f) == (pytest.approx(0.6), True)


def test_codex_cost_flags_unpriced_models(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _rollout(f, [{"type": "turn_context", "payload": {"model": "gpt-9-mystery"}},
                 _tc(10, {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 0})])
    assert sc.codex_cost(f) == (0.0, False)


def test_codex_cost_waits_for_a_half_written_line(tmp_path):
    f = tmp_path / "rollout.jsonl"
    line = json.dumps(_tc(200_000, {"input_tokens": 200_000, "cached_input_tokens": 0,
                                    "output_tokens": 0}))
    f.write_text(json.dumps({"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}}) + "\n"
                 + line[:30], encoding="utf-8")
    assert sc.codex_cost(f)[0] == 0.0
    f.write_text(f.read_text() + line[30:] + "\n", encoding="utf-8")
    assert sc.codex_cost(f)[0] == pytest.approx(0.4)


def test_python_status_line_records_the_session_cost(tmp_path):
    payload = {"session_id": SID, "cwd": "/x/y", "cost": {"total_cost_usd": 4.2871, "total_duration_ms": 1},
               "context_window": {"used_percentage": 1, "context_window_size": 1000}}
    # The tap looks for the repo under $HOME, so give it a home that points at this checkout.
    home = tmp_path / "home"
    (home / "Documents" / "Projects").mkdir(parents=True)
    (home / "Documents" / "Projects" / "serena").symlink_to(REPO)
    env = dict(os.environ, CHATS_DATA_DIR=str(tmp_path), HOME=str(home), USERPROFILE=str(home))
    done = subprocess.run(["python3", str(REPO / "scripts" / "claude-statusline.py")],
                          input=json.dumps(payload), capture_output=True, text=True, timeout=60, env=env,
                          cwd=REPO)
    assert done.returncode == 0, done.stderr
    assert sc.claude_cost(SID, tmp_path / "session-costs") == pytest.approx(4.2871)


def test_bash_status_line_records_the_session_cost(tmp_path):
    script = Path.home() / ".claude" / "statusline.sh"
    if not script.exists():
        pytest.skip("bash status line is installed per-machine")
    payload = {"session_id": SID, "cwd": "/x/y", "cost": {"total_cost_usd": 4.2871, "total_duration_ms": 1},
               "context_window": {"used_percentage": 1, "context_window_size": 1000}}
    done = subprocess.run(["bash", str(script)], input=json.dumps(payload), capture_output=True, text=True,
                          timeout=90, env=dict(os.environ, XDG_DATA_HOME=str(tmp_path)))
    assert done.returncode == 0, done.stderr
    assert sc.claude_cost(SID, tmp_path / "chats" / "session-costs") == pytest.approx(4.2871)


def test_session_costs_route_returns_both_agents(tmp_path, monkeypatch):
    from ui import web

    monkeypatch.setattr(sc, "COST_DIR", tmp_path)
    sc.record_claude_cost({"session_id": SID, "cost": {"total_cost_usd": 2.5}}, tmp_path)
    monkeypatch.setattr(sc, "claude_cost", lambda sid, cost_dir=None: 2.5)
    rollout = tmp_path / "rollout.jsonl"
    _rollout(rollout, [{"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}},
                       _tc(200_000, {"input_tokens": 200_000, "cached_input_tokens": 0, "output_tokens": 0})])
    monkeypatch.setattr(sc, "_codex_path", lambda sid: str(rollout))
    monkeypatch.setattr("core.indexer.get_session", lambda sid: {"agent": "claude" if sid == SID else "codex"})
    body = web.app.test_client().get(f"/api/session-costs?sids={SID},01a0f38a").get_json()
    assert body[SID] == {"agent": "claude", "cost_usd": 2.5, "estimated": False}
    assert body["01a0f38a"]["agent"] == "codex" and body["01a0f38a"]["cost_usd"] == pytest.approx(0.4)
