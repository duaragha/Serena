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
    update(events, "plan", entries=[{"content": "step", "status": "pending", "priority": "high"}])
    second = update(events, "agent_message_chunk", messageId="a", content={"type": "text", "text": "two"})
    assert first["id"] == second["id"] and second["text"] == "onetwo"
    raw = update(events, "future_kind", metadata={"value": 2})
    assert raw["providerOriginal"]["metadata"] == {"value": 2}


def test_plan_updates_replace_the_list_and_preserve_native_status():
    events = AcpEvents("exact")
    events.begin("turn")
    first = update(events, "plan", entries=[{"content": "old", "status": "pending", "priority": "high"}])
    second = update(events, "plan", entries=[{"content": "new", "status": "in_progress", "priority": "low"}])
    assert first["id"] == second["id"]
    assert len(events.items) == 1 and second["entries"][0]["content"] == "new"
    assert second["entries"][0]["status"] == "in_progress"
    with pytest.raises(ValueError, match="Invalid ACP plan"):
        update(events, "plan", entries=[{"content": "fake", "status": "done", "priority": "high"}])
    cleared = update(events, "plan", entries=[])
    assert cleared["id"] == first["id"] and cleared["entries"] == []


@pytest.mark.parametrize("reason,status", [("end_turn", "completed"), ("cancelled", "interrupted"),
    ("max_tokens", "interrupted"), ("max_turn_requests", "interrupted"), ("refusal", "failed")])
def test_stop_reason_does_not_claim_incomplete_work_succeeded(reason, status):
    events = AcpEvents("exact")
    events.begin("turn")
    result = events.complete(reason)["params"]["turn"]
    assert result["status"] == status and result["stopReason"] == reason
    assert events.turn is None


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


@pytest.mark.parametrize("used,size,valid", [(0, 200000, True), (53000, 200000, True),
    (210000, 200000, True), (True, 200000, False), (10, 0, False), (-1, 100, False),
    (None, 100, False), (10, 2**54, False)])
def test_context_usage_is_provider_reported_not_an_account_limit(used, size, valid):
    events = AcpEvents("exact")
    raw = {"sessionUpdate": "usage_update", "used": used, "size": size,
           "cost": {"amount": 0.5, "currency": "USD"}}
    event = events.update({"sessionId": "exact", "update": raw})
    assert event["method"] == "workspace/acpUsage"
    assert event["params"]["usage"] == ({"used": used, "size": size} if valid else None)
    assert event["params"]["providerOriginal"] == raw
    assert events.turn is None and not events.items


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
