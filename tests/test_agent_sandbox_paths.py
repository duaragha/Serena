"""Agents Serena spawns must not be fenced out of their own files.

The units that serve the chat UI used to run ProtectHome=read-only with a
whitelist of Serena's own state directories. Claude writes transcripts under
~/.claude/projects and Codex writes rollouts under ~/.codex/sessions, so every
agent started from those surfaces did its work and then silently failed to
record any of it. The whitelist had even grown a hand-added carve-out for one
~/.claude/projects subfolder: the same bug, found once, patched for one chat.

A whitelist could only ever list the paths someone had already been bitten by,
so the filesystem sandbox is gone rather than extended. ProtectControlGroups is
out too, because idle panes reclaim their own memory through cgroup knobs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SYSTEMD = REPO / "systemd"

# Units whose process tree can spawn a claude or codex CLI.
AGENT_SPAWNING_UNITS = (
    "serena-mobile-host.service",
    "serena-desk.service",
)

# Directives that stop an agent reading or writing what it legitimately needs.
BLOCKING_DIRECTIVES = (
    "ProtectSystem=",
    "ProtectHome=",
    "ReadWritePaths=",
    "PrivateTmp=",
    "ProtectControlGroups=",
)


def _unit(name: str) -> Path:
    unit = SYSTEMD / name
    if not unit.is_file():
        pytest.skip(f"{name} is not part of this checkout")
    return unit


@pytest.mark.parametrize("name", AGENT_SPAWNING_UNITS)
def test_no_filesystem_sandbox_fences_the_agents(name):
    text = _unit(name).read_text(encoding="utf-8")

    for directive in BLOCKING_DIRECTIVES:
        offenders = [
            line
            for line in text.splitlines()
            if line.strip().startswith(directive)
        ]
        assert not offenders, (
            f"{name} reintroduces {directive.rstrip('=')}, which is how agents "
            f"lost the ability to save their own transcripts: {offenders}"
        )


@pytest.mark.parametrize("name", AGENT_SPAWNING_UNITS)
def test_kernel_hardening_that_costs_nothing_is_kept(name):
    """Dropping the fence is not a reason to drop everything else."""

    text = _unit(name).read_text(encoding="utf-8")

    for directive in (
        "NoNewPrivileges=true",
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
    ):
        assert directive in text, f"{name} lost {directive}"


@pytest.mark.parametrize("name", AGENT_SPAWNING_UNITS)
def test_the_unit_still_starts_the_right_thing(name):
    text = _unit(name).read_text(encoding="utf-8")

    assert "ExecStart=" in text
    assert "Restart=" in text
