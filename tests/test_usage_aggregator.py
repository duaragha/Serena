import json
import os
import subprocess
import sys
import types
from pathlib import Path

import core.usage_aggregator as usage_aggregator
from core.usage_aggregator import (
    SCHEMA_VERSION,
    codex_window_name,
    merge_observation,
    merge_window,
)


RESET = 2_000_000_000


def observation(
    used=None,
    *,
    reset=RESET,
    observed_at=100,
    provider="claude",
    window="five_hour",
):
    data = {
        "provider": provider,
        "source": f"{provider}-test",
        "observed_at": observed_at,
        "windows": {},
    }
    if used is not None or reset is not None:
        data["windows"][window] = {
            "used_percentage": used,
            "resets_at": reset,
        }
    return data


def test_same_reset_is_monotonic_max():
    state = merge_observation({}, observation(62, observed_at=100))
    state = merge_observation(state, observation(41, observed_at=110))

    window = state["claude"]["five_hour"]
    assert window["used_percentage"] == 62
    assert window["observed_at"] == 110
    assert window["resets_at"] == RESET


def test_newer_reset_starts_lower_generation():
    state = merge_observation({}, observation(91, observed_at=100))
    state = merge_observation(
        state, observation(7, reset=RESET + 18_000, observed_at=105)
    )

    assert state["claude"]["five_hour"]["used_percentage"] == 7
    assert state["claude"]["five_hour"]["resets_at"] == RESET + 18_000


def test_small_reset_jitter_stays_in_the_same_generation():
    state = merge_observation({}, observation(37, reset=RESET + 3, observed_at=100))
    state = merge_observation(state, observation(58, reset=RESET, observed_at=200))

    window = state["claude"]["five_hour"]
    assert window["used_percentage"] == 58
    assert window["resets_at"] == RESET + 3
    assert window["observed_at"] == 200


def test_partial_observation_preserves_other_window_and_provider():
    state = merge_observation({}, observation(35, window="five_hour"))
    state = merge_observation(
        state, observation(70, window="seven_day", observed_at=120)
    )
    state = merge_observation(
        state,
        {
            "provider": "codex",
            "source": "codex-test",
            "observed_at": 130,
            "windows": {"five_hour": {"used_percentage": 18, "resets_at": RESET}},
        },
    )
    state = merge_observation(state, observation(None, reset=None, observed_at=140))

    assert state["claude"]["five_hour"]["used_percentage"] == 35
    assert state["claude"]["seven_day"]["used_percentage"] == 70
    assert state["codex"]["five_hour"]["used_percentage"] == 18
    assert state["claude"]["available"] is True


def test_older_reset_cannot_replace_newer_and_older_sample_cannot_regress():
    current = {"used_percentage": 44, "resets_at": RESET, "observed_at": 200}

    assert merge_window(
        current,
        {"used_percentage": 99, "resets_at": RESET - 18_000},
        observed_at=300,
    ) == current
    merged = merge_window(
        current,
        {"used_percentage": 12, "resets_at": RESET},
        observed_at=150,
    )
    assert merged["used_percentage"] == 44
    assert merged["observed_at"] == 200


def test_zero_is_known_but_missing_is_unknown():
    zero = merge_window({}, {"used_percentage": 0, "resets_at": RESET}, observed_at=10)
    unknown = merge_window({}, {"used_percentage": None, "resets_at": RESET}, observed_at=10)

    assert zero["used_percentage"] == 0
    assert "used_percentage" not in unknown
    assert merge_window(zero, {"resets_at": RESET}, observed_at=20)["used_percentage"] == 0


def test_codex_windows_are_classified_by_duration_not_api_slot():
    assert codex_window_name({"window_minutes": 300}, "five_hour") == "five_hour"
    assert codex_window_name({"window_minutes": 10_080}, "five_hour") == "seven_day"
    assert codex_window_name({}, "seven_day") == "seven_day"


def test_locked_cli_writers_publish_valid_max_and_preserve_other_provider(tmp_path):
    state_file = tmp_path / "live-usage.json"
    state_file.write_text(
        json.dumps(merge_observation({}, observation(27, provider="codex"))),
        encoding="utf-8",
    )
    reducer = Path(__file__).parents[1] / "core" / "usage_aggregator.py"
    processes = []
    for used in (12, 81, 39, 67, 5, 73):
        process = subprocess.Popen(
            [sys.executable, str(reducer), "--state-file", str(state_file)],
            stdin=subprocess.PIPE,
            text=True,
        )
        assert process.stdin is not None
        process.stdin.write(json.dumps(observation(used, observed_at=100 + used)))
        process.stdin.close()
        processes.append(process)

    assert [process.wait(timeout=10) for process in processes] == [0] * len(processes)
    state = json.loads(state_file.read_text(encoding="utf-8"))

    assert state["schema_version"] == SCHEMA_VERSION
    assert state["claude"]["five_hour"]["used_percentage"] == 81
    assert state["codex"]["five_hour"]["used_percentage"] == 27
    if os.name != "nt":
        # Windows has no POSIX mode bits: chmod there only toggles read-only,
        # and access is governed by the ACL on the user's profile instead.
        assert state_file.stat().st_mode & 0o777 == 0o600


def test_materialized_state_drops_chat_specific_fields():
    item = observation(22)
    item.update({"cwd": "/private/chat", "cost_usd": 9.5, "context_window": {"x": 1}})

    provider = merge_observation({}, item)["claude"]
    assert "cwd" not in provider
    assert "cost_usd" not in provider
    assert "context_window" not in provider

    legacy = {
        "claude": {
            **provider,
            "cwd": "/old/private-chat",
            "cost_usd": 12,
            "context_window": {"used_percentage": 88},
            "statusline_seen_at": 99,
        },
        "unbounded": "drop me",
    }
    migrated = merge_observation(legacy, observation(None, reset=None, observed_at=150))
    assert "cwd" not in migrated["claude"]
    assert "context_window" not in migrated["claude"]
    assert "statusline_seen_at" not in migrated["claude"]
    assert "unbounded" not in migrated


def test_windows_lock_uses_msvcrt_without_importing_fcntl(monkeypatch, tmp_path):
    calls = []
    fake_msvcrt = types.SimpleNamespace(
        LK_LOCK=1,
        LK_UNLCK=2,
        locking=lambda fd, mode, size: calls.append((fd, mode, size)),
    )
    monkeypatch.setattr(usage_aggregator, "_IS_WINDOWS", True)
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)

    with (tmp_path / "state.lock").open("a+b") as lock:
        usage_aggregator._lock_file(lock)
        usage_aggregator._unlock_file(lock)

    assert [mode for _, mode, _ in calls] == [fake_msvcrt.LK_LOCK, fake_msvcrt.LK_UNLCK]
    assert all(size == 1 for _, _, size in calls)


def test_module_import_does_not_require_fcntl():
    repo = Path(__file__).parents[1]
    script = """
import builtins
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == 'fcntl':
        raise ImportError('fcntl unavailable')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
import core.usage_aggregator
"""

    result = subprocess.run([sys.executable, "-c", script], cwd=repo, check=False)
    assert result.returncode == 0


# --- Claude's numbers only ever exist in the statusline payload -------------
#
# Codex writes its usage into session files the app can read at any time.
# Claude's arrive on stdin when Claude Code renders the status line and are gone
# when that process exits. The Linux statusline had been taping them into the
# state file with jq all along; the Windows one only rendered, so the app showed
# Claude as "waiting" forever on that machine.

def test_a_statusline_render_becomes_an_observation() -> None:
    from core.usage_aggregator import observation_from_statusline

    observation = observation_from_statusline(
        {
            "rate_limits": {
                "five_hour": {"used_percentage": 12.4, "resets_at": 1788060000},
                "seven_day": {"used_percentage": 8, "resets_at": 1788602400},
            },
            "model": {"display_name": "Opus 5 (1M context)"},
            "version": "2.1.251",
        },
        observed_at=1788056103,
    )

    assert observation["provider"] == "claude"
    assert observation["source"] == "claude-statusline"
    assert observation["observed_at"] == 1788056103
    assert observation["windows"]["five_hour"]["used_percentage"] == 12.4
    assert observation["windows"]["seven_day"]["resets_at"] == 1788602400
    # The parenthetical is context size, not a different model.
    assert observation["model"] == "Opus 5"


def test_a_render_without_rate_limits_is_not_an_observation() -> None:
    """Otherwise an ordinary render would erase real numbers with nothing."""
    from core.usage_aggregator import observation_from_statusline

    assert observation_from_statusline({"model": {"display_name": "Opus 5"}}) is None
    assert observation_from_statusline({"rate_limits": {}}) is None
    assert observation_from_statusline({"rate_limits": {"five_hour": {}}}) is None
    assert observation_from_statusline(None) is None


def test_a_half_reported_window_still_counts() -> None:
    """A percentage with no reset, or the reverse, is still worth recording."""
    from core.usage_aggregator import observation_from_statusline

    observation = observation_from_statusline(
        {"rate_limits": {"five_hour": {"used_percentage": 3}}}
    )

    assert observation["windows"] == {"five_hour": {"used_percentage": 3}}


def test_recording_writes_state_the_app_can_read(tmp_path) -> None:
    from core.usage_aggregator import record_statusline

    state_file = tmp_path / "live-usage.json"
    payload = {
        "rate_limits": {"five_hour": {"used_percentage": 4, "resets_at": 1788160000}},
        "model": {"display_name": "Opus 5"},
    }

    assert record_statusline(payload, state_file) is True

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["claude"]["five_hour"]["used_percentage"] == 4
    assert state["claude"]["source"] == "claude-statusline"


def test_recording_never_takes_the_status_line_down(tmp_path) -> None:
    """It runs inside the status line; an exception there breaks every render."""
    from core.usage_aggregator import record_statusline

    unwritable = tmp_path / "nope" / "\0bad" / "live-usage.json"

    assert record_statusline({"rate_limits": {"five_hour": {"used_percentage": 1}}}, unwritable) is False
    assert record_statusline(object(), tmp_path / "x.json") is False
