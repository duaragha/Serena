from __future__ import annotations

import asyncio

from core.brain_memory_tools import delete_memory, edit_memory, save_memory
from memory import store as legacy_store


def _message(result: dict) -> str:
    return str(result["content"][0]["text"])


def test_legacy_memory_write_tools_fail_closed_without_mutating_markdown(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(legacy_store, "MEMORY_DIR", tmp_path / "memory")
    memory_id = legacy_store.add_memory(
        "Raghav prefers the blue car.", "user", _no_mirror=True
    )
    before = legacy_store.list_memories()

    save_result = asyncio.run(
        save_memory.handler({"content": "Raghav prefers the red car.", "type": "user"})
    )
    edit_result = asyncio.run(
        edit_memory.handler(
            {"memory_id": memory_id, "content": "Raghav prefers the red car."}
        )
    )
    delete_result = asyncio.run(delete_memory.handler({"memory_id": memory_id}))

    assert "direct legacy memory writes are disabled" in _message(save_result)
    assert "direct legacy memory writes are disabled" in _message(edit_result)
    assert "direct legacy memory writes are disabled" in _message(delete_result)
    assert legacy_store.list_memories() == before
