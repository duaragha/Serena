"""A brand-new chat must render as ONE row, not two.

Creating a chat adds a placeholder row immediately, then the reconciler adopts
the real session the terminal writes and drops the placeholder. It matches them
by cwd. When the user picked no project, the placeholder recorded an empty cwd
while Python spawned the terminal in $HOME, so the two could never match: the
placeholder stranded under Active terminals keeping the typed title, and the
real session appeared again under Today with an auto-generated title.

These lock the fix from both ends. The server has to expose the directory it
would really spawn in, and the client has to resolve a cwd-less placeholder
through that value in both places it matches sessions.
"""

from __future__ import annotations

import re
from pathlib import Path

from ui import web

REPO = Path(__file__).resolve().parents[1]
WEB_SOURCE = (REPO / "ui" / "web.py").read_text(encoding="utf-8")


def test_default_cwd_endpoint_reports_the_real_spawn_directory():
    response = web.app.test_client().get("/api/default-cwd")

    assert response.status_code == 200
    payload = response.get_json()
    # Whatever Python would resolve an empty cwd to before spawning is exactly
    # what the client must record on the placeholder.
    assert payload["cwd"] == web.resolve_session_cwd("")
    assert payload["cwd"] == str(Path.home())
    assert payload["cwd"]


def test_a_new_chat_without_a_project_records_the_spawn_directory():
    """The placeholder must not store '' while the terminal starts in $HOME."""

    block = re.search(
        r"const cwd = \(cwdOverride !== undefined && cwdOverride !== null\)"
        r"(.{0,220}?)const shortProj",
        WEB_SOURCE,
        re.DOTALL,
    )
    assert block is not None, "the new-chat cwd assignment moved"
    assignment = block.group(1)

    assert "_defaultCwd()" in assignment, (
        "a new chat with no project selected must fall back to the server's "
        "spawn directory, not to an empty cwd the reconciler can never match"
    )
    assert not re.search(r":\s*\(currentProjectCwd \|\| ''\)", assignment)


def test_both_matchers_resolve_a_placeholder_that_has_no_cwd():
    """The reconciler and the /clear migration must agree on the fallback."""

    reconciler = re.search(
        r"const pseudoCwd = _normCwd\(pseudo\.cwd\) \|\| _normCwd\(_defaultCwd\(\)\);"
        r"(.{0,400}?)\);",
        WEB_SOURCE,
        re.DOTALL,
    )
    assert reconciler is not None, "the reconciler no longer resolves an empty placeholder cwd"
    assert "_normCwd(s.cwd) === pseudoCwd" in reconciler.group(1)

    migration = re.search(
        r"const pseudoCwd = pseudo\.cwd \|\| _defaultCwd\(\);(.{0,300}?)s\.cwd === pseudoCwd",
        WEB_SOURCE,
        re.DOTALL,
    )
    assert migration is not None, (
        "the /clear migration must resolve a cwd-less placeholder too, or an "
        "unrelated active terminal steals the session that placeholder created"
    )


def test_the_default_cwd_cache_is_warmed_before_any_placeholder_exists():
    """Both matchers read the cache synchronously, so it must load at boot."""

    boot = re.search(
        r"_initAgentFilterIcons\(\);(.{0,400}?)loadSessions\(\);",
        WEB_SOURCE,
        re.DOTALL,
    )
    assert boot is not None, "the startup sequence moved"
    assert "_loadDefaultCwd();" in boot.group(1)
