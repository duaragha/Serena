from fleet.gemini import GeminiStream, MODEL, READ_TOOLS


def initialized():
    stream = GeminiStream()
    stream.accept({"event": "init", "conversation_id": "session", "init": {
        "model": MODEL, "tools": sorted(READ_TOOLS)}})
    return stream


def finish(stream, status="SUCCESS", **extra):
    stream.accept({"event": "result", "result": {
        "conversation_id": "session", "status": status, "response": "findings", **extra}})


def test_success_preserves_usage():
    stream = initialized()
    finish(stream, usage={"input_tokens": 12, "thinking_tokens": 3})
    assert stream.completion_error(0) is None
    assert stream.usage["thinking_tokens"] == 3


def test_zero_exit_is_not_completion():
    for status in ("WAITING", "RUNNING", "ERROR", "CANCELED", "INTERRUPTED", "INVALID"):
        stream = initialized()
        finish(stream, status)
        assert stream.completion_error(0)
    assert initialized().completion_error(0)


def test_unsafe_init_remains_failed_after_success():
    stream = GeminiStream()
    stream.accept({"event": "init", "conversation_id": "session", "init": {
        "model": MODEL, "tools": ["view_file", "run_command"]}})
    finish(stream)
    assert "read-only" in stream.completion_error(0)


def test_identity_and_process_failures():
    stream = initialized()
    finish(stream, conversation_id="other")
    assert "conversation" in stream.completion_error(0)
    stream = initialized()
    finish(stream)
    assert stream.completion_error(1)
    stream = GeminiStream()
    finish(stream)
    assert stream.completion_error(0)


def test_step_count_deduplicates_stream_updates():
    stream = initialized()
    for _ in range(3):
        stream.accept({"event": "step_update", "step_update": {
            "step_index": 2, "step_type": "tool", "state": "DONE"}})
    assert len(stream.completed_steps) == len(stream.tool_steps) == 1


def test_malformed_events_do_not_crash():
    stream = initialized()
    for event in (None, [], 7, {}, {"event": "init", "init": []}):
        stream.accept(event)
    assert stream.completion_error(0)
