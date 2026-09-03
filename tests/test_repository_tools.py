from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from scripts.backup import create_snapshot
from scripts.bootstrap import install_services, run_doctor
from scripts.restore import restore_snapshot
from scripts.runtime_manifest import load_manifest


def _manifest(repo: Path, *, runtime: str = "~/.config/serena") -> Path:
    config = repo / "config"
    config.mkdir(parents=True)
    data = {
        "schema_version": 1,
        "project": {"name": "serena", "repository": str(repo), "python": ">=3.10"},
        "portable_source": ["README.md"],
        "private_repository_paths": ["Persona.md", "memory"],
        "runtime_paths": [runtime],
        "session_paths": ["~/.claude/projects"],
        "auth_paths": ["~/.claude/.credentials.json"],
        "model_paths": ["voice/models", "~/.config/serena/models"],
        "rebuildable_paths": [".venv"],
        "sensitive_names": ["*.env", "chat_token", "auth.json"],
        "transient_names": ["*.lock", "*.sqlite3-wal", "__pycache__"],
        "services": {
            "always_on": ["serena-brain.service"],
            "activation_only": [],
            "acceptance_only": [],
            "forbidden_without_authorization": ["serena-brain-soak.service"],
        },
        "required_commands": ["python3"],
        "required_python_imports": ["json"],
    }
    path = config / "runtime-manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_runtime_manifest_loads_current_contract():
    manifest = load_manifest()
    assert manifest["schema_version"] == 1
    assert "serena-brain-soak.service" in manifest["services"]["forbidden_without_authorization"]
    assert "voice/desk" in manifest["portable_source"]


def test_backup_excludes_secrets_and_models_then_restores(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (repo / "README.md").write_text("source", encoding="utf-8")
    (repo / "Persona.md").write_text("private persona", encoding="utf-8")
    (repo / "memory").mkdir()
    (repo / "memory" / "fact.md").write_text("remember", encoding="utf-8")
    (repo / "voice" / "models").mkdir(parents=True)
    (repo / "voice" / "models" / "large.bin").write_bytes(b"model")

    runtime = home / ".config" / "serena"
    runtime.mkdir(parents=True)
    (runtime / "brain.env").write_text("TOKEN=secret", encoding="utf-8")
    (runtime / "state.json").write_text("{}", encoding="utf-8")
    database = runtime / "work.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("create table jobs (id integer primary key)")
        connection.execute("insert into jobs default values")

    manifest = _manifest(repo)
    result = create_snapshot(
        manifest_path=manifest,
        output_root=tmp_path / "backups",
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    assert result["files"] == 4
    assert result["skipped_sensitive"] == 1
    assert result["skipped_models"] == 0
    snapshot = Path(result["snapshot"])
    assert (snapshot / "snapshot.json").is_file()

    restored_repo = tmp_path / "restored-repo"
    restored_home = tmp_path / "restored-home"
    restored_repo.mkdir()
    restored_home.mkdir()
    restore_result = restore_snapshot(
        snapshot,
        repo_root=restored_repo,
        home_root=restored_home,
        apply=True,
    )
    assert restore_result["ok"] is True
    assert (restored_repo / "Persona.md").read_text(encoding="utf-8") == "private persona"
    with sqlite3.connect(restored_home / ".config" / "serena" / "work.sqlite3") as connection:
        assert connection.execute("select count(*) from jobs").fetchone()[0] == 1
    assert not (restored_home / ".config" / "serena" / "brain.env").exists()


def test_source_only_doctor_and_service_plan(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (repo / "README.md").write_text("source", encoding="utf-8")
    (repo / "systemd").mkdir()
    (repo / "systemd" / "serena-brain.service").write_text(
        "[Service]\nExecStart=true\n", encoding="utf-8"
    )
    manifest = _manifest(repo, runtime=str(tmp_path / "runtime"))

    doctor = run_doctor(manifest_path=manifest, repo_root=repo, source_only=True)
    assert doctor["ok"] is True
    plan = install_services(manifest_path=manifest, repo_root=repo, apply=False)
    assert plan["applied"] is False
    assert plan["changes"] == [
        {
            "unit": "serena-brain.service",
            "action": "link",
            "target": str(repo / "systemd" / "serena-brain.service"),
        }
    ]
