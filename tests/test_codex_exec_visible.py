import json
import shutil
from pathlib import Path

from click.testing import CliRunner

import cli
from core import indexer, metadata


def _fake_codex(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import time
from pathlib import Path

sid = "019e0000-0000-7000-8000-000000000001"
print(json.dumps({"type": "thread.started", "thread_id": sid}), flush=True)
time.sleep(float(os.environ.get("SERENA_FAKE_CODEX_DELAY", "0.2")))
Path(os.environ["SERENA_FAKE_CODEX_DONE"]).write_text("done", encoding="utf-8")
print(json.dumps({
    "type": "item.completed",
    "item": {"type": "agent_message", "text": "real codex result"},
}), flush=True)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_link_current_surfaces_real_codex_before_agent_finishes(monkeypatch, tmp_path):
    fake_codex = tmp_path / "codex"
    done_marker = tmp_path / "done"
    _fake_codex(fake_codex)
    monkeypatch.setenv("SERENA_FAKE_CODEX_DONE", str(done_marker))
    monkeypatch.setattr(cli, "_detect_claude_sid", lambda: "claude-parent-sid")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda command: str(fake_codex) if command == "codex" else None,
    )

    calls = []

    def set_resident_work(sid):
        calls.append(("resident", sid, done_marker.exists()))

    monkeypatch.setattr(metadata, "set_resident_work", set_resident_work)
    monkeypatch.setattr(
        metadata,
        "set_custom_title",
        lambda sid, title: calls.append(("title", sid, title)),
    )
    monkeypatch.setattr(
        metadata,
        "set_external_runtime",
        lambda sid, **runtime: calls.append(
            ("claim", sid, runtime["kind"], runtime["pid"], done_marker.exists())
        ),
    )
    monkeypatch.setattr(
        metadata,
        "clear_external_runtime",
        lambda sid, **runtime: calls.append(
            ("release", sid, runtime["pid"], done_marker.exists())
        ) or True,
    )
    monkeypatch.setattr(
        metadata,
        "link_sessions",
        lambda sids: calls.append(("link", tuple(sids))) or "workflow-group",
    )
    monkeypatch.setattr(indexer, "update_index", lambda: calls.append(("index",)))

    result = CliRunner().invoke(
        cli.main,
        [
            "codex-exec",
            "--link-current",
            "--model",
            "gpt-5.6-sol",
            "--effort",
            "xhigh",
            "--cwd",
            str(tmp_path),
            "delegated task",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    assert payload["ok"] is True
    assert payload["result_text"] == "real codex result"
    assert payload["visible"] is True
    assert payload["group"] == "workflow-group"
    assert calls[0] == (
        "resident",
        "019e0000-0000-7000-8000-000000000001",
        False,
    )
    assert (
        "title",
        "019e0000-0000-7000-8000-000000000001",
        "Sol 5.6 xhigh: delegated task",
    ) in calls
    assert (
        "link",
        ("019e0000-0000-7000-8000-000000000001", "claude-parent-sid"),
    ) in calls
    claim = next(call for call in calls if call[0] == "claim")
    release = next(call for call in calls if call[0] == "release")
    assert claim[1:3] == (
        "019e0000-0000-7000-8000-000000000001",
        "codex-exec",
    )
    assert claim[3] > 0
    assert claim[4] is False
    assert release == (
        "release",
        "019e0000-0000-7000-8000-000000000001",
        claim[3],
        True,
    )
    assert calls.count(("index",)) == 2


def test_link_current_and_explicit_link_are_mutually_exclusive():
    result = CliRunner().invoke(
        cli.main,
        [
            "codex-exec",
            "--link-current",
            "--link-to",
            "other-session",
            "task",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output.strip())
    assert payload["ok"] is False
    assert payload["error"] == "use either --link-current or --link-to, not both"


def test_codex_exec_has_no_default_deadline(monkeypatch, tmp_path):
    fake_codex = tmp_path / "codex"
    done_marker = tmp_path / "done"
    _fake_codex(fake_codex)
    monkeypatch.setenv("SERENA_FAKE_CODEX_DONE", str(done_marker))
    monkeypatch.setenv("SERENA_FAKE_CODEX_DELAY", "0.05")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda command: str(fake_codex) if command == "codex" else None,
    )

    timeout_param = next(param for param in cli.codex_exec.params if param.name == "timeout")
    assert timeout_param.default == 0

    result = CliRunner().invoke(
        cli.main,
        ["codex-exec", "--timeout", "0", "--cwd", str(tmp_path), "long task"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    assert payload["ok"] is True
    assert done_marker.read_text(encoding="utf-8") == "done"
