from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.workspace_catalog import register_fork


@pytest.mark.parametrize("case", ["missing", "ambiguous", "wrong-project", "wrong-id", "outside"])
def test_invalid_native_fork_never_writes_catalog(tmp_path, monkeypatch, case):
    sid = str(uuid4())
    config = tmp_path / "config"
    projects = config / "projects"
    path = projects / "project" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True)
    if case != "missing":
        path.write_text("{}\n")
    if case == "ambiguous":
        other = projects / "other" / path.name
        other.parent.mkdir()
        other.write_text("{}\n")
    if case == "outside":
        outside = tmp_path / "external.jsonl"
        outside.write_text("{}\n")
        path.unlink()
        path.symlink_to(outside)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setattr("core.parser.parse_metadata", lambda *args: SimpleNamespace(
        session_id=str(uuid4()) if case == "wrong-id" else sid,
        cwd=str(tmp_path / "wrong") if case == "wrong-project" else str(tmp_path),
    ))
    monkeypatch.setattr("core.indexer._get_db", lambda: pytest.fail("Invalid fork reached catalog write"))
    with pytest.raises(ValueError):
        register_fork({"session_id": sid, "provider": "claude", "cwd": str(tmp_path)})
