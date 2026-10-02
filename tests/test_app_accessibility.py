"""Launchers that make his Chromium and Electron apps publish their widgets."""

from __future__ import annotations

import json
import re

import pytest

from core import app_accessibility as a11y

EDGE = """[Desktop Entry]
Name=Microsoft Edge
Exec=/usr/bin/microsoft-edge-stable --max-old-space-size=2048 %U
Actions=new-window;

[Desktop Action new-window]
Name=New Window
Exec=/usr/bin/microsoft-edge-stable
"""
UNIFIED_AUTOSTART = """[Desktop Entry]
Name=Unified
Exec="/usr/lib/unified-inbox/unified" --login-launch
"""
ARGV = """// This configuration file allows you to pass permanent command line arguments to VS Code.
{
\t// Allows to disable crash reporting.
\t"enable-crash-reporter": true,

\t// Do not edit this value.
\t"crash-reporter-id": "40ee2a23"
}"""


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    local = home / ".local/share/applications"
    local.mkdir(parents=True)
    (local / "microsoft-edge.desktop").write_text(EDGE)
    (local / "msedge-abc-Default.desktop").write_text(
        '[Desktop Entry]\nExec=/opt/microsoft/msedge/microsoft-edge "--profile-directory=Profile 2" '
        "--app=https://x\n"
    )
    (local / "serena-desktop.desktop").write_text(
        "[Desktop Entry]\nExec=/home/r/Applications/Serena.AppImage %U\n"
    )
    (home / ".config/autostart").mkdir(parents=True)
    (home / ".config/autostart/unified.desktop").write_text(UNIFIED_AUTOSTART)
    (home / ".vscode").mkdir()
    (home / ".vscode/argv.json").write_text(ARGV)
    system = tmp_path / "system"
    system.mkdir()
    (system / "unified-inbox.desktop").write_text("[Desktop Entry]\nName=Unified\nExec=unified-inbox %U\n")
    return home, system


def _exec_lines(path):
    return [line for line in path.read_text().splitlines() if line.startswith("Exec=")]


def _argv(home):
    text = (home / ".vscode/argv.json").read_text()
    return json.loads("\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//")))


def test_apply_flags_every_launch_line_once_and_undo_restores_them_exactly(home):
    home, system = home
    before = {path: path.read_text() for path in home.rglob("*") if path.is_file()}
    results = a11y.apply(home, system)
    changes = {(row["app"], row["change"]) for row in results}
    assert ("Edge", "added") in changes and ("Unified", "created") in changes
    assert ("VS Code", "added") in changes
    edge = home / ".local/share/applications/microsoft-edge.desktop"
    assert _exec_lines(edge) == [
        "Exec=/usr/bin/microsoft-edge-stable --force-renderer-accessibility --max-old-space-size=2048 %U",
        "Exec=/usr/bin/microsoft-edge-stable --force-renderer-accessibility",
    ]
    assert _exec_lines(home / ".config/autostart/unified.desktop") == [
        'Exec="/usr/lib/unified-inbox/unified" --force-renderer-accessibility --login-launch'
    ]
    shadow = home / ".local/share/applications/unified-inbox.desktop"
    assert _exec_lines(shadow) == ["Exec=unified-inbox --force-renderer-accessibility %U"]
    assert _argv(home) == {
        "force-renderer-accessibility": True,
        "enable-crash-reporter": True,
        "crash-reporter-id": "40ee2a23",
    }
    assert all(row["flag"] for row in a11y.status(home, system))

    # Idempotent: an app update rewriting one launcher is fixed by re-running it.
    edge.write_text(EDGE)
    desktops = list(home.rglob("*.desktop"))
    a11y.apply(home, system)
    a11y.apply(home, system)
    for path in desktops:
        lines = _exec_lines(path)
        assert sum(line.split().count(a11y.FLAG) for line in lines) == len(lines)
    assert (home / ".vscode/argv.json").read_text().count('"force-renderer-accessibility"') == 1

    a11y.undo(home, system)
    after = {path: path.read_text() for path in home.rglob("*") if path.is_file()}
    assert after == before
    assert not shadow.exists()


def test_an_empty_argv_file_gains_the_key_without_a_trailing_comma(home):
    home, system = home
    (home / ".vscode/argv.json").write_text("{\n}\n")
    a11y.apply(home, system)
    assert _argv(home) == {"force-renderer-accessibility": True}
    a11y.undo(home, system)
    assert (home / ".vscode/argv.json").read_text() == "{\n}\n"


def test_a_deliberate_false_is_left_for_him(home):
    home, system = home
    (home / ".vscode/argv.json").write_text('{\n\t"force-renderer-accessibility": false\n}\n')
    with pytest.raises(ValueError, match="by hand"):
        a11y.apply(home, system)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("code %F", "code --force-renderer-accessibility %F"),
        ('"/a b/c" --x', '"/a b/c" --force-renderer-accessibility --x'),
        ("/usr/bin/app", "/usr/bin/app --force-renderer-accessibility"),
        ("app --force-renderer-accessibility", "app --force-renderer-accessibility"),
    ],
)
def test_the_flag_goes_right_after_the_executable(value, expected):
    assert a11y.with_flag(value) == expected
    assert re.sub(r"\s+", " ", a11y.without_flag(expected)) == re.sub(
        r"\s+", " ", value.replace(" --force-renderer-accessibility", "")
    )
