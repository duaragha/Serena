"""Proof prerequisites must fail before accessing authentication or starting agents."""

import asyncio
import importlib.util
from pathlib import Path

import pytest


def test_claude_relative_sdk_is_resolved_before_authentication(monkeypatch, tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/verify-workspace-claude-roundtrip.py"
    spec = importlib.util.spec_from_file_location("claude_roundtrip_proof", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    # No executable means no authentication access or child process is possible.
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError):
        asyncio.run(module.main(typescript_sdk="missing-sdk.mjs"))
    (tmp_path / "sdk.mjs").write_text("export {};")
    with pytest.raises(RuntimeError, match="Installed Claude unavailable"):
        asyncio.run(module.main(typescript_sdk="sdk.mjs"))
