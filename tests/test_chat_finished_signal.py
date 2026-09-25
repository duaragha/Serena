"""A finished chat reaches the app that shows it, once, under its own name."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import cli
from core import chat_attention


@pytest.fixture(autouse=True)
def fresh_attention(monkeypatch):
    monkeypatch.setattr(chat_attention, "_ATTENTION", {})
    monkeypatch.setattr(chat_attention, "_EVENTS", chat_attention.deque(maxlen=200))
    monkeypatch.setattr(chat_attention, "_LAST_EVENT_AT", {})
    monkeypatch.setattr(chat_attention, "_seq", 0)
    monkeypatch.setattr(chat_attention, "_group_siblings", lambda sid: {sid})
    # mark() also settles voice work aimed at the chat; keep his inbox out of it.
    from core import voice_inbox

    monkeypatch.setattr(voice_inbox, "get_default_voice_inbox",
                        lambda: SimpleNamespace(finish_work_target=lambda sid: 0))


# ── which processes are Serena ─────────────────────────────────────────────

@pytest.mark.parametrize("args", [
    # Packaged Electron sidecars, Linux and Windows.
    ["/tmp/.mount_SerenaX/resources/sidecar/serena-web-sidecar", "--host", "127.0.0.1", "--port", "39701"],
    [r"C:\Users\ragha\AppData\Local\Programs\Serena Dev\resources\sidecar\serena-web-sidecar.exe", "--port", "5"],
    # Unpackaged `npm start`, and the phone host, run the script itself.
    ["/home/r/serena/.venv/bin/python", "/home/r/serena/apps/desktop/sidecar.py", "--port", "8767"],
    [r"C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe", r"C:\Users\ragha\Projects\serena\apps\desktop\sidecar.py"],
    # GTK-era entry points still count.
    ["python", "/home/r/chats/cli.py", "desktop"],
])
def test_every_serena_backend_is_recognised(args):
    assert cli._is_serena_backend_cmdline(args)


@pytest.mark.parametrize("args", [
    ["/home/r/serena/.venv/bin/python3", "-m", "core.brain_daemon"],
    ["/usr/bin/python3", "/home/r/frameworth/fw-board/serve.py"],
    ["node", "/home/r/app/sidecar.py.js"],
])
def test_other_local_servers_are_not(args):
    assert not cli._is_serena_backend_cmdline(args)


def test_every_running_serena_is_found_newest_first(monkeypatch):
    import psutil

    procs = {
        10: (["/opt/Serena/resources/sidecar/serena-web-sidecar", "--port", "39701"], 200.0),
        11: (["/venv/python", "/repo/apps/desktop/sidecar.py", "--port", "8767"], 100.0),
        12: (["/usr/bin/python3", "/x/serve.py"], 300.0),
    }

    def conn(pid, port, ip="127.0.0.1", status=psutil.CONN_LISTEN):
        return SimpleNamespace(pid=pid, status=status, laddr=SimpleNamespace(ip=ip, port=port))

    class Proc:
        def __init__(self, pid):
            self.pid = pid

        def cmdline(self):
            return procs[self.pid][0]

        def create_time(self):
            return procs[self.pid][1]

    monkeypatch.setattr(psutil, "net_connections", lambda kind: [
        conn(11, 8767), conn(10, 39701), conn(12, 8791),
        conn(10, 39702, ip="0.0.0.0"), conn(10, 40000, status="ESTABLISHED"),
    ])
    monkeypatch.setattr(psutil, "Process", Proc)
    assert cli._detect_serena_ports() == [39701, 8767]


# ── the Stop hook ───────────────────────────────────────────────────────────

def _run_mark_done(monkeypatch, env, ports=(39701, 41000)):
    posted = []

    def urlopen(req, timeout):
        posted.append((req.full_url, json.loads(req.data)))
        return SimpleNamespace(read=lambda: b"{}")

    monkeypatch.setattr(cli, "_detect_serena_ports", lambda: list(ports))
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    result = CliRunner().invoke(cli.main, ["mark-done"], env=env)
    assert result.exit_code == 0, result.output
    return sorted(posted)


def test_stop_hook_tells_every_running_serena(monkeypatch):
    posted = _run_mark_done(monkeypatch, {
        "CLAUDE_CODE_SESSION_ID": "abc", "CLAUDE_CODE_SESSION_ATTENDED": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli", "SERENA_FLEET_WORKER": "",
    })
    assert posted == [
        ("http://127.0.0.1:39701/api/chat-finished", {"session_id": "abc"}),
        ("http://127.0.0.1:41000/api/chat-finished", {"session_id": "abc"}),
    ]


@pytest.mark.parametrize("env", [
    {"CLAUDE_CODE_SESSION_ATTENDED": "0"},   # claude -p: titles, journal, jobs
    {"CLAUDE_CODE_ENTRYPOINT": "sdk-ts"},     # SDK panes report through the host
    {"SERENA_FLEET_WORKER": "1"},
])
def test_headless_claude_runs_are_not_announced(monkeypatch, env):
    base = {"CLAUDE_CODE_SESSION_ID": "abc", "CLAUDE_CODE_SESSION_ATTENDED": "1",
            "CLAUDE_CODE_ENTRYPOINT": "cli", "SERENA_FLEET_WORKER": ""}
    assert _run_mark_done(monkeypatch, {**base, **env}) == []


# ── finish events ───────────────────────────────────────────────────────────

def test_a_page_without_a_cursor_gets_no_backlog():
    chat_attention.mark("old")
    events, seq = chat_attention.events_since(None)
    assert events == [] and seq == 1
    chat_attention.mark("new")
    events, seq = chat_attention.events_since(seq)
    assert [e["sid"] for e in events] == ["new"] and seq == 2


def test_the_event_names_the_chat_that_finished_not_its_partners(monkeypatch):
    monkeypatch.setattr(chat_attention, "_group_siblings", lambda sid: {"claude-sid", "codex-sid"})
    chat_attention.mark("codex-sid")
    assert set(chat_attention.list_active()) == {"claude-sid", "codex-sid"}
    events, _ = chat_attention.events_since(0)
    assert [e["sid"] for e in events] == ["codex-sid"]


def test_one_turn_reported_twice_is_announced_once(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(chat_attention, "time", SimpleNamespace(time=lambda: now[0]))
    chat_attention.mark("sid")   # workspace host
    now[0] += 1.5
    chat_attention.mark("sid")   # the rollout watcher, same turn
    now[0] += chat_attention.EVENT_DEDUPE_SECONDS
    chat_attention.mark("sid")   # the next turn
    events, _ = chat_attention.events_since(0)
    assert [e["seq"] for e in events] == [1, 2]


def test_a_restarted_backend_does_not_strand_an_open_page():
    chat_attention.mark("after-restart")
    events, seq = chat_attention.events_since(57)
    assert [e["sid"] for e in events] == ["after-restart"] and seq == 1


# ── the web surface ─────────────────────────────────────────────────────────

def test_attention_api_describes_each_finish_for_the_toast(monkeypatch):
    from ui import web

    rows = {
        "chat": {"session_id": "chat", "agent": "codex", "display_title": "Fix the release gate"},
        "worker": {"session_id": "worker", "agent": "claude", "display_title": "Fleet worker"},
    }
    monkeypatch.setattr(web, "get_session", lambda sid: rows.get(sid))
    monkeypatch.setattr(web, "_fleet_worker_marker", lambda sid: {"run_id": "r"} if sid == "worker" else None)
    client = web.app.test_client()
    first = client.get("/api/chat-attention").get_json()
    for sid in ("chat", "worker", "scratch"):
        chat_attention.mark(sid)
    data = client.get(f"/api/chat-attention?since={first['seq']}").get_json()
    assert set(data["sessions"]) == {"chat", "worker", "scratch"}
    assert data["seq"] == 3
    assert [(e["sid"], e["indexed"], e.get("agent"), e.get("title"), e.get("quiet")) for e in data["events"]] == [
        ("chat", True, "codex", "Fix the release gate", False),
        ("worker", True, "claude", "Fleet worker", True),
        ("scratch", False, None, None, None),
    ]


def test_sending_a_codex_turn_watches_that_chats_own_rollout(monkeypatch):
    from ui import web

    watched = []
    terminal = SimpleNamespace(session_id="019f-codex", agent="codex")
    monkeypatch.setattr(web.pty_terminal, "get", lambda tid: terminal if tid == "t1" else None)
    monkeypatch.setattr(web.pty_terminal, "mark_turn_started", lambda tid, version=None: True)
    monkeypatch.setattr(web, "_terminal_file_snapshot", lambda tid: (None, None))
    monkeypatch.setattr(web, "get_session", lambda sid: {"file_path": "/r/2026/01/01/rollout-x.jsonl"})
    from core import codex_attention_watcher

    monkeypatch.setattr(codex_attention_watcher, "watch", lambda sid, path: watched.append((sid, path)))
    client = web.app.test_client()
    assert client.post("/api/terminal-runtime/turn-start/t1").get_json() == {"ok": True}
    assert watched == [("019f-codex", "/r/2026/01/01/rollout-x.jsonl")]
    terminal.agent = "claude"
    client.post("/api/terminal-runtime/turn-start/t1")
    assert len(watched) == 1


@pytest.mark.parametrize("status,announced", [
    ("completed", True), ("failed", True), ("interrupted", False),
])
def test_a_structured_pane_turn_is_announced_when_it_ends_on_its_own(tmp_path, status, announced):
    """Serena Dev panes run their agents under the workspace host, where no
    Stop hook or rollout tail sees them. A stop he pressed is not news."""
    import threading

    from core.workspace_host import WorkspaceHost
    from core.workspace_journal import WorkspaceJournal
    from ui.workspace_app import _turn_finished

    told = threading.Event()

    def on_turn_finished(sid):
        _turn_finished(sid)   # what the installed app wires in
        told.set()

    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "events.db"), resolve=lambda sid: None,
                         on_turn_finished=on_turn_finished)
    try:
        host._dispatch(host._publish("pane", {
            "method": "turn/completed", "params": {"turn": {"id": "t1", "status": status}},
        }), 5)
        host._dispatch(host._publish("pane", {
            "method": "item/agentMessage/delta", "params": {"threadId": "pane"},
        }), 5)
        assert told.wait(5 if announced else 0.3) is announced
    finally:
        host.shutdown()
    events, _ = chat_attention.events_since(0)
    assert [e["sid"] for e in events] == (["pane"] if announced else [])


def test_the_installed_workspace_announces_its_turns(tmp_path):
    from flask import Flask

    from ui.workspace_app import _turn_finished, install_workspace

    host = install_workspace(Flask(__name__), tmp_path / "events.db")
    try:
        assert host.on_turn_finished is _turn_finished
    finally:
        host.shutdown()
