from copy import deepcopy

import pytest

from core.workspace_claude_events import ClaudeEvents


def event(events, method):
    return next(item["params"] for item in events if item["method"] == method)


def test_thinking_stream_is_visible_and_uses_the_completed_block_identity():
    adapter = ClaudeEvents("exact")
    adapter.begin_input("turn")
    def stream(value):
        return adapter.receive({"type": "stream_event", "event": value})
    stream({"type": "message_start", "message": {"id": "m"}})
    stream({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}})
    result = stream({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Checking the files"}})
    item = event(result, "item/started")["item"]
    assert item["type"] == "claudeThinking"
    assert item["text"] == "Checking the files"
    done = adapter.blocks([{"thinking": "Checking the files", "signature": "opaque"}], "m")[0]
    assert done["id"] == item["id"]
    assert done["type"] == "claudeThinking"


def test_queued_user_echo_and_individual_results_keep_exact_turns():
    events = ClaudeEvents("exact")
    events.begin_input("first")
    events.begin_input("second")
    assert events.turn == "first"
    echo = event(events.receive({"type": "user", "session_id": "exact", "uuid": "second",
                                 "message": {"role": "user", "content": "follow-up"}}), "item/completed")
    assert echo["turnId"] == "second"
    result = {"type": "result", "session_id": "exact", "is_error": False}
    first = event(events.receive({**result, "user_message_uuid": "first"}), "turn/completed")
    assert first["turn"]["id"] == "first" and events.turn == "second"
    second = event(events.receive({**result, "user_message_uuid": "second"}), "turn/completed")
    assert second["turn"]["id"] == "second" and events.turn is None
    assert events.pending_inputs == []


def test_grouped_result_completes_only_acknowledged_prefix_without_duplicate_output():
    events = ClaudeEvents("exact")
    for input_id in ["first", "second", "third"]:
        events.begin_input(input_id)
    output = events.receive({"type": "result", "session_id": "exact", "is_error": False,
                             "user_message_uuids": ["first", "second"], "num_turns": 0,
                             "result": "Combined reply", "duration_ms": 10})
    completed = [entry["params"]["turn"] for entry in output if entry["method"] == "turn/completed"]
    assert [turn["id"] for turn in completed] == ["first", "second"]
    assert completed[1]["combinedWithTurnId"] == "first" and "durationMs" not in completed[1]
    assert sum(entry["method"] == "item/completed" for entry in output) == 1
    assert events.turn == "third" and events.pending_inputs == ["third"]


@pytest.mark.parametrize("ack", [{}, {"user_message_uuids": ["first", "third"]},
    {"user_message_uuids": ["first", "unknown"]}])
def test_ambiguous_queued_result_preserves_pending_inputs(ack):
    events = ClaudeEvents("exact")
    for input_id in ["first", "second", "third"]:
        events.begin_input(input_id)
    with pytest.raises(ValueError, match="acknowledge"):
        events.receive({"type": "result", "session_id": "exact", "is_error": False, **ack})
    assert events.pending_inputs == ["first", "second", "third"] and events.turn == "first"


@pytest.mark.parametrize("acknowledgement", [
    {"user_message_uuid": "older"}, {"user_message_uuids": ["older"]},
    {"user_message_uuids": "active"}, {"user_message_uuids": [None]},
])
def test_native_result_cannot_complete_an_unacknowledged_input(acknowledgement):
    events = ClaudeEvents("exact")
    events.turn = "active"
    with pytest.raises(ValueError, match="acknowledge"):
        events.receive({"type": "result", "session_id": "exact", "is_error": False,
                        **acknowledgement})
    assert events.turn == "active"


@pytest.mark.parametrize("acknowledgement", [{}, {"user_message_uuid": "active"},
    {"user_message_uuids": ["active"]}])
def test_native_result_accepts_matching_or_legacy_input_identity(acknowledgement):
    events = ClaudeEvents("exact")
    events.turn = "active"
    completed = event(events.receive({"type": "result", "session_id": "exact", "is_error": False,
                                     **acknowledgement}), "turn/completed")
    assert completed["turn"]["id"] == "active" and events.turn is None


@pytest.mark.parametrize("array", [False, True])
def test_history_command_is_readable_without_losing_original(array):
    raw = "<command-name>/effort</command-name>\n<command-message>effort</command-message>\n<command-args>low</command-args>"
    content = [{"type": "text", "text": raw}] if array else raw
    record = {"session_id": "exact", "uuid": "command", "type": "user", "message": {"content": content}}
    original = deepcopy(record)
    history = ClaudeEvents("exact").history([record])
    item = history["params"]["thread"]["turns"][0]["items"][0]
    assert item["content"] == [{"type": "text", "text": "/effort low"}]
    assert item["providerOriginal"] == original
    assert record == original


@pytest.mark.parametrize("raw", [
    "explain <command-name>/effort</command-name>",
    "<command-name>/effort</command-name><command-message>effort</command-message>",
    "<command-name>/effort</command-name><command-message>other</command-message><command-args>low</command-args>",
    "<command-name>/effort</command-name><command-message>effort</command-message><command-args><b>low</b></command-args>",
    '<!DOCTYPE command [<!ENTITY x "expanded">]><command-name>/effort</command-name>',
    "<command-name>/effort</command-name><command-message>effort</command-message><command-args>low</command-args> do not omit this",
])
def test_history_does_not_rewrite_partial_or_ambiguous_markup(raw):
    record = {"session_id": "exact", "uuid": "user", "type": "user", "message": {"content": raw}}
    item = ClaudeEvents("exact").history([record])["params"]["thread"]["turns"][0]["items"][0]
    assert item["content"] == [{"type": "text", "text": raw}]


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


@pytest.mark.parametrize("parent", [None, "child-tool"])
@pytest.mark.parametrize("include_thinking", [False, True])
def test_final_text_keeps_stream_identity_when_sdk_omits_thinking(parent, include_thinking):
    adapter = ClaudeEvents("exact")
    adapter.begin_input("turn")
    def stream(value):
        return adapter.receive({"type": "stream_event", "parent_tool_use_id": parent, "event": value})
    for message_id in ["first", "second"]:
        stream({"type": "message_start", "message": {"id": message_id}})
        for index, kind, value in [(0, "thinking", "Checking"), (1, "text", "Same reply")]:
            stream({"type": "content_block_start", "index": index, "content_block": {"type": kind, kind: ""}})
            stream({"type": "content_block_delta", "index": index, "delta": {"type": kind + "_delta", kind: value}})
            stream({"type": "content_block_stop", "index": index})
        content = ([{"type": "thinking", "thinking": "Checking"}] if include_thinking else [])
        content += [{"type": "text", "text": "Same reply"}]
        final = {"type": "assistant", "parent_tool_use_id": parent,
                 "message": {"id": message_id, "content": content}}
        original = deepcopy(final)
        for _ in range(2):
            output = adapter.receive(final)
            items = [entry["params"]["item"] for entry in output if entry["method"] == "item/completed"]
            text = next(item for item in items if item["type"] == "agentMessage")
            assert text["id"] == message_id + ":1"
            assert text["text"] == "Same reply"
            assert event(output, "workspace/claude")["record"] == original
        assert final == original
    adapter.receive({"type": "result", "is_error": False})
    assert adapter.streamed_blocks == {}


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


@pytest.mark.parametrize("parent, next_turn, error, text, rows", [
    (None, False, False, "Effort set to low", 0),
    ("child-tool", False, False, "Effort set to low", 1),
    (None, True, False, "Effort set to low", 1),
    (None, False, True, "Effort set to low", 1),
    (None, False, False, "Different result", 1),
])
def test_local_result_dedup_is_exact_root_turn_only(parent, next_turn, error, text, rows):
    converter = ClaudeEvents("exact")
    converter.turn = "first"
    assistant = {"type": "assistant", "session_id": "exact", "parent_tool_use_id": parent,
                 "message": {"id": "message", "model": "<synthetic>",
                             "content": [{"type": "text", "text": "Effort set to low"}]}}
    assert event(converter.receive(assistant), "item/completed")["item"]["text"] == "Effort set to low"
    if next_turn:
        converter.turn = "second"
    result = {"type": "result", "session_id": "exact", "num_turns": 0,
              "is_error": error, "result": text, "duration_ms": 4}
    events = converter.receive(result)
    assert len([item for item in events if item["method"] == "item/completed"]) == rows
    assert event(events, "workspace/claude")["record"] == result
    assert event(events, "turn/completed")["turn"]["status"] == ("failed" if error else "completed")
    assert converter.last_root_text is None
