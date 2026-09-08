import os
from pathlib import Path

from core import gemini_usage_reader as reader


def test_service_path_finds_user_install(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "system-bin"))
    binary = tmp_path / ".local" / "bin" / ("agy.exe" if os.name == "nt" else "agy")
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    assert reader._binary() == str(binary)


def test_path_install_has_precedence(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(reader.shutil, "which", lambda name, **kwargs: "/custom/agy" if name == "agy" else None)
    assert reader._binary() == "/custom/agy"


def test_missing_install_is_not_invented(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert reader._binary() is None


def test_usage_failure_is_not_shown_as_waiting():
    page = (Path(__file__).resolve().parents[1] / "ui" / "web.py").read_text()
    assert "svc.reason ? 'unavailable' : 'waiting'" in page
    assert "esc(svc.reason || '')" in page
