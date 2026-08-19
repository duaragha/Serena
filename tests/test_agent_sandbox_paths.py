"""An agent Serena spawns must be able to save its own session.

The units that serve the chat UI run with ProtectHome=read-only and a narrow
ReadWritePaths whitelist. Claude writes transcripts under ~/.claude/projects and
Codex writes rollouts under ~/.codex/sessions, and neither was on that list, so
every agent started from those surfaces did its work and then silently failed to
record any of it. The whitelist even carried a hand-added carve-out for a single
~/.claude/projects subfolder, which is the same bug patched once for one case.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SYSTEMD = REPO / "systemd"

# Units whose process tree can spawn a claude or codex CLI.
AGENT_SPAWNING_UNITS = (
    "serena-mobile-host.service",
    "serena-desk.service",
)


def _read_write_paths(unit: Path) -> str:
    for line in unit.read_text(encoding="utf-8").splitlines():
        if line.startswith("ReadWritePaths="):
            return line
    return ""


@pytest.mark.parametrize("name", AGENT_SPAWNING_UNITS)
def test_agent_state_directories_are_writable(name):
    unit = SYSTEMD / name
    if not unit.is_file():
        pytest.skip(f"{name} is not part of this checkout")

    paths = _read_write_paths(unit)
    assert paths, f"{name} declares no ReadWritePaths"
    assert "%h/.claude" in paths, f"{name} cannot write Claude transcripts"
    assert "%h/.codex" in paths, f"{name} cannot write Codex rollouts"


@pytest.mark.parametrize("name", AGENT_SPAWNING_UNITS)
def test_no_single_project_carve_outs_remain(name):
    """A per-project exception only ever fixes the one chat someone noticed."""

    unit = SYSTEMD / name
    if not unit.is_file():
        pytest.skip(f"{name} is not part of this checkout")

    paths = _read_write_paths(unit)
    assert not re.search(r"%h/\.claude/projects/\S+", paths), (
        "a single project path cannot cover transcripts, which are written per "
        "working directory; whitelist %h/.claude instead"
    )


@pytest.mark.parametrize("name", AGENT_SPAWNING_UNITS)
def test_the_sandbox_is_still_a_sandbox(name):
    """Fixing the hole must not turn the whole home directory writable."""

    unit = SYSTEMD / name
    if not unit.is_file():
        pytest.skip(f"{name} is not part of this checkout")

    text = unit.read_text(encoding="utf-8")
    assert "ProtectSystem=strict" in text
    assert "ProtectHome=read-only" in text
    paths = _read_write_paths(unit)
    assert paths.strip() != "ReadWritePaths=%h", "that would remove the sandbox entirely"
