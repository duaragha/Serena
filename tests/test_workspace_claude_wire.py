from copy import deepcopy

import pytest

from core.workspace_claude_events import ClaudeEvents


def event(events, method):
    return next(item["params"] for item in events if item["method"] == method)


def test_native_init_keeps_raw_record_and_sets_capabilities():
    converter = ClaudeEvents("exact")
    message = {"type": "system", "subtype": "init", "session_id": "exact",
               "model": "parent-model", "tools": ["Read"], "permissionMode": "default"}
    original = deepcopy(message)
    events = converter.receive(message)
    assert event(events, "workspace/settings")["model"] == "parent-model"
    assert event(events, "workspace/claude")["record"] == original
    assert message == original


def test_native_stream_and_canonical_message_share_item_identity():
    converter = ClaudeEvents("exact")
    converter.turn = "turn"
    converter.receive({"type": "stream_event", "session_id": "exact",
                       "event": {"type": "message_start", "message": {"id": "message"}}})
    delta = converter.receive({"type": "stream_event", "session_id": "exact",
                               "event": {"type": "content_block_delta", "index": 0,
                                         "delta": {"type": "text_delta", "text": "hello"}}})
    final = converter.receive({"type": "assistant", "session_id": "exact", "uuid": "different-uuid",
                               "message": {"id": "message", "model": "parent-model",
                                           "content": [{"type": "text", "text": "hello"}]}})
    assert event(delta, "item/agentMessage/delta")["itemId"] == "message:0"
    assert event(final, "item/completed")["item"]["id"] == "message:0"
    assert event(final, "workspace/settings")["model"] == "parent-model"


def test_native_subagent_model_never_changes_parent_model():
    converter = ClaudeEvents("exact")
    converter.turn = "turn"
    events = converter.receive({"type": "assistant", "session_id": "exact", "parent_tool_use_id": "parent-tool",
                                "message": {"id": "child", "model": "child-model",
                                            "content": [{"type": "text", "text": "child response"}]}})
    item = event(events, "item/completed")["item"]
    assert item["parentToolUseId"] == "parent-tool"
    assert item["sourceModel"] == "child-model"
    assert not any(item["method"] == "workspace/settings" for item in events)


def test_native_tools_results_and_task_lifecycle():
    converter = ClaudeEvents("exact")
    converter.turn = "turn"
    converter.receive({"type": "assistant", "session_id": "exact", "message": {"id": "tool-message",
                       "content": [{"type": "tool_use", "id": "tool", "name": "Bash", "input": {"command": "pwd"}}]}})
    events = converter.receive({"type": "user", "session_id": "exact", "uuid": "result-message",
                                "message": {"content": [{"type": "tool_result", "tool_use_id": "tool",
                                                         "content": "/project", "is_error": False}]}})
    item = event(events, "item/completed")["item"]
    assert item["id"] == "tool" and item["tool"] == "Bash"
    assert item["output"] == "/project" and item["status"] == "completed"
    for subtype, status in [("task_started", "running"), ("task_notification", "completed"), ("task_progress", "running")]:
        tasks = converter.receive({"type": "system", "session_id": "exact", "subtype": subtype,
                                   "task_id": "background", "status": status})
    assert event(tasks, "workspace/backgroundTask")["task"]["status"] == "completed"


def test_native_local_result_finishes_turn_with_usage_and_unknown_records_survive():
    converter = ClaudeEvents("exact")
    converter.turn = "turn"
    result = {"type": "result", "session_id": "exact", "num_turns": 0, "is_error": False,
              "result": "Effort set to low", "duration_ms": 4, "usage": {"input_tokens": 0}}
    events = converter.receive(result)
    assert event(events, "turn/completed")["turn"]["status"] == "completed"
    assert event(events, "item/completed")["item"]["text"] == result["result"]
    assert event(events, "workspace/claudeUsage")["usage"] == result["usage"]
    assert converter.turn is None
    unknown = {"type": "future_sdk_event", "session_id": "exact", "payload": {"original": True}}
    assert event(converter.receive(unknown), "workspace/claude")["record"] == unknown
    with pytest.raises(ValueError, match="different session"):
        converter.receive({**unknown, "session_id": "wrong"})
