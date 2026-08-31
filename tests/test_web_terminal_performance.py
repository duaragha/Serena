import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from core import indexer
from core.indexer import _dedupe_discovered
from ui import pty_terminal
from ui import web


class _FakeProcess:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode


def test_index_refresh_runs_out_of_process_and_coalesces(monkeypatch):
    processes = []

    def fake_launch():
        process = _FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(web, "_INDEX_REFRESH_PROC", None)
    monkeypatch.setattr(web, "_launch_index_refresh_process", fake_launch)

    started = time.perf_counter()
    assert web._schedule_index_refresh() is True
    assert time.perf_counter() - started < 0.1
    assert web._schedule_index_refresh() is False
    assert len(processes) == 1

    processes[0].returncode = 0
    assert web._schedule_index_refresh() is True
    assert len(processes) == 2


def test_terminal_page_keeps_accelerated_web_fallback_without_graphics():
    assert 'src="/static/vendor/xterm/addon-webgl.js"' in web.HTML
    assert 'src="/static/vendor/xterm/addon-canvas.js"' in web.HTML
    assert "@xterm/addon-image" not in web.HTML
    assert "sixel" not in web.HTML.lower()
    assert "renderer: rendererKind" in web.HTML
    # The runtime sweep polls whenever a terminal is open, fast while a split is
    # visible and slower otherwise. It used to run only in split view, which
    # meant a single-pane session never put a background runtime to sleep.
    assert "setInterval(" in web.HTML
    assert "_syncWebRuntimePolicy," in web.HTML
    assert "_gtkSplitActive ? 2000 : 15000" in web.HTML


def test_renderer_acks_parsed_bytes_in_batches_instead_of_pausing_per_chunk():
    """xterm.js flow control guide: credit after the parse, batched."""
    assert "const FLOW_ACK_BYTES = 131072;" in web.HTML
    # Credit is added in the write callback, i.e. once xterm actually parsed
    # the batch, not when the frame arrived off the socket.
    assert "ackPending += written;" in web.HTML
    assert "if (!force && ackPending < FLOW_ACK_BYTES) return;" in web.HTML
    assert "socket.send(JSON.stringify({ ack: ackPending }));" in web.HTML
    # Flush the residue when the queue drains, so a quiet terminal can never
    # leave the backend parked below the ack threshold.
    assert "sendAck(true);" in web.HTML
    assert "socket.send(JSON.stringify({ flow: { enabled: true } }))" in web.HTML


def test_renderer_reconnects_to_the_same_pty_instead_of_respawning():
    assert "const _openTerminalSocket = (isResume) => {" in web.HTML
    assert "socket = new WebSocket(state.wsUrl);" in web.HTML
    assert "_scheduleTerminalReconnect();" in web.HTML
    assert "const backoff = Math.min(8000, 250 * Math.pow(2, state.reconnectAttempts - 1));" in web.HTML
    assert "setTermStatus('Reconnecting…', 'error');" in web.HTML
    # A drop must not re-run /api/spawn-terminal: the reconnect reuses the
    # terminal id captured at spawn time.
    assert "'/ws/terminal/' + spawnResp.terminal_id" in web.HTML
    # Stale sockets stay quiet once a newer one owns the pane.
    assert "if (state.ws !== socket || state.closed || state.giveUp) return;" in web.HTML
    # A backend "not found" is terminal: stop retrying a PTY that is gone.
    assert "state.giveUp = true;" in web.HTML


def test_renderer_forces_resize_on_reattach_and_dedupes_otherwise():
    assert "function _sendResizeForSid(sid, force)" in web.HTML
    assert "if (!force && s.sentRows === rows && s.sentCols === cols) return;" in web.HTML
    assert "_sendResizeForSid(state.sid, true);" in web.HTML


def test_window_focus_returns_the_keyboard_to_the_active_pane():
    assert "function _bindWebTerminalWindowFocus()" in web.HTML
    assert "window.addEventListener('focus', refocus);" in web.HTML
    assert "window.addEventListener('blur', () => _setWebTerminalFocus(null));" in web.HTML
    assert "document.addEventListener('visibilitychange', () => {" in web.HTML
    assert "_bindWebTerminalWindowFocus();" in web.HTML
    # Never steal focus from a text field or an open modal.
    assert "el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable" in web.HTML


def test_webgl_renderer_is_probed_before_use_and_disposed_on_teardown():
    assert "const webgl2Available = () => {" in web.HTML
    assert "document.createElement('canvas').getContext('webgl2')" in web.HTML
    assert "window.WebglAddon && window.WebglAddon.WebglAddon && webgl2Available()" in web.HTML
    assert "try { loadCanvasRenderer(); } catch(e) {}" in web.HTML
    assert "try { if (s.rendererAddon) s.rendererAddon.dispose(); } catch(e) {}" in web.HTML


def test_terminal_socket_detaches_on_drop_and_only_kills_on_exit():
    root = Path(web.__file__).resolve().parents[1]
    web_source = (root / "ui" / "web.py").read_text(encoding="utf-8")

    assert "attach_token, backlog = attachment" in web_source
    assert "if not pty_terminal.wait_for_flow(tid, 0.1):" in web_source
    assert "pty_terminal.note_sent(tid, len(chunk))" in web_source
    assert "pty_terminal.note_ack(tid, int(payload[\"ack\"]))" in web_source
    assert "pty_terminal.enable_flow_control(tid)" in web_source
    assert "pty_terminal.detach(tid, attach_token)" in web_source
    # The old behavior, any socket close SIGTERMs the child, is gone.
    assert "stop_reader.set()\n        pty_terminal.kill(tid)" not in web_source
    # Explicit teardown still kills.
    assert "def api_kill_terminal(tid):" in web_source


def test_gtk_terminal_path_is_untouched_by_the_xterm_work():
    """The native shell must keep working while the Electron path lands."""
    root = Path(web.__file__).resolve().parents[1]
    gtk_source = (root / "desktop" / "app_gtk.py").read_text(encoding="utf-8")

    assert "pty_terminal.attach" not in gtk_source
    assert "pty_terminal.detach" not in gtk_source
    assert "pty_terminal.note_ack" not in gtk_source
    assert "pty_terminal.kill_all()" in gtk_source


def test_open_terminal_status_shows_copyable_session_ids():
    # What the row contains lives here; that it renders for unlinked panes too
    # is covered by tests/test_terminal_identity_row.py.
    assert 'id="termSessionIds"' in web.HTML
    assert "_renderOpenSessionIds(_visibleRuntimeSids())" in web.HTML
    assert "navigator.clipboard.writeText(sid)" in web.HTML


def test_linked_workflow_agents_show_count_and_cycle_every_group_member():
    assert "(!dualAgents || opts.threadCount > 1)" in web.HTML
    assert "member.session_id === currentSessionId" in web.HTML
    assert "members[(currentIndex + 1) % members.length]" in web.HTML
    assert "openConv(next.session_id);" in web.HTML


def test_linux_desktop_uses_renderer_terminal_by_default_with_explicit_vte_recovery():
    root = Path(web.__file__).resolve().parents[1]
    gtk_source = (root / "desktop" / "app_gtk.py").read_text(encoding="utf-8")

    assert 'os.environ.get("SERENA_TERMINAL_BACKEND", "renderer")' in gtk_source
    assert '_USE_NATIVE_VTE = _TERMINAL_BACKEND == "vte"' in gtk_source
    assert 'window.__terminalBackend' in gtk_source
    assert "if self._use_native_vte:\n            self.overlay.add_overlay(self._stack)" in gtk_source
    assert "if self._use_native_vte:\n            self.overlay.add_overlay(self._paned)" in gtk_source
    assert "self._stack.set_no_show_all(True)" in gtk_source
    assert "self._paned.set_no_show_all(True)" in gtk_source


def test_renderer_terminal_ignores_hidden_geometry_and_restores_after_layout():
    assert '<script src="/static/terminal_lifecycle.js"></script>' in web.HTML
    assert "window.SerenaTerminalLifecycle.isRenderable({" in web.HTML
    assert "window.SerenaTerminalLifecycle.afterReveal(() => {" in web.HTML
    assert "if (currentTab !== 'chats' || convMode !== 'live') return;" in web.HTML


def test_renderer_terminal_opens_at_tail_without_trapping_manual_scrollback():
    assert "function _armWebTerminalTail(runtime)" in web.HTML
    assert "runtime.term.scrollToBottom();" in web.HTML
    assert "_scrollWebTerminalTail(state);" in web.HTML
    assert "for (const runtime of visibleRuntimes) _scrollWebTerminalTail(runtime);" in web.HTML
    assert "if (ev.deltaY < 0) _cancelWebTerminalTail(state);" in web.HTML
    assert "e.key === 'PageUp' || (e.key === 'Home' && e.ctrlKey)" in web.HTML
    assert "{ passive: true, capture: true }" in web.HTML


def test_renderer_terminal_dependencies_are_local_and_pinned():
    root = Path(web.__file__).resolve().parents[1]
    package = json.loads((root / "ui" / "renderer" / "package.json").read_text())
    assert "cdn.jsdelivr.net" not in web.HTML
    assert package["dependencies"] == {
        "@xterm/addon-canvas": "0.7.0",
        "@xterm/addon-fit": "0.10.0",
        "@xterm/addon-web-links": "0.11.0",
        "@xterm/addon-webgl": "0.18.0",
        "@xterm/xterm": "5.5.0",
    }
    for asset in (
        "xterm.css",
        "xterm.js",
        "addon-fit.js",
        "addon-web-links.js",
        "addon-webgl.js",
        "addon-canvas.js",
        "LICENSE.txt",
    ):
        assert (root / "ui" / "static" / "vendor" / "xterm" / asset).is_file()


def test_native_terminal_tab_switch_preserves_real_gtk_geometry():
    assert "type: 'code-tab-visible', visible: false" in web.HTML
    assert "type: 'code-tab-visible', visible: true, rect: r" in web.HTML
    assert "if (leavingChats && window.__nativeTerminalBridge) {" in web.HTML
    assert "leavingChats && window.__nativeTerminalBridge && _gtkCodeSid" not in web.HTML
    assert "if (tab === 'chats' && window.__nativeTerminalBridge) {" in web.HTML
    assert "tab === 'chats' && window.__nativeTerminalBridge && _gtkCodeSid" not in web.HTML
    assert "restoreNativeTerminal();" in web.HTML
    assert "requestAnimationFrame(() => requestAnimationFrame(restoreNativeTerminal))" not in web.HTML
    assert "if (currentTab !== 'chats' || !_gtkCodeSid) return;" in web.HTML
    assert "if (_gtkRectIsVisible(r)) window.gtkSend({ type: 'code-rect', rect: r });" in web.HTML
    assert "if (currentTab !== 'chats') return null;" in web.HTML
    assert "currentTab === 'chats' &&\n    (!rect || rect.w < 160 || rect.h < 120)" in web.HTML
    assert (
        "if (currentTab !== 'chats' || seq !== _gtkStartSeq || "
        "_gtkCodeSid !== sid) return;"
    ) in web.HTML


def test_duplicate_session_copies_choose_complete_then_stable_path(tmp_path):
    sid = "11111111-2222-3333-4444-555555555555"
    first = tmp_path / "first" / f"{sid}.jsonl"
    second = tmp_path / "second" / f"{sid}.jsonl"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"short")
    second.write_bytes(b"more-complete")
    rows = [
        ("claude", "first", first),
        ("claude", "second", second),
    ]

    assert _dedupe_discovered(rows, {}) == [("claude", "second", second)]

    first.write_bytes(b"same-size-data")
    second.write_bytes(b"same-size-data")
    assert _dedupe_discovered(rows, {sid: str(first)}) == [
        ("claude", "first", first)
    ]


def test_duplicate_session_copies_prefer_transcript_cwd_over_stale_path(tmp_path):
    sid = "11111111-2222-3333-4444-555555555555"
    wrong = tmp_path / "wrong" / f"{sid}.jsonl"
    right = tmp_path / "right" / f"{sid}.jsonl"
    wrong.parent.mkdir()
    right.parent.mkdir()
    cwd = "/home/raghav/Documents/Projects/serena"
    payload = (json.dumps({"type": "user", "cwd": cwd}) + "\n").encode()
    wrong.write_bytes(payload)
    right.write_bytes(payload)
    right_slug = "-home-raghav-Documents-Projects-serena"
    rows = [
        ("claude", "-home-raghav-Documents-Projects-personal-projects-locket", wrong),
        ("claude", right_slug, right),
    ]

    assert _dedupe_discovered(rows, {sid: str(wrong)}) == [
        ("claude", right_slug, right)
    ]


def test_custom_title_repairs_same_project_duplicate_group(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            project_dir TEXT,
            cwd TEXT,
            last_cwd TEXT,
            title TEXT,
            custom_title TEXT,
            is_teammate INTEGER DEFAULT 0
        )"""
    )
    conn.executemany(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, 0)",
        [
            ("custom", "-home-raghav-Documents-Projects-serena", None,
             "/home/raghav/Documents/Projects/serena", "generated", "My Chat"),
            ("restored", "C--Users-ragha-Projects-serena", None,
             "C:/Users/ragha/Projects/serena", "My Chat", None),
            ("other", "-home-raghav-Documents-Projects-locket", None,
             "/home/raghav/Documents/Projects/locket", "My Chat", None),
        ],
    )
    linked = []
    monkeypatch.setattr(indexer.meta_sync, "get_all_meta", lambda: {})
    monkeypatch.setattr(indexer.meta_sync, "link_sessions", lambda sids: linked.append(sids))

    assert indexer._repair_custom_title_groups(conn) == 1
    assert linked == [["custom", "restored"]]


def test_internal_brain_rotations_are_hidden_from_chat_rows():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT, project_dir TEXT, is_teammate INTEGER)"
    )
    conn.executemany(
        "INSERT INTO sessions VALUES (?, ?, 0)",
        [
            ("brain", "-home-raghav--cache-serena-headless-brain"),
            ("test", "-tmp-serena-http-rotation-cwd"),
            ("chat", "-home-raghav-Documents-Projects-serena"),
        ],
    )

    indexer._hide_internal_sessions(conn)

    hidden = {
        row["session_id"]: row["is_teammate"]
        for row in conn.execute("SELECT * FROM sessions")
    }
    assert hidden == {"brain": 1, "test": 1, "chat": 0}


def test_modal_keyboard_is_exclusive():
    guard = "document.getElementById('modalBackdrop')?.classList.contains('visible')"
    assert web.HTML.count(guard) >= 2
    assert "e.preventDefault(); e.stopImmediatePropagation(); close(true);" in web.HTML
    assert "e.preventDefault(); e.stopImmediatePropagation(); close(false);" in web.HTML


def test_xterm_editing_shortcuts_reach_the_pty_before_widget_shortcuts():
    root = Path(web.__file__).resolve().parents[1]
    web_source = (root / "ui" / "web.py").read_text(encoding="utf-8")
    gtk_source = (root / "desktop" / "app_gtk.py").read_text(encoding="utf-8")

    # The newline is chosen per platform now: a bare LF on POSIX, and the CSI-u
    # form on Windows, where ConPTY eats the escape prefix the usual remedy uses.
    # See tests/test_terminal_newline.py.
    assert "ws.send(_TERM_NEWLINE)" in web_source
    assert r"ws.send('\x17')" in web_source
    assert "function _extendTermKeyboardSelection(state, key)" in web_source
    assert "term.select(start % cols, Math.floor(start / cols), end - start);" in web_source
    assert "if (_extendTermKeyboardSelection(s, k))" in web_source
    assert r"arrowup: '\x1b[1;2A'" not in web_source
    assert r"arrowdown: '\x1b[1;2B'" not in web_source
    assert r"arrowright: '\x1b[1;2C'" not in web_source
    assert r"arrowleft: '\x1b[1;2D'" not in web_source
    assert 'if action.startswith("term-") and self._focused_vte() is None:' in gtk_source


# ═══════════════════════════════════════════════════════════════════════════
# Flow control and reconnect for the xterm.js terminal path.
#
# The Electron shell runs the browser terminal, not the GTK VTE, so it needs
# two behaviours the native widget never had: output bounded by watermarks
# with the renderer acking what it parsed, and a dropped websocket that
# detaches from the PTY instead of killing claude/codex.
# ═══════════════════════════════════════════════════════════════════════════

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX PTY backend")


@pytest.fixture(autouse=True)
def clean_terminal_registry():
    yield
    for tid in list(pty_terminal._terminals):
        pty_terminal.kill(tid)
    pty_terminal._session_tids.clear()
    pty_terminal._web_focus_sid = None
    pty_terminal._web_split_sids = ()


def _spawn(argv=("/bin/sleep", "30"), **kwargs):
    return pty_terminal.spawn(list(argv), cwd="/tmp", **kwargs)


def _wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ── flow control ────────────────────────────────────────────────────────────


@_POSIX_ONLY
def test_flow_control_stays_off_until_the_renderer_arms_it():
    """A client that never acks must stream, not deadlock behind a watermark."""
    tid = _spawn()

    assert pty_terminal.note_sent(tid, pty_terminal.FLOW_HIGH_WATER * 4) is False
    assert pty_terminal.flow_paused(tid) is False
    assert pty_terminal.wait_for_flow(tid, 0.01) is True

    assert pty_terminal.enable_flow_control(tid) is True


@_POSIX_ONLY
def test_pre_handshake_bytes_stay_on_the_ledger_when_flow_control_arms():
    """The reconnect replay ships before the handshake; it still has to count."""
    tid = _spawn()

    # Mirrors the route: backlog goes out, then the renderer arms flow control.
    replay = pty_terminal.BACKLOG_MAX_BYTES
    assert pty_terminal.note_sent(tid, replay) is False
    pty_terminal.enable_flow_control(tid)

    # Arming must not reset the ledger to a false zero, or the first watermark
    # check would start from scratch and the replay would escape accounting.
    assert pty_terminal.flow_snapshot(tid)["unacked"] == replay
    assert pty_terminal.flow_paused(tid) is False

    # And the renderer's ack for those replayed bytes clears them normally.
    assert pty_terminal.note_ack(tid, replay) == 0


@_POSIX_ONLY
def test_arming_flow_control_over_a_full_ledger_pauses_immediately():
    tid = _spawn()

    pty_terminal.note_sent(tid, pty_terminal.FLOW_HIGH_WATER + 1)
    assert pty_terminal.flow_paused(tid) is False, "gated before the handshake"

    pty_terminal.enable_flow_control(tid)

    assert pty_terminal.flow_paused(tid) is True
    assert pty_terminal.wait_for_flow(tid, 0.01) is False


@_POSIX_ONLY
def test_watermarks_replace_naive_per_chunk_pausing():
    """Chunks flow freely until the outstanding total crosses the high mark."""
    tid = _spawn()
    pty_terminal.enable_flow_control(tid)

    chunk = 64 * 1024
    sent = 0
    while sent + chunk < pty_terminal.FLOW_HIGH_WATER:
        assert pty_terminal.note_sent(tid, chunk) is False, "paused mid-stream"
        assert pty_terminal.flow_paused(tid) is False
        sent += chunk

    # Crossing the high watermark is the only thing that stops the reader.
    assert pty_terminal.note_sent(tid, chunk * 4) is True
    assert pty_terminal.flow_paused(tid) is True
    assert pty_terminal.wait_for_flow(tid, 0.01) is False

    # Hysteresis: a partial ack that leaves us above the low mark keeps it shut.
    outstanding = pty_terminal.flow_snapshot(tid)["unacked"]
    partial = outstanding - pty_terminal.FLOW_LOW_WATER - 1
    assert pty_terminal.note_ack(tid, partial) > pty_terminal.FLOW_LOW_WATER
    assert pty_terminal.flow_paused(tid) is True

    assert pty_terminal.note_ack(tid, chunk * 8) == 0
    assert pty_terminal.flow_paused(tid) is False
    assert pty_terminal.wait_for_flow(tid, 0.01) is True


@_POSIX_ONLY
def test_ack_credit_is_clamped_and_never_goes_negative():
    tid = _spawn()
    pty_terminal.enable_flow_control(tid)

    pty_terminal.note_sent(tid, 1000)
    assert pty_terminal.note_ack(tid, 99999) == 0
    assert pty_terminal.note_ack(tid, -5) == 0
    assert pty_terminal.flow_paused(tid) is False


# ── detach / reattach ───────────────────────────────────────────────────────


@_POSIX_ONLY
def test_detach_keeps_the_child_alive_and_reattach_replays_missed_output():
    tid = _spawn(["/bin/sh", "-c", "while :; do echo tick; sleep 0.05; done"])
    pid = pty_terminal.get(tid).proc.pid

    token, backlog = pty_terminal.attach(tid)
    assert backlog == b""
    assert pty_terminal.is_attached(tid, token) is True

    assert pty_terminal.detach(tid, token, grace=30) is True
    assert pty_terminal.is_attached(tid, token) is False
    # The child must keep draining while nobody is attached, or it blocks on a
    # full PTY buffer and the "reconnect" resumes a wedged process.
    assert _wait_until(
        lambda: pty_terminal.attachment_snapshot(tid)["backlog_bytes"] > 0
    )

    next_token, replay = pty_terminal.attach(tid)
    assert next_token > token
    assert b"tick" in replay
    assert pty_terminal.is_alive(tid) is True
    assert pty_terminal.get(tid).proc.pid == pid, "reconnect respawned the PTY"
    assert pty_terminal.attachment_snapshot(tid)["backlog_bytes"] == 0


@_POSIX_ONLY
def test_reattach_resets_flow_state_and_supersedes_the_stale_socket():
    tid = _spawn()
    first, _ = pty_terminal.attach(tid)
    pty_terminal.enable_flow_control(tid)
    pty_terminal.note_sent(tid, pty_terminal.FLOW_HIGH_WATER)
    assert pty_terminal.flow_paused(tid) is True

    second, _ = pty_terminal.attach(tid)

    snapshot = pty_terminal.flow_snapshot(tid)
    assert snapshot["enabled"] is False
    assert snapshot["unacked"] == 0
    assert snapshot["paused"] is False
    assert pty_terminal.is_attached(tid, first) is False
    assert pty_terminal.is_attached(tid, second) is True
    # A late teardown from the socket that already lost the terminal must not
    # detach the one that replaced it.
    assert pty_terminal.detach(tid, first) is False
    assert pty_terminal.attachment_snapshot(tid)["attached"] is True


@_POSIX_ONLY
def test_truncated_backlog_is_prefixed_with_a_terminal_reset(monkeypatch):
    """A cut backlog starts mid-escape-sequence; replaying it raw scrambles."""
    monkeypatch.setattr(pty_terminal, "BACKLOG_MAX_BYTES", 512)
    tid = _spawn(["/bin/sh", "-c", "seq 1 20000; sleep 30"])

    token, _ = pty_terminal.attach(tid)
    pty_terminal.detach(tid, token, grace=30)
    assert _wait_until(
        lambda: pty_terminal.attachment_snapshot(tid)["backlog_truncated"]
    )

    _, replay = pty_terminal.attach(tid)
    assert replay.startswith(b"\x1bc")
    assert len(replay) <= 512 + len(b"\x1bc")
    assert pty_terminal.is_alive(tid) is True


@_POSIX_ONLY
def test_detach_grace_expiry_reaps_an_abandoned_pty():
    tid = _spawn()
    token, _ = pty_terminal.attach(tid)

    pty_terminal.detach(tid, token, grace=0.05)

    assert _wait_until(lambda: pty_terminal.get(tid) is None)


@_POSIX_ONLY
def test_reserved_runtime_outlives_the_grace_period_until_work_finishes():
    """A closed browser tab must not kill a PTY that Serena work owns."""
    tid = _spawn(session_id="codex-session", agent="codex")
    assert pty_terminal.reserve_work(tid, "job-1") == (True, "reserved")
    token, _ = pty_terminal.attach(tid)

    pty_terminal.detach(tid, token, grace=0.05)
    time.sleep(0.4)
    assert pty_terminal.get(tid) is not None, "reaped a reserved runtime"
    assert pty_terminal.is_alive(tid) is True

    assert pty_terminal.release_work(tid, "job-1") is True
    assert _wait_until(lambda: pty_terminal.get(tid) is None)


# ── the websocket route ─────────────────────────────────────────────────────


class _ScriptedSocket:
    """Minimal stand-in for the flask-sock server socket."""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []
        self.closed = False

    def send(self, data):
        self.sent.append(data)

    def receive(self, timeout=None):
        if self.script:
            return self.script.pop(0)
        raise RuntimeError("socket dropped")  # what an unclean drop looks like

    def close(self):
        self.closed = True

    def control_frames(self):
        frames = []
        for item in self.sent:
            if isinstance(item, str) and item.startswith("{"):
                frames.append(json.loads(item))
        return frames


def _ws_terminal_handler():
    # flask-sock's decorator returns None, so reach the real handler through
    # the registered view function's functools.wraps back-reference.
    return web.app.view_functions["ws_terminal"].__wrapped__


@_POSIX_ONLY
def test_socket_attaches_arms_flow_control_and_applies_resize():
    tid = _spawn()
    socket = _ScriptedSocket([
        json.dumps({"flow": {"enabled": True}}),
        json.dumps({"resize": {"rows": 44, "cols": 132}}),
        json.dumps({"ack": 4096}),
    ])

    _ws_terminal_handler()(socket, tid)

    hello = socket.control_frames()[0]
    assert hello["attached"] is True
    assert hello["terminal_id"] == tid
    assert hello["ack_bytes"] == pty_terminal.FLOW_ACK_BYTES
    terminal = pty_terminal.get(tid)
    assert terminal is not None
    assert (terminal.rows, terminal.cols) == (44, 132)


@_POSIX_ONLY
def test_dropped_socket_detaches_instead_of_killing_the_pty():
    tid = _spawn()
    pid = pty_terminal.get(tid).proc.pid

    _ws_terminal_handler()(_ScriptedSocket([]), tid)

    assert pty_terminal.get(tid) is not None, "a dropped socket killed the PTY"
    assert pty_terminal.is_alive(tid) is True
    snapshot = pty_terminal.attachment_snapshot(tid)
    assert snapshot["attached"] is False
    assert snapshot["detached_at"] is not None

    # Reconnecting to the same terminal id resumes the same process.
    socket = _ScriptedSocket([])
    _ws_terminal_handler()(socket, tid)
    assert socket.control_frames()[0]["attached"] is True
    assert pty_terminal.get(tid).proc.pid == pid


@_POSIX_ONLY
def test_one_frame_carrying_several_control_keys_applies_all_of_them():
    """A batched frame must not lose whichever key the dispatcher checks last."""
    tid = _spawn()
    pty_terminal.enable_flow_control(tid)
    pty_terminal.note_sent(tid, 50000)
    socket = _ScriptedSocket([
        json.dumps({"resize": {"rows": 51, "cols": 143}, "ack": 20000}),
    ])

    _ws_terminal_handler()(socket, tid)

    terminal = pty_terminal.get(tid)
    assert terminal is not None
    # Both keys took effect: the resize applied AND the ack was credited.
    assert (terminal.rows, terminal.cols) == (51, 143)
    # attach() reset the ledger, so the ack floors it rather than going negative.
    assert pty_terminal.flow_snapshot(tid)["unacked"] == 0
    # The frame was consumed as control, never typed into the PTY as raw text.
    assert terminal.input_draft == ""


@_POSIX_ONLY
def test_replayed_backlog_is_counted_against_the_flow_ledger():
    tid = _spawn(["/bin/sh", "-c", "seq 1 4000; sleep 30"])
    token, _ = pty_terminal.attach(tid)
    pty_terminal.detach(tid, token, grace=30)
    assert _wait_until(
        lambda: pty_terminal.attachment_snapshot(tid)["backlog_bytes"] > 1000
    )

    pending = pty_terminal.attachment_snapshot(tid)["backlog_bytes"]

    # detach() clears the ledger on the way out, so sample it while the
    # handler is still attached: one message to arm flow control, then the
    # probe fires on the next receive, after that message was processed.
    class _ProbingSocket(_ScriptedSocket):
        def __init__(self, script, probe):
            super().__init__(script)
            self._probe = probe
            self.samples = []

        def receive(self, timeout=None):
            if self.script:
                return self.script.pop(0)
            self.samples.append(self._probe())
            raise RuntimeError("socket dropped")

    socket = _ProbingSocket(
        [json.dumps({"flow": {"enabled": True}})],
        lambda: pty_terminal.flow_snapshot(tid),
    )
    _ws_terminal_handler()(socket, tid)

    replayed = socket.control_frames()[0]["replayed_bytes"]
    assert replayed >= pending
    # Without the fix this sampled 0: the replay shipped before the handshake
    # and enable_flow_control zeroed whatever had already gone out.
    assert socket.samples[0]["enabled"] is True
    assert socket.samples[0]["unacked"] >= replayed


@_POSIX_ONLY
def test_socket_for_a_dead_terminal_reports_an_unrecoverable_error():
    socket = _ScriptedSocket([])

    _ws_terminal_handler()(socket, "does-not-exist")

    assert socket.control_frames()[0]["error"] == "Terminal not found"
    assert socket.closed is True
