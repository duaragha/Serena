from fleet.gemini import AGENT, GeminiStream, MODEL, READ_TOOLS


def initialized():
    stream = GeminiStream()
    stream.accept({"event": "init", "conversation_id": "session", "init": {
        "model": MODEL, "agent": AGENT, "tools": sorted(READ_TOOLS)}})
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


def test_research_profiles_only_change_discover():
    from fleet.policy import build_policy, builtin_config, validate_policy_snapshot
    from fleet.policy import policy_models_match_contract
    arms = [build_policy("research", f"Fleet research comparison: {arm}\nCompare SQLite transactions",
                         config=builtin_config(), provider_mode="balanced", worker_count=1)
            for arm in ("luna", "gemini")]
    for arm in arms:
        validate_policy_snapshot(arm.to_dict())
        assert policy_models_match_contract("research", arm.to_dict())
    def specs(arm):
        return [(w.provider, w.model, w.effort, w.access_mode)
                for p in arm.phases[1:] for w in p.workers]
    assert specs(arms[0]) == specs(arms[1])
    assert arms[1].phases[0].workers[0].provider == "gemini"


def test_gemini_cannot_override_provider_only():
    import pytest
    from fleet.policy import build_policy, builtin_config
    with pytest.raises(ValueError, match="balanced"):
        build_policy("research", "Fleet research comparison: gemini", config=builtin_config(),
                     provider_mode="codex")


def test_native_search_receipts(tmp_path):
    import json
    from fleet.completion_gate import _event_log_research_activity
    path = tmp_path / "events.jsonl"
    lines = []
    for index, state, error in ((1, "ACTIVE", None), (1, "DONE", None), (1, "DONE", None),
                                (2, "DONE", {"message": "denied"})):
        event = {"event": "step_update", "step_update": {"step_index": index,
                 "state": state, "step_type": "tool", "tool_name": "search_web",
                 "tool_info": {"error": error}}}
        lines.append(json.dumps({"line": json.dumps(event)}))
    path.write_text("\n".join(lines))
    assert _event_log_research_activity(str(path)) == {"searches": 1, "fetches": 0}
