import json

from core.runtime_activity import TurnActivityReader


def _append(path, record):
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _codex_event(kind):
    return {"type": "event_msg", "payload": {"type": kind}}


def test_codex_task_markers_track_active_and_finished_turns(tmp_path):
    path = tmp_path / "rollout.jsonl"
    reader = TurnActivityReader()

    _append(path, _codex_event("task_complete"))
    _append(path, _codex_event("task_started"))
    _append(path, {"type": "response_item", "payload": {"type": "reasoning"}})
    assert reader.read(path, "codex") is True

    _append(path, _codex_event("task_complete"))
    assert reader.read(path, "codex") is False


def test_codex_aborted_turn_is_not_working(tmp_path):
    path = tmp_path / "rollout.jsonl"
    _append(path, _codex_event("task_started"))
    _append(path, _codex_event("turn_aborted"))

    assert TurnActivityReader().read(path, "codex") is False


def test_claude_turn_duration_is_the_completion_boundary(tmp_path):
    path = tmp_path / "claude.jsonl"
    reader = TurnActivityReader()

    _append(path, {"type": "user", "message": {"role": "user"}})
    _append(path, {"type": "assistant", "message": {"role": "assistant"}})
    assert reader.read(path, "claude") is True

    _append(path, {"type": "system", "subtype": "turn_duration"})
    assert reader.read(path, "claude") is False


def test_claude_rate_limit_is_also_a_completion_boundary(tmp_path):
    path = tmp_path / "claude.jsonl"
    reader = TurnActivityReader()

    _append(path, {"type": "system", "subtype": "turn_duration"})
    start_offset = path.stat().st_size
    _append(path, {"type": "user", "message": {"role": "user"}})
    assert reader.completed_since(path, "claude", start_offset) is False

    _append(
        path,
        {
            "type": "assistant",
            "message": {"role": "assistant"},
            "error": "rate_limit",
            "isApiErrorMessage": True,
        },
    )
    assert reader.read(path, "claude") is False
    assert reader.completed_since(path, "claude", start_offset) is True
    assert reader.waiting_for_usage_reset(path, "claude") is True

    _append(path, {"type": "user", "message": {"role": "user"}})
    assert reader.waiting_for_usage_reset(path, "claude") is False


def test_completion_before_turn_offset_does_not_finish_new_turn(tmp_path):
    path = tmp_path / "claude.jsonl"
    reader = TurnActivityReader()

    _append(path, {"type": "system", "subtype": "turn_duration"})
    start_offset = path.stat().st_size
    _append(path, {"type": "user", "message": {"role": "user"}})

    assert reader.completed_since(path, "claude", start_offset) is False


def test_long_active_tail_without_start_marker_still_counts_as_working(tmp_path):
    path = tmp_path / "rollout.jsonl"
    _append(path, _codex_event("task_started"))
    for index in range(80):
        _append(
            path,
            {
                "type": "response_item",
                "payload": {"type": "reasoning", "summary": f"chunk-{index}"},
            },
        )

    assert TurnActivityReader(tail_bytes=1024).read(path, "codex") is True
