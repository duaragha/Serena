import json
from uuid import uuid4

import pytest

from core.codex_history import HistoryUnavailable
from core.codex_records import read_messages


def rollout(root, messages, *, parent=None, archived=False, legacy=False):
    sid = str(uuid4())
    directory = root / ("archived_sessions" if archived else "sessions") / "2026" / "09"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-{sid}.jsonl"
    meta = {"id": sid}
    start = 0
    if parent:
        rows = [json.loads(line) for line in parent.read_text().splitlines()]
        start = rows[-1]["ordinal"] + 1
        meta["history_base"] = {"thread_id": rows[0]["payload"]["id"],
                                "end_byte_offset": parent.stat().st_size,
                                "end_ordinal_exclusive": start}
    rows = [{"type": "session_meta", "payload": meta, "ordinal": start}]
    for number, text in enumerate(messages, start + 1):
        if legacy:
            rows.append({"ordinal": number, "type": "event_msg",
                         "payload": {"type": "user_message", "message": text}})
        else:
            rows.append({"ordinal": number, "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def alter_base(path, **updates):
    lines = path.read_text().splitlines()
    first = json.loads(lines[0])
    first["payload"]["history_base"].update(updates)
    path.write_text(json.dumps(first) + "\n" + "\n".join(lines[1:]) + "\n")


def test_nested_archived_prefix_excludes_later_parent_messages(tmp_path):
    parent = rollout(tmp_path, ["original"], archived=True, legacy=True)
    fork = rollout(tmp_path, ["fork"], parent=parent)
    child = rollout(tmp_path, ["nested"], parent=fork)
    with parent.open("a") as stream:
        stream.write(json.dumps({"ordinal": 2, "type": "event_msg", "payload": {
            "type": "user_message", "message": "later parent only"}}) + "\n")
    assert [text for _, text, _ in read_messages(child)] == ["original", "fork", "nested"]


@pytest.mark.parametrize("case", ["missing", "duplicate", "escape", "cycle", "identity",
                                   "offset", "ordinal", "split", "invalid_id", "bool_bound"])
def test_untrustworthy_ancestry_is_unavailable(tmp_path, case):
    root = tmp_path / "store"
    parent = rollout(root, ["original"])
    fork = rollout(root, ["fork"], parent=parent)
    if case == "missing":
        parent.unlink()
    elif case == "duplicate":
        duplicate = root / "archived_sessions" / parent.name
        duplicate.parent.mkdir()
        duplicate.write_bytes(parent.read_bytes())
    elif case == "escape":
        outside = tmp_path / "outside.jsonl"
        parent.rename(outside)
        parent.symlink_to(outside)
    elif case == "cycle":
        sid = json.loads(fork.read_text().splitlines()[0])["payload"]["id"]
        alter_base(fork, thread_id=sid, end_byte_offset=fork.stat().st_size, end_ordinal_exclusive=999)
    elif case == "identity":
        parent.write_text(parent.read_text().replace(json.loads(parent.read_text().splitlines()[0])["payload"]["id"], str(uuid4())))
    elif case == "offset":
        alter_base(fork, end_byte_offset=999999)
    elif case == "ordinal":
        alter_base(fork, end_ordinal_exclusive=1)
    elif case == "split":
        alter_base(fork, end_byte_offset=parent.stat().st_size - 1)
    elif case == "invalid_id":
        alter_base(fork, thread_id="../../outside")
    else:
        alter_base(fork, end_byte_offset=True)
    with pytest.raises(HistoryUnavailable):
        read_messages(fork)
