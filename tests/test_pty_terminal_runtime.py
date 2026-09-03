import os
import time

import pytest

from ui import pty_terminal


@pytest.fixture(autouse=True)
def clean_terminal_registry():
    pty_terminal._web_focus_sid = None
    pty_terminal._web_split_sids = ()
    yield
    for tid in list(pty_terminal._terminals):
        pty_terminal.kill(tid)
    pty_terminal._session_tids.clear()
    pty_terminal._web_focus_sid = None
    pty_terminal._web_split_sids = ()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
def test_runtime_busy_guard_and_hot_resume():
    tid = pty_terminal.spawn(
        ["/bin/sleep", "30"],
        cwd="/tmp",
        session_id="new-test",
        agent="codex",
    )
    term = pty_terminal.get(tid)
    assert term is not None

    pty_terminal.mark_turn_started(tid, (10, 100))
    assert pty_terminal.pause(tid, prewarm_seconds=0) is False
    assert pty_terminal.refresh_turn_state(tid, False, (10, 100)) is True
    assert pty_terminal.refresh_turn_state(tid, False, (20, 200)) is False

    assert pty_terminal.pause(tid, prewarm_seconds=0) is True
    assert pty_terminal.get_runtime_state(tid) == "paused"
    started = time.perf_counter()
    assert pty_terminal.resume(tid) is True
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert pty_terminal.get_runtime_state(tid) == "live"
    assert elapsed_ms < 100
    assert pty_terminal.is_alive(tid)


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY backend")
def test_pseudo_session_mapping_migrates_without_restarting_process():
    tid = pty_terminal.spawn(
        ["/bin/sleep", "30"],
        cwd="/tmp",
        session_id="new-pseudo",
        agent="claude",
    )
    pty_terminal.register_session("new-pseudo", tid)
    pid = pty_terminal.get(tid).proc.pid

    assert pty_terminal.migrate_session("new-pseudo", "real-session", tid)
    assert pty_terminal.tid_for_session("new-pseudo") is None
    assert pty_terminal.tid_for_session("real-session") == tid
    assert pty_terminal.get(tid).session_id == "real-session"
    assert pty_terminal.get(tid).proc.pid == pid


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY backend")
def test_work_reservation_blocks_manual_writes_but_allows_exact_owner():
    """Browser keystrokes must not enter a Codex turn owned by voice work."""
    tid = pty_terminal.spawn(
        ["/bin/sleep", "30"],
        cwd="/tmp",
        session_id="codex-session",
        agent="codex",
    )
    pty_terminal.register_session("codex-session", tid)

    assert pty_terminal.reserve_work(tid, "job-1") == (True, "reserved")
    assert pty_terminal.write(tid, b"manual") is False
    assert pty_terminal.write_reserved(tid, b"owned", "job-2") is False
    assert pty_terminal.write_reserved(tid, b"owned", "job-1") is True
    assert pty_terminal.pause(tid, prewarm_seconds=0) is False
    assert pty_terminal.release_work(tid, "job-2") is False
    assert pty_terminal.release_work(tid, "job-1") is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY backend")
def test_work_reservation_rejects_busy_draft_and_non_codex_targets():
    """A live process alone must never make an unsafe target reusable."""
    busy_tid = pty_terminal.spawn(
        ["/bin/sleep", "30"], cwd="/tmp", session_id="busy", agent="codex"
    )
    pty_terminal.mark_turn_started(busy_tid)
    assert pty_terminal.reserve_work(busy_tid, "job-1") == (
        False,
        "runtime has an active turn",
    )

    draft_tid = pty_terminal.spawn(
        ["/bin/sleep", "30"], cwd="/tmp", session_id="draft", agent="codex"
    )
    pty_terminal.note_manual_input(draft_tid, b"unfinished")
    assert pty_terminal.reserve_work(draft_tid, "job-1") == (
        False,
        "runtime has an unsent draft",
    )

    claude_tid = pty_terminal.spawn(
        ["/bin/sleep", "30"], cwd="/tmp", session_id="claude", agent="claude"
    )
    assert pty_terminal.reserve_work(claude_tid, "job-1") == (
        False,
        "target is not a codex runtime",
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY backend")
def test_xterm_runtime_context_tracks_focus_without_exposing_draft_text(tmp_path):
    """Discovery needs exact focus and draft flags, never draft contents."""
    tid = pty_terminal.spawn(
        ["/bin/sleep", "30"],
        cwd=str(tmp_path),
        session_id="codex-session",
        agent="codex",
    )
    pty_terminal.register_session("codex-session", tid)
    pty_terminal.note_web_context(
        tid,
        focused_sid="codex-session",
        split_sids=["claude-session", "codex-session"],
        draft="private unfinished prompt",
    )

    context = pty_terminal.runtime_context_snapshot()

    assert context["focused_sid"] == "codex-session"
    assert context["split_pair"] == ["claude-session", "codex-session"]
    entry = context["runtimes"][0]
    assert entry["cwd"] == str(tmp_path)
    assert entry["draft"] is True
    assert entry["reserved"] is False
    assert "private unfinished prompt" not in repr(context)


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY backend")
def test_renderer_pty_clamps_tiny_geometry_and_kills_the_complete_registry(monkeypatch):
    tid = pty_terminal.spawn(
        ["/bin/sleep", "30"],
        cwd="/tmp",
        cols=1,
        rows=1,
        session_id="tiny",
        agent="codex",
    )
    terminal = pty_terminal.get(tid)
    assert terminal is not None
    assert terminal.cols == pty_terminal.MIN_COLS
    assert terminal.rows == pty_terminal.MIN_ROWS

    resized = []
    monkeypatch.setattr(
        terminal.proc,
        "setwinsize",
        lambda rows, cols: resized.append((rows, cols)),
    )
    assert pty_terminal.resize(tid, 0, 0)
    assert resized == [(pty_terminal.MIN_ROWS, pty_terminal.MIN_COLS)]

    assert pty_terminal.kill_all() == 1
    assert pty_terminal.get(tid) is None
