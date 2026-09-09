"""Measure real history conversion on controlled tool-heavy records, no provider."""
import json
import sys
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_claude_events import ClaudeEvents


def records(count):
    yield {"session_id": "proof", "uuid": "user", "type": "user", "message": {"content": "Inspect the project"}}
    for index in range(count):
        yield {"session_id": "proof", "uuid": f"a-{index}", "type": "assistant", "message": {
            "id": f"a-{index}", "model": "configured-model", "content": [{"type": "tool_use", "id": f"tool-{index}", "name": "Read", "input": {"file_path": f"file-{index}.py"}}]}}
        yield {"session_id": "proof", "uuid": f"r-{index}", "type": "user", "message": {
            "content": [{"type": "tool_result", "tool_use_id": f"tool-{index}", "content": f"result-{index}"}]}}


for size in (1000, 4000, 8000):
    source = list(records(size))
    start = perf_counter()
    result = ClaudeEvents("proof").history(source)
    elapsed = perf_counter() - start
    turns = result["params"]["thread"]["turns"]
    assert len(turns) == 1 and len(turns[0]["items"]) == size + 1
    for index, item in enumerate(turns[0]["items"][1:]):
        assert item["id"] == f"tool-{index}"
        assert item["input"] == {"file_path": f"file-{index}.py"}
        assert item["output"] == f"result-{index}" and item["status"] == "completed"
    print(json.dumps({"tool_calls": size, "seconds": round(elapsed, 4), "ordered_pairs_preserved": size}))
