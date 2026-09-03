import json
from pathlib import Path

from core.codex_usage_reader import CodexUsageReader


def _rate_event(
    used: int,
    *,
    reset: int,
    timestamp: str,
    include_five_hour: bool = True,
) -> str:
    limits = {
        "limit_id": "codex",
        "secondary": {
            "used_percent": used,
            "resets_at": reset,
            "window_minutes": 10_080,
        },
    }
    if include_five_hour:
        limits["primary"] = {
            "used_percent": used // 2,
            "resets_at": reset - 100,
            "window_minutes": 300,
        }
    return json.dumps(
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": limits},
        }
    )


def _rollout(root: Path) -> Path:
    path = root / "2026" / "07" / "15" / "rollout-test.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"payload": {"cli_version": "1.2.3"}}) + "\n",
        encoding="utf-8",
    )
    return path


def test_reader_tails_large_rollouts_and_then_reads_only_appends(tmp_path):
    rollout = _rollout(tmp_path)
    with rollout.open("a", encoding="utf-8") as fh:
        fh.write(("x" * 100 + "\n") * 200)
        fh.write(
            _rate_event(
                57,
                reset=2_000_000_000,
                timestamp="2026-07-15T12:00:00Z",
            )
            + "\n"
        )

    reader = CodexUsageReader(
        discovery_interval=0,
        tail_bytes=4096,
        max_increment_bytes=8192,
    )
    first = reader.read(tmp_path, model="gpt-test", now=1_800_000_000)

    assert first["available"] is True
    assert first["seven_day"]["used_percentage"] == 57
    assert first["five_hour"]["used_percentage"] == 28
    assert first["version"] == "1.2.3"
    assert reader.bytes_read <= 4096

    before = reader.bytes_read
    appended = _rate_event(
        63,
        reset=2_000_000_000,
        timestamp="2026-07-15T12:01:00Z",
    ) + "\n"
    with rollout.open("a", encoding="utf-8") as fh:
        fh.write(appended)

    second = reader.read(tmp_path, model="gpt-test", now=1_800_000_001)
    assert second["seven_day"]["used_percentage"] == 63
    assert reader.bytes_read - before == len(appended.encode("utf-8"))


def test_latest_observation_controls_which_windows_are_active(tmp_path):
    rollout = _rollout(tmp_path)
    with rollout.open("a", encoding="utf-8") as fh:
        fh.write(
            _rate_event(
                40,
                reset=2_000_000_000,
                timestamp="2026-07-15T12:00:00Z",
            )
            + "\n"
        )

    reader = CodexUsageReader(discovery_interval=0)
    assert "five_hour" in reader.read(tmp_path, now=1_800_000_000)

    with rollout.open("a", encoding="utf-8") as fh:
        fh.write(
            _rate_event(
                61,
                reset=2_000_000_000,
                timestamp="2026-07-15T12:01:00Z",
                include_five_hour=False,
            )
            + "\n"
        )

    latest = reader.read(tmp_path, now=1_800_000_001)
    assert "five_hour" not in latest
    assert latest["seven_day"]["used_percentage"] == 61
