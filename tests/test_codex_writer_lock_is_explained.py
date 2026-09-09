"""A Codex pane that dies a second after opening says why.

Codex 0.153 holds a per-thread writer lock. `codex resume` on a thread some
other process owns draws its banner, prints one line, and exits 1 within a
second -- and the renderer showed "Session ended." with nothing else. The
holder on the day this was found was the ChatGPT desktop app, whose code-mode
app-server had loaded and locked seven of the user's recent local threads.

The child says exactly what happened on the way out. These tests cover
keeping those bytes, recognising them, naming the holder through /proc, and
the handler sending a reason instead of a bare exit.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ui import pty_terminal, web

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")

SID = "019fae07-38c5-71c1-87fa-a021dcfc31b2"
REFUSAL = (
    b"Failed to resume session from /x/rollout-2026-07-29T09-18-56-" + SID.encode() + b".jsonl: "
    b"thread/resume failed during TUI bootstrap: thread/resume failed: thread " + SID.encode()
    + b" already has an active writer (code -32600)\r\n\x1b[0 q"
)


class _DeadProc:
    def isalive(self):
        return False


class _LiveProc:
    def isalive(self):
        return True


def _term(agent="codex", tail=REFUSAL, proc=None, age=0.0, sid=SID):
    term = pty_terminal.Terminal(id="t", proc=proc or _DeadProc(), cols=100, rows=30)
    term.agent = agent
    term.session_id = sid
    term.tail = bytearray(tail)
    term.started_at = time.monotonic() - age
    return term


@pytest.fixture
def registered(monkeypatch):
    made = []

    def register(term):
        with pty_terminal._registry_lock:
            pty_terminal._terminals[term.id] = term
        made.append(term.id)
        return term.id

    yield register
    with pty_terminal._registry_lock:
        for tid in made:
            pty_terminal._terminals.pop(tid, None)


# ── recognising the holder ────────────────────────────────────────────────────

@pytest.mark.parametrize("cmdline, label", [
    ("/usr/lib/chatgpt/resources/codex -c features.code_mode_host=true app-server", "the ChatGPT desktop app"),
    ("node /home/r/.nvm/versions/node/v24/bin/codex app-server --stdio --disable shell_tool", "Serena's codex brain"),
    ("node /home/r/.nvm/versions/node/v24/bin/codex resume 019fae07-38c5 --cd /home/r", "another Codex pane"),
    ("/usr/lib/chatgpt/resources/codex app-server --listen stdio://", "the ChatGPT desktop app"),
    ("/some/other/codex app-server --listen stdio://", "a Codex app-server"),
    ("python3 -c 'import time; time.sleep(9)'", "another process"),
])
def test_holders_are_named_in_words_the_user_recognises(cmdline, label) -> None:
    assert pty_terminal._holder_label(cmdline) == label


def test_a_process_holding_the_rollout_is_found_through_proc(tmp_path) -> None:
    # A sid nothing on this machine could be holding: the real one above was
    # found held by the ChatGPT desktop app the first time this ran.
    sid = "00000000-0000-4000-8000-00000000c0de"
    rollout = tmp_path / f"rollout-2026-07-29T09-18-56-{sid}.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    child = subprocess.Popen(
        [sys.executable, "-c", f"import time; f=open({str(rollout)!r},'a'); print('held',flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout.readline().strip() == "held"

        holders = pty_terminal.writer_lock_holders(sid)

        assert [h["pid"] for h in holders] == [child.pid]
        assert holders[0]["label"] == "another process"
        assert pty_terminal.writer_lock_holders("00000000-0000-0000-0000-000000000000") == []
    finally:
        child.kill()
        child.wait(timeout=10)


# ── the explanation ───────────────────────────────────────────────────────────

def test_the_refusal_is_explained_with_the_holder_named(registered, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        pty_terminal, "writer_lock_holders",
        lambda sid: [{"pid": 2174854, "cmdline": "/usr/lib/chatgpt/resources/codex app-server",
                      "label": "the ChatGPT desktop app"}],
    )
    tid = registered(_term())

    reason = pty_terminal.explain_early_exit(tid)

    assert reason is not None
    assert "019fae07" in reason
    assert "the ChatGPT desktop app (pid 2174854)" in reason
    assert "Close it there" in reason


def test_a_stale_lock_with_no_holder_is_explained_too(registered, monkeypatch) -> None:
    monkeypatch.setattr(pty_terminal, "writer_lock_holders", lambda sid: [])
    tid = registered(_term())

    reason = pty_terminal.explain_early_exit(tid)

    assert reason is not None
    assert "no process is holding the thread" in reason


@pytest.mark.parametrize("term, why", [
    (_term(agent="claude"), "not codex"),
    (_term(tail=b"goodbye\r\n"), "different last words"),
    (_term(age=pty_terminal.EARLY_EXIT_SECONDS + 5), "lived too long to be a bootstrap failure"),
    (_term(proc=_LiveProc()), "still alive"),
])
def test_anything_else_stays_an_ordinary_exit(registered, monkeypatch, term, why) -> None:
    monkeypatch.setattr(pty_terminal, "writer_lock_holders", lambda sid: [{"pid": 1, "cmdline": "", "label": "x"}])
    tid = registered(term)

    assert pty_terminal.explain_early_exit(tid) is None, why


def test_an_unregistered_terminal_is_not_explained() -> None:
    assert pty_terminal.explain_early_exit("nope") is None


# ── keeping the last words ────────────────────────────────────────────────────

def test_the_tail_keeps_the_end_and_stays_bounded() -> None:
    term = pty_terminal.Terminal(id="t", proc=_DeadProc(), cols=100, rows=30)

    pty_terminal._remember_tail(term, b"a" * 3000)
    pty_terminal._remember_tail(term, b"b" * 3000)

    assert len(term.tail) == pty_terminal._TAIL_BYTES
    assert bytes(term.tail).endswith(b"b" * 3000)
    assert bytes(term.tail).startswith(b"a")


def test_read_available_records_the_tail_on_a_real_pty(registered) -> None:
    from ptyprocess import PtyProcess

    proc = PtyProcess.spawn([sys.executable, "-c", "print('last words'); import time; time.sleep(2)"],
                            dimensions=(30, 100))
    term = pty_terminal.Terminal(id="real", proc=proc, cols=100, rows=30)
    tid = registered(term)
    try:
        got = b""
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and b"last words" not in got:
            chunk = pty_terminal.read_available(tid, timeout=0.1)
            if chunk is None:
                break
            got += chunk
        assert b"last words" in bytes(term.tail)
    finally:
        proc.terminate(force=True)


# ── the handler hands the reason over ─────────────────────────────────────────

def test_the_socket_sends_a_reason_instead_of_a_bare_exit() -> None:
    source = Path(web.__file__).read_text(encoding="utf-8")
    start = source.index('@sock.route("/ws/terminal/<tid>")')
    body = source[start : start + 6000]
    assert "reason = pty_terminal.explain_early_exit(tid)" in body
    assert 'json.dumps({"error": reason} if reason else {"exit": True})' in body
    assert body.index("explain_early_exit") < body.index('{"exit": True}')
