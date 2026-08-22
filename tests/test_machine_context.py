"""Every agent must learn which machine it is on before it runs a command.

Raghav works the same repos from a Linux laptop and a Windows PC, and the paths
differ. Nothing announced that, so a chat resumed on the other machine would run
Linux paths on Windows and fail on its first command. This hook answers the
question before any work starts.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from core import machine_context

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "core" / "machine_context.py"


def test_it_reports_this_machine_and_the_paths_that_matter():
    facts = machine_context.describe()

    assert facts["machine"], "the machine must be named"
    assert facts["os"]
    # The repo path has to be real, since agents are told to use it.
    assert Path(facts["serena_repo"]).is_dir()
    assert (Path(facts["serena_repo"]) / "cli.py").is_file()


def test_the_projects_root_is_derived_from_the_repo_not_guessed():
    """The PC has a nearly empty Documents/Projects beside the real root.

    Guessing by folder name reported the wrong one, so the answer comes from
    where this checkout actually sits.
    """

    facts = machine_context.describe()
    assert Path(facts["serena_repo"]).parent == Path(facts["projects_root"])


def test_the_banner_names_the_operating_system_convention():
    banner = machine_context.banner()

    assert "[machine]" in banner
    if sys.platform.startswith("win"):
        assert "C:\\Users\\ragha" in banner
        assert "Do not assume Linux paths" in banner
    else:
        assert "/home/raghav" in banner


def test_the_hook_emits_the_contract_claude_expects():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input="",
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    hook = payload["hookSpecificOutput"]
    assert hook["hookEventName"] == "SessionStart"
    assert "[machine]" in hook["additionalContext"]


def test_text_mode_is_plain_for_codex():
    """Codex has no hook; it runs this and reads the output directly."""

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--text"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("[machine]")
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def test_the_codex_instructions_no_longer_hardcode_one_machines_paths():
    agents = Path.home() / ".codex" / "AGENTS.md"
    if not agents.is_file():
        pytest.skip("codex is not configured on this machine")

    text = agents.read_text(encoding="utf-8")
    assert "machine_context.py --text" in text, "codex must be told to locate itself"
    # The synced file cannot claim the laptop's paths are the truth.
    assert "- `/home/raghav/Documents/Projects/serena/Persona.md`" not in text


def test_the_window_reports_its_host_machine_to_the_page():
    """The same answer agents get from the hook, visible in the header."""

    from ui import web

    facts = web._host_machine()
    assert facts["os"], "the page must be able to name the OS"

    body = web.app.test_client().get("/").get_data(as_text=True)
    assert '"machine":' in body.replace(" ", "")
    # A badge with no renderer, or a renderer never called, shows nothing.
    assert "_machineBadge" in body
    assert "root.appendChild(_machineBadge())" in body


def test_the_host_badge_survives_a_broken_machine_lookup(monkeypatch):
    """A cosmetic badge must never be able to break the page."""

    import core.machine_context as machine_context
    from ui import web

    monkeypatch.setattr(
        machine_context, "describe", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    facts = web._host_machine()

    assert facts["os"], "the fallback must still name an OS"
    assert facts["name"] == ""
