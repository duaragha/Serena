from pathlib import Path

from core.frontdoor import _valid_spawn


def test_spawn_validation_accepts_posix_absolute_path(monkeypatch):
    cwd = str(Path(__file__).resolve().parents[1])
    monkeypatch.setattr("core.frontdoor._spawn_cwd_allowed", lambda value: value == cwd)
    spawn = {"agents": ["claude"], "cwd": cwd, "seed": "go"}
    assert _valid_spawn(spawn)
    assert spawn["cwd"] == cwd


def test_spawn_validation_accepts_windows_absolute_path(monkeypatch):
    monkeypatch.setattr("core.frontdoor.os.path.isdir", lambda value: True)
    spawn = {
        "agents": ["codex"],
        "cwd": r"C:\Users\ragha\Projects\serena",
        "seed": "go",
    }
    assert _valid_spawn(spawn)
    assert spawn["cwd"] == r"C:\Users\ragha\Projects\serena"


def test_spawn_validation_rejects_relative_path():
    spawn = {"agents": ["claude"], "cwd": "projects/serena", "seed": "go"}
    assert not _valid_spawn(spawn)


def test_spawn_validation_rejects_missing_directory(monkeypatch):
    monkeypatch.setattr("core.frontdoor.os.path.isdir", lambda value: False)
    spawn = {"agents": ["claude"], "cwd": "/missing/project", "seed": "go"}
    assert not _valid_spawn(spawn)


def test_spawn_validation_rejects_empty_seed(monkeypatch):
    monkeypatch.setattr("core.frontdoor.os.path.isdir", lambda value: True)
    spawn = {"agents": ["claude"], "cwd": "/home/raghav", "seed": "  "}
    assert not _valid_spawn(spawn)


def test_spawn_validation_rejects_duplicate_or_reordered_agents(monkeypatch):
    monkeypatch.setattr("core.frontdoor.os.path.isdir", lambda value: True)

    assert not _valid_spawn({"agents": ["claude", "claude"], "cwd": "/home/raghav", "seed": "go"})
    assert not _valid_spawn({"agents": ["codex", "claude"], "cwd": "/home/raghav", "seed": "go"})


def test_spawn_validation_rejects_filesystem_root(monkeypatch):
    monkeypatch.setattr("core.frontdoor.os.path.isdir", lambda value: True)
    spawn = {"agents": ["codex"], "cwd": "/", "seed": "brief"}

    assert not _valid_spawn(spawn)


def test_spawn_validation_rejects_path_outside_home_project_tree(monkeypatch):
    monkeypatch.setattr("core.frontdoor.os.path.isdir", lambda value: True)
    spawn = {"agents": ["codex"], "cwd": "/etc", "seed": "brief"}

    assert not _valid_spawn(spawn)
