from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import fleet_capacity

NOW = 2_000_000_000.0


def _write_usage(path: Path, claude: dict) -> None:
    path.write_text(json.dumps({"updated_at": NOW, "claude": claude}), encoding="utf-8")


def _read(
    monkeypatch,
    usage_path: Path,
    *,
    codex_limits: dict | None,
    extra: dict[str, str] | None = None,
):
    monkeypatch.setattr(
        fleet_capacity,
        "_read_codex_app_server",
        lambda _environ: codex_limits,
    )
    environ = {"SERENA_FLEET_LIVE_USAGE_PATH": str(usage_path)}
    environ.update(extra or {})
    return fleet_capacity.read_fleet_capacity(now=NOW, environ=environ)


def test_fresh_claude_exhaustion_and_codex_headroom_are_detected(tmp_path, monkeypatch):
    usage = tmp_path / "live-usage.json"
    _write_usage(
        usage,
        {
            "available": True,
            "updated_at": NOW - 5,
            "five_hour": {
                "used_percentage": 111,
                "resets_at": NOW + 900,
                "observed_at": NOW - 5,
            },
            "seven_day": {
                "used_percentage": 40,
                "resets_at": NOW + 86_400,
                "observed_at": NOW - 5,
            },
        },
    )

    states = _read(
        monkeypatch,
        usage,
        codex_limits={
            "primary": {"usedPercent": 79, "resetsAt": NOW + 86_400},
            "credits": {"hasCredits": False, "balance": "0"},
            "rateLimitReachedType": None,
            "spendControlReached": False,
        },
    )

    assert set(states) == {"codex", "claude"}
    assert states["claude"].status == "unavailable"
    assert states["claude"].usable is False
    assert states["claude"].used_percent == 111
    assert states["codex"].status == "available"
    assert states["codex"].usable is True
    assert states["codex"].used_percent == 79


def test_stale_or_expired_claude_observation_never_blocks(tmp_path, monkeypatch):
    usage = tmp_path / "live-usage.json"
    _write_usage(
        usage,
        {
            "updated_at": NOW - 31,
            "five_hour": {
                "used_percentage": 105,
                "resets_at": NOW + 900,
                "observed_at": NOW - 31,
            },
        },
    )
    stale = _read(monkeypatch, usage, codex_limits=None)
    assert stale["claude"].status == "unknown"
    assert stale["claude"].usable is True

    _write_usage(
        usage,
        {
            "updated_at": NOW - 1,
            "five_hour": {
                "used_percentage": 105,
                "resets_at": NOW - 1,
                "observed_at": NOW - 1,
            },
        },
    )
    expired = _read(monkeypatch, usage, codex_limits=None)
    assert expired["claude"].status == "available"
    assert expired["claude"].usable is True


@pytest.mark.parametrize(
    ("limits", "reason_fragment"),
    [
        (
            {
                "primary": {"usedPercent": 20, "resetsAt": NOW + 900},
                "rateLimitReachedType": "secondary_window",
            },
            "secondary_window",
        ),
        (
            {
                "primary": {"usedPercent": 20, "resetsAt": NOW + 900},
                "spendControlReached": True,
            },
            "spend control",
        ),
        (
            {
                "primary": {"usedPercent": 100, "resetsAt": NOW + 900},
                "rateLimitReachedType": None,
            },
            "exhausted",
        ),
    ],
)
def test_codex_definitive_limit_signals_block(
    tmp_path,
    monkeypatch,
    limits,
    reason_fragment,
):
    states = _read(monkeypatch, tmp_path / "missing.json", codex_limits=limits)
    codex = states["codex"]
    assert codex.status == "unavailable"
    assert codex.usable is False
    assert reason_fragment in codex.reason


def test_codex_expired_limit_and_missing_credit_balance_do_not_block(tmp_path, monkeypatch):
    states = _read(
        monkeypatch,
        tmp_path / "missing.json",
        codex_limits={
            "primary": {"usedPercent": 100, "resetsAt": NOW - 1},
            "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            "rateLimitReachedType": None,
            "spendControlReached": False,
        },
    )
    assert states["codex"].status == "available"
    assert states["codex"].usable is True


def test_codex_jsonl_fallback_respects_freshness_and_reset(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    rollout = sessions / "2026/01/01/rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {
                "timestamp": "2033-05-18T03:33:15Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "limit_id": "codex",
                        "primary": {
                            "used_percent": 100,
                            "window_minutes": 10080,
                            "resets_at": NOW + 900,
                        },
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fleet_capacity, "_read_codex_app_server", lambda _environ: None)

    states = fleet_capacity.read_fleet_capacity(
        now=NOW,
        environ={
            "SERENA_FLEET_LIVE_USAGE_PATH": str(tmp_path / "missing.json"),
            "SERENA_FLEET_CODEX_SESSIONS_DIR": str(sessions),
            "SERENA_FLEET_CODEX_FRESH_SECONDS": "120",
        },
    )

    assert states["codex"].source == "codex-jsonl"
    assert states["codex"].status == "unavailable"
    assert states["codex"].resets_at == NOW + 900


def test_complete_json_override_is_deterministic_and_truthful():
    override = json.dumps(
        {
            "codex": {
                "status": "unknown",
                "reason": "probe timed out",
                "usable": False,
            },
            "claude": {
                "status": "unavailable",
                "reason": "session limit",
                "used_percent": 100,
                "resets_at": NOW + 60,
            },
        }
    )

    states = fleet_capacity.read_fleet_capacity(
        now=NOW,
        environ={"SERENA_FLEET_CAPACITY_JSON": override},
    )

    assert states["codex"].status == "unknown"
    assert states["codex"].usable is True
    assert states["claude"].status == "unavailable"
    assert states["claude"].usable is False
    assert states["claude"].to_dict()["resets_at"] == NOW + 60


def test_invalid_override_fails_closed_as_configuration_error():
    with pytest.raises(ValueError, match="requires a claude object"):
        fleet_capacity.read_fleet_capacity(
            environ={"SERENA_FLEET_CAPACITY_JSON": json.dumps({"codex": {"status": "available"}})}
        )
