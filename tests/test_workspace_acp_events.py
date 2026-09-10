import pytest

from core.workspace_acp_events import AcpEvents


def update(events, kind, **kwargs):
    return events.update({"sessionId": "exact", "update": {"sessionUpdate": kind, **kwargs}})["params"]["item"]


def test_stream_tool_updates_and_identity_boundaries():
    events = AcpEvents("exact")
    events.begin("turn")
    first = update(events, "agent_message_chunk", content={"type": "text", "text": "hello "})
    second = update(events, "agent_message_chunk", content={"type": "text", "text": "world"})
    assert first["id"] == second["id"] and second["text"] == "hello world"
    tool = update(events, "tool_call", toolCallId="tool", title="Read file", rawInput={"path": "file"}, status="pending")
    done = update(events, "tool_call_update", toolCallId="tool", status="completed", rawOutput="result")
    assert tool["id"] == done["id"] and done["input"] == {"path": "file"}
    assert done["tool"] == "Read file" and done["output"] == "result"
    third = update(events, "agent_message_chunk", content={"type": "text", "text": "after tool"})
    assert third["id"] != first["id"]
    with pytest.raises(ValueError, match="another session"):
        events.update({"sessionId": "wrong", "update": {"sessionUpdate": "tool_call"}})
    final = events.complete("end_turn")["params"]["turn"]
    assert len(final["items"]) == 3 and final["status"] == "completed"


def test_explicit_message_ids_and_unrecognized_data_are_retained():
    events = AcpEvents("exact")
    events.begin("turn")
    first = update(events, "agent_message_chunk", messageId="a", content={"type": "text", "text": "one"})
    update(events, "plan", entries=[{"content": "step", "status": "pending"}])
    second = update(events, "agent_message_chunk", messageId="a", content={"type": "text", "text": "two"})
    assert first["id"] == second["id"] and second["text"] == "onetwo"
    raw = update(events, "future_kind", metadata={"value": 2})
    assert raw["providerOriginal"]["metadata"] == {"value": 2}


def test_tool_display_content_survives_partial_updates_alongside_raw_output():
    events = AcpEvents("exact")
    events.begin("turn")
    content = [{"type": "content", "content": {"type": "text", "text": "readable"}}]
    first = update(events, "tool_call", toolCallId="tool", content=content, rawOutput={"raw": True})
    content.clear()
    final = update(events, "tool_call_update", toolCallId="tool", status="completed")
    assert final["displayContent"] == first["displayContent"]
    assert final["displayContent"][0]["content"]["text"] == "readable"
    assert final["output"] == {"raw": True}


def test_idle_metadata_never_creates_a_turn():
    events = AcpEvents("exact")
    metadata = events.update({"sessionId": "exact", "update": {
        "sessionUpdate": "available_commands_update", "availableCommands": [{"name": "help"}]}})
    assert metadata["method"] == "workspace/acpMetadata" and events.turn is None
    with pytest.raises(ValueError, match="no active"):
        update(events, "agent_message_chunk", content={"type": "text", "text": "late"})


def test_user_image_keeps_text_boundaries_and_original_content():
    events = AcpEvents("exact")
    events.begin("history")
    before = update(events, "user_message_chunk", content={"type": "text", "text": "before"})
    content = {"type": "image", "mimeType": "image/png", "data": "encoded", "annotations": {"audience": ["assistant"]}}
    image = update(events, "user_message_chunk", content=content)
    after = update(events, "user_message_chunk", content={"type": "text", "text": "after"})
    assert len({before["id"], image["id"], after["id"]}) == 3
    assert image["type"] == "userMessage"
    assert image["content"] == [{"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": "encoded"}}]
    assert image["providerOriginal"]["content"] == content
    content["data"] = "changed"
    assert image["providerOriginal"]["content"]["data"] == "encoded"
    with pytest.raises(ValueError, match="Invalid ACP image"):
        update(events, "user_message_chunk", content={"type": "image", "data": None})


def test_permission_requires_exact_offered_option_and_explicit_resolution():
    events = AcpEvents("exact")
    events.begin("turn")
    params = {"sessionId": "exact", "toolCall": {"title": "Write file"},
              "options": [{"optionId": "once", "name": "Allow once", "kind": "allow_once"}]}
    question = events.permission("request", params)
    assert question["params"]["threadId"] == "exact"
    params["options"][0]["optionId"] = "changed"
    with pytest.raises(ValueError, match="not offered"):
        events.answer("request", "changed")
    assert events.answer("request", "once") == {"outcome": {"outcome": "selected", "optionId": "once"}}
    assert events.answer("request") == {"outcome": {"outcome": "cancelled"}}
    with pytest.raises(ValueError, match="unresolved"):
        events.complete("end_turn")
    events.resolved("request")
    with pytest.raises(ValueError, match="no longer"):
        events.answer("request", "once")
    assert events.complete("cancelled")["params"]["turn"]["status"] == "interrupted"
