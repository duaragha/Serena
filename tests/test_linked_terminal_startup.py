"""Linked membership must not depend on which terminals have already spawned."""

import json
import shutil
import subprocess

import pytest

from core import gemini_scanner
from ui import web


def _function(name):
    start = web.HTML.index(f"function {name}(")
    end = web.HTML.index("\n}", start) + 2
    return web.HTML[start:end]


@pytest.mark.skipif(shutil.which("node") is None, reason="node required")
@pytest.mark.parametrize("entry", ["claude", "codex", "gemini"])
def test_opening_any_member_starts_all_three_once(entry):
    functions = "\n".join(_function(name) for name in (
        "_agentOf", "_linkedGroupSids", "_startLinkedTerminals",
    ))
    script = r"""
const assert = require('node:assert/strict');
const _AGENT_QUAD_ORDER = ['claude', 'codex', 'gemini'];
const sessionSource = _AGENT_QUAD_ORDER.map(agent => ({session_id:agent, agent, group:'group'}));
sessionSource.push({session_id:'worker', agent:'codex', group:'group', fleet_worker:true});
sessionSource.push({session_id:'external', agent:'codex', group:'group', external_runtime_active:true});
sessionSource.push({session_id:'solo', agent:'gemini'});
const sessions = sessionSource;
const _pendingTermPartners = new Map();
const _termStarting = new Set();
const termSessions = new Map();
const calls = [];
function _findClientSession(sid) { return sessionSource.find(s => s.session_id === sid); }
function _siblingsInGroup(group, sid) {
  return sessionSource.filter(s => s.group === group && s.session_id !== sid);
}
function startLiveTerminal(sid, opts) { calls.push([sid,opts]); _termStarting.add(sid); }
__FUNCTIONS__
const entry = __ENTRY__;
assert.deepEqual(_linkedGroupSids(entry), [entry]);
assert.deepEqual(_linkedGroupSids(entry, {liveOnly:false}), _AGENT_QUAD_ORDER);
_startLinkedTerminals(entry);
_startLinkedTerminals(entry);
assert.deepEqual(calls.map(c => c[0]), _AGENT_QUAD_ORDER.filter(s => s !== entry));
assert(calls.every(c => c[1].background));
for (const id of _AGENT_QUAD_ORDER) termSessions.set(id, {agent:id});
assert.deepEqual(_linkedGroupSids(entry), _AGENT_QUAD_ORDER);
_startLinkedTerminals(entry);
assert.equal(calls.length, 2);
_startLinkedTerminals('solo');
assert.equal(calls.length, 2);
assert.deepEqual(_linkedGroupSids('solo'), ['solo']);
// The two-agent path still works; an unrelated open Gemini pane is excluded.
sessionSource.find(s => s.session_id === 'gemini').group = 'other';
assert.deepEqual(_linkedGroupSids('claude'), ['claude', 'codex']);
""".replace("__FUNCTIONS__", functions).replace("__ENTRY__", json.dumps(entry))
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_all_startup_and_socket_paths_use_group_membership():
    assert "_startLinkedTerminals(currentSessionId)" in _function("setConvMode")
    source = web.HTML[web.HTML.index("async function startLiveTerminal("):]
    source = source[:source.index("\nasync function ")]
    assert "_startLinkedTerminals(sid)" in source
    assert source.index("_startLinkedTerminals(sid)") < source.index("await _spawnTerminalRequest(body)")
    assert "_linkedGroupSids(activeTermSid).includes(state.sid)" in source
    assert "_linkedSiblingSid(" not in source


@pytest.mark.skipif(shutil.which("node") is None, reason="node required")
def test_background_failure_names_its_session_without_overwriting_foreground():
    script = r"""
const assert = require('node:assert/strict');
let currentSessionId = 'claude', activeTermSid = 'claude';
const toasts = [], statuses = [];
function _agentOf(sid) { return sid; }
function _linkedGroupSids(sid) { return [sid]; }
function showToast(text) { toasts.push(text); }
function setTermStatus(text) { statuses.push(text); }
__FUNCTIONS__
_reportTerminalStartFailure('gemini', {background:true}, 'native conversation missing');
assert.deepEqual(toasts, ['Gemini gemini: native conversation missing']);
assert.deepEqual(statuses, []);
_reportTerminalStartFailure('gemini', {}, 'stale request');
assert.deepEqual(statuses, []);
currentSessionId = 'gemini';
_reportTerminalStartFailure('gemini', {}, 'not found');
assert.deepEqual(statuses, ['Gemini gemini: not found']);
""".replace("__FUNCTIONS__", _function("_agentLabel") + "\n" + _function("_reportTerminalStartFailure"))
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("native", [".db", ".pb", None])
@pytest.mark.parametrize("existing", [False, True])
def test_gemini_resumes_exact_native_session_or_reports_unavailable(
    tmp_path, monkeypatch, native, existing,
):
    sid = "9984a527-b5fa-4226-a9e3-e55661c8d9f1"
    monkeypatch.setattr(gemini_scanner, "CONVERSATIONS_DIR", tmp_path)
    if native:
        (tmp_path / (sid + native)).touch()
    monkeypatch.setattr(web, "get_session", lambda _: {
        "session_id": sid, "agent": "gemini", "cwd": str(tmp_path),
    })
    monkeypatch.setattr(web, "ensure_session_visible", lambda *a: None)
    monkeypatch.setattr(web, "_fleet_worker_marker", lambda _: False)
    monkeypatch.setattr(web, "_external_runtime_active", lambda _: False)
    monkeypatch.setattr(web.shutil, "which", lambda _: "/bin/agy")
    monkeypatch.setattr(web.pty_terminal, "tid_for_session", lambda _: "existing" if existing else None)
    registered = []
    monkeypatch.setattr(web.pty_terminal, "register_session", lambda *a: registered.append(a))
    spawned = []

    def spawn(argv, **kwargs):
        spawned.append((argv, kwargs))
        return "test-terminal"

    monkeypatch.setattr(web, "_spawn_terminal_with_recovery", spawn)
    response = web.app.test_client().post("/api/spawn-terminal", json={"session_id": sid})
    data = response.get_json()
    if existing:
        assert response.status_code == 200 and data["reused"]
        assert not spawned and not registered
    elif native:
        assert response.status_code == 200 and data["ok"]
        assert spawned[0][0][-2:] == ["--conversation", sid]
        assert spawned[0][1]["session_id"] == sid
        assert registered == [(sid, "test-terminal")]
    else:
        assert response.status_code == 409
        assert "native conversation file is missing" in data["error"]
        assert not spawned and not registered
