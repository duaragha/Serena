"""The new-chat picker can select more than one agent.

One agent makes an ordinary chat. Two or three make a linked thread: every
pane spawned together in one split and linked as one thread once the real
sessions exist -- the front door's claude+codex spawn, generalised to whatever
was picked.

Three things had to give to make a trio work. The picker returned one agent.
The pending-partner map held one partner per pane, so a thread of three showed
two until the backend group existed. And the reconciler linked a pair the
moment two members resolved, which for a trio would have linked two and
stranded the third.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ui import web


def _extract(name: str) -> str:
    start = web.HTML.index(f"function {name}(")
    depth = 0
    for index in range(web.HTML.index("{", start), len(web.HTML)):
        char = web.HTML[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return web.HTML[start : index + 1]
    raise AssertionError(f"{name} is not brace-balanced")


HARNESS = r"""
'use strict';
const assert = require('node:assert/strict');
const _AGENT_PANE_ORDER = ['claude', 'codex', 'gemini'];
const _pendingTermPartners = new Map();
__FUNCTIONS__
const out = {};

// picker: toggling, ordering, and the floor of one
out.addCodex = _toggleAgentChoice(['claude'], 'codex');
out.addGeminiFirst = _toggleAgentChoice(['codex'], 'gemini');
out.outOfOrder = _toggleAgentChoice(['gemini', 'codex'], 'claude');
out.removeOne = _toggleAgentChoice(['claude', 'codex'], 'claude');
out.refuseLast = _toggleAgentChoice(['codex'], 'codex');
out.dedupe = _toggleAgentChoice(['claude', 'claude'], 'codex');

// partners: one sid or a list, read the same way
_setPendingPartners('a', ['b']);
out.pairShape = _pendingTermPartners.get('a');
out.pairRead = _pendingPartnersOf('a');
_setPendingPartners('a', ['b', 'c']);
out.trioShape = _pendingTermPartners.get('a');
out.trioRead = _pendingPartnersOf('a');
_setPendingPartners('a', ['a', 'b', 'b', '', null]);
out.selfAndDupesDropped = _pendingPartnersOf('a');
_setPendingPartners('a', []);
out.emptyClears = _pendingTermPartners.has('a');
out.unknownRead = _pendingPartnersOf('nobody');
// a legacy single-sid entry written directly by the bridge still reads
_pendingTermPartners.set('legacy', 'other');
out.legacyRead = _pendingPartnersOf('legacy');

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def run():
    if shutil.which("node") is None:
        pytest.skip("node is required to run page code")
    body = "\n".join(_extract(n) for n in ("_toggleAgentChoice", "_pendingPartnersOf", "_setPendingPartners"))
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        script = Path(d) / "picker.cjs"
        script.write_text(HARNESS.replace("__FUNCTIONS__", body), encoding="utf-8")
        done = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60,
                              env={"PATH": "/usr/bin:/bin:/usr/local/bin"})
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout)


def test_selecting_a_second_agent_keeps_the_first(run) -> None:
    assert run["addCodex"] == ["claude", "codex"]


def test_the_selection_is_always_in_pane_order(run) -> None:
    """The thread should read the way the split lays it out."""
    assert run["addGeminiFirst"] == ["codex", "gemini"]
    assert run["outOfOrder"] == ["claude", "codex", "gemini"]


def test_an_agent_can_be_deselected_but_not_the_last_one(run) -> None:
    assert run["removeOne"] == ["codex"]
    assert run["refuseLast"] == ["codex"], "the picker let the selection go empty"
    assert run["dedupe"] == ["claude", "codex"]


def test_pending_partners_hold_one_or_many_and_read_the_same(run) -> None:
    assert run["pairShape"] == "b", "a pair should keep the bridge's single-sid shape"
    assert run["pairRead"] == ["b"]
    assert run["trioShape"] == ["b", "c"]
    assert run["trioRead"] == ["b", "c"]
    assert run["selfAndDupesDropped"] == ["b"]
    assert run["emptyClears"] is False
    assert run["unknownRead"] == []
    assert run["legacyRead"] == ["other"]


def test_the_picker_returns_every_agent_chosen() -> None:
    page = web.HTML
    assert "resolve({ value: result, agent: chosenAgents[0], agents: chosenAgents.slice() })" in page


def test_a_pseudo_knows_how_big_its_thread_is() -> None:
    page = web.HTML
    assert "function _fdPseudo(agent, cwd, label, pairId, pairSize)" in page
    assert "fd_pair_size: pairId ? (pairSize || 2) : null" in page


def test_the_reconciler_waits_for_the_whole_thread_before_linking() -> None:
    """Linking at two would have paired two of a trio and stranded the third."""
    page = web.HTML
    assert "if (bucket.length >= (pseudo.fd_pair_size || 2))" in page
    assert "if (bucket.length === 2)" not in page


def test_more_than_one_agent_takes_the_linked_path() -> None:
    page = web.HTML
    start = page.index("async function newChatInline(")
    body = page[start : page.index("let _lastNewChatAgent = 'claude';", start)]
    assert "if (agents.length > 1) {" in body
    assert "await newLinkedChatInline(agents, cwdOverride, typedTitle);" in body


def test_the_linked_path_spawns_all_but_the_lead_in_the_background() -> None:
    body = _extract("newLinkedChatInline")
    assert "for (const p of pseudos.slice(1))" in body
    assert "background: true" in body
    lead = body.index("const leadRuntime = await startLiveTerminal(lead.session_id")
    assert body.index("background: true") < lead, "the lead must start last, so it takes focus"
    assert "_activateTermPane(lead.session_id)" in body
    assert "pseudos.forEach(p => _setPendingPartners(p.session_id, sids))" in body
    assert "pseudo.pending_rename_title = typedTitle || null" in body


def test_every_pending_partner_consumer_reads_through_the_helper() -> None:
    """A direct .get() would see an array and treat it as one sid."""
    page = web.HTML
    direct = [m.start() for m in re.finditer(r"_pendingTermPartners\.get\(", page)]
    # Only the helper itself may call .get().
    helper = _extract("_pendingPartnersOf")
    assert len(direct) == helper.count("_pendingTermPartners.get("), (
        "something reads _pendingTermPartners directly again"
    )
