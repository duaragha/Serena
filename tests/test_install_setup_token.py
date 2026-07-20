from __future__ import annotations

import stat
from pathlib import Path

import pytest

from core.install_setup_token import install_token


def test_install_setup_token_is_private_and_preserves_other_settings(tmp_path: Path) -> None:
    target = tmp_path / "config" / "brain.env"
    target.parent.mkdir()
    target.write_text("SERENA_BRAIN_MODEL=sonnet\nOLD=value\n", encoding="utf-8")

    result = install_token("sk-ant-oat01-test-token", target)

    assert result == target
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text(encoding="utf-8") == (
        "SERENA_BRAIN_MODEL=sonnet\n"
        "OLD=value\n"
        "CLAUDE_CODE_OAUTH_TOKEN='sk-ant-oat01-test-token'\n"
    )


def test_install_setup_token_replaces_only_the_existing_assignment(tmp_path: Path) -> None:
    target = tmp_path / "brain.env"
    target.write_text(
        "CLAUDE_CODE_OAUTH_TOKEN='sk-ant-oat01-old'\nKEEP=yes\n",
        encoding="utf-8",
    )

    install_token("sk-ant-oat01-new", target)

    assert target.read_text(encoding="utf-8") == (
        "KEEP=yes\nCLAUDE_CODE_OAUTH_TOKEN='sk-ant-oat01-new'\n"
    )


def test_install_setup_token_rejects_non_setup_token(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sk-ant-oat01"):
        install_token("ordinary-api-key", tmp_path / "brain.env")
