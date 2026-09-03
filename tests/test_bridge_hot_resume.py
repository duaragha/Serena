import pytest

from core import claude_bridge, codex_bridge
from ui import pty_terminal


class _Instance:
    def __init__(self, sid):
        self._vtes = {sid: object()}
        self._vte_pids = {sid: 1234}
        self._vte_rect = None
        self.wakes = []
        self.turns = []

    def _wake_runtime(self, sid, reason, focus=False):
        self.wakes.append((sid, reason, focus))

    def runtime_begin_turn(self, sid):
        self.turns.append(sid)


@pytest.mark.parametrize("bridge", [claude_bridge, codex_bridge])
def test_live_bridge_target_skips_cold_start_delay(monkeypatch, bridge):
    sid = "session-1"
    inst = _Instance(sid)
    sleeps = []
    monkeypatch.setattr(bridge, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(bridge, "_wait_for_gtk_vte", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(bridge.time, "sleep", sleeps.append)

    from gi.repository import GLib

    monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: callback(*args))

    assert bridge._gtk_resume_target(inst, sid) == (True, "resumed")
    assert inst.wakes == [(sid, "bridge", False)]
    assert inst.turns == [sid]
    assert sleeps == []


@pytest.mark.parametrize("bridge", [claude_bridge, codex_bridge])
def test_xterm_shell_never_spawns_a_hidden_native_vte(monkeypatch, bridge):
    from desktop.app_gtk import ChatsApp

    inst = _Instance("session-1")
    inst._use_native_vte = False
    monkeypatch.setattr(ChatsApp, "INSTANCE", inst)

    assert bridge._gtk_feed("session-1", "hello") == (
        False,
        "GTK shell uses the xterm backend",
    )
    assert inst.wakes == []
    assert inst.turns == []


@pytest.mark.parametrize("bridge", [claude_bridge, codex_bridge])
def test_xterm_bridge_resumes_runtime_before_writing(monkeypatch, bridge, tmp_path):
    sid = "session-1"
    tid = "terminal-1"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    calls = []

    monkeypatch.setattr(pty_terminal, "tid_for_session", lambda value: tid if value == sid else None)
    monkeypatch.setattr(pty_terminal, "resume", lambda value: calls.append(("resume", value)) or True)
    monkeypatch.setattr(
        pty_terminal,
        "mark_turn_started",
        lambda value, version: calls.append(("mark", value, version)) or True,
    )
    monkeypatch.setattr(
        pty_terminal,
        "write",
        lambda value, data: calls.append(("write", value, data)) or True,
    )
    monkeypatch.setattr(bridge.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    finder = "find_codex_jsonl" if bridge is codex_bridge else "find_claude_jsonl"
    monkeypatch.setattr(bridge, finder, lambda _sid: transcript)

    assert bridge._pty_feed(sid, "hello") == (True, "queued (pty)")
    assert calls[0] == ("resume", tid)
    assert calls[1][0:2] == ("mark", tid)
    assert calls[2] == ("write", tid, b"\x1b[200~hello\x1b[201~")
    assert calls[3] == ("sleep", 0.25)
    assert calls[4] == ("write", tid, b"\r")
