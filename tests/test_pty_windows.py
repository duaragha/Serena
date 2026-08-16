import queue
import sys
from inspect import signature

import pytest

import ui
from ui import pty_terminal, pty_windows


class FakeProcess:
    def __init__(self):
        self.reads = queue.Queue()
        self.writes = []
        self.sizes = []
        self.alive = True
        self.terminated_with = None
        self.closed_with = None
        self.pid = 4242

    def read(self, size):
        assert size == 8192
        try:
            item = self.reads.get(timeout=0.05)
        except queue.Empty:
            return ""
        if isinstance(item, BaseException):
            raise item
        return item

    def write(self, data):
        self.writes.append(data)

    def setwinsize(self, rows, cols):
        self.sizes.append((rows, cols))

    def isalive(self):
        return self.alive

    def terminate(self, force=False):
        self.terminated_with = force
        self.alive = False
        self.reads.put(EOFError())

    def close(self, force=False):
        self.closed_with = force

    def feed(self, item):
        self.reads.put(item)


class FakePtyProcess:
    calls = []

    @classmethod
    def spawn(cls, argv, **kwargs):
        proc = FakeProcess()
        cls.calls.append((argv, kwargs, proc))
        return proc


@pytest.fixture(autouse=True)
def isolated_backend(monkeypatch):
    FakePtyProcess.calls.clear()
    monkeypatch.setattr(pty_windows, "_PtyProcess", FakePtyProcess)
    yield
    for tid in list(pty_windows._terminals):
        pty_windows.kill(tid)
    pty_windows._session_tids.clear()
    pty_windows._backend_registry.clear()


def test_backend_registers_under_win32():
    registry = {}

    backend = pty_windows.register_backend(registry)

    assert registry == {"win32": pty_windows}
    assert backend is pty_windows
    assert pty_windows.backend_for("win32", registry) is pty_windows
    assert pty_windows.backend_for("linux", registry) is None


def test_activate_backend_routes_ui_terminal_imports_to_windows_host():
    original_module = sys.modules.get("ui.pty_terminal")
    original_attribute = getattr(ui, "pty_terminal", None)

    try:
        backend = pty_windows.activate_backend()

        assert backend is pty_windows
        assert sys.modules["ui.pty_terminal"] is pty_windows
        assert ui.pty_terminal is pty_windows
        assert pty_windows.backend_for("win32") is pty_windows
    finally:
        if original_module is None:
            sys.modules.pop("ui.pty_terminal", None)
        else:
            sys.modules["ui.pty_terminal"] = original_module
        if original_attribute is None:
            delattr(ui, "pty_terminal")
        else:
            ui.pty_terminal = original_attribute


def test_windows_host_matches_the_browser_facing_terminal_contract():
    interface = (
        "spawn",
        "get",
        "register_session",
        "migrate_session",
        "tid_for_session",
        "write",
        "mark_turn_started",
        "refresh_turn_state",
        "is_runtime_busy",
        "pause",
        "resume",
        "get_runtime_state",
        "resize",
        "read_available",
        "is_alive",
        "kill",
    )

    for name in interface:
        posix_parameters = tuple(signature(getattr(pty_terminal, name)).parameters)
        windows_parameters = tuple(signature(getattr(pty_windows, name)).parameters)
        assert windows_parameters == posix_parameters, name


def test_spawn_sets_conpty_dimensions_environment_and_metadata():
    tid = pty_windows.spawn(
        ["pwsh.exe", "-NoLogo"],
        cwd=r"C:\Users\Raghav",
        cols=132,
        rows=43,
        session_id="new-session",
        agent="Codex",
        terminal_protocol="sixel",
        env={"CUSTOM": "kept"},
    )

    argv, kwargs, proc = FakePtyProcess.calls[-1]
    terminal = pty_windows.get(tid)
    assert argv == ["pwsh.exe", "-NoLogo"]
    assert kwargs["cwd"] == r"C:\Users\Raghav"
    assert kwargs["dimensions"] == (43, 132)
    assert kwargs["env"] == {
        "CUSTOM": "kept",
        "TERM": "xterm-sixel",
        "COLORTERM": "truecolor",
        "COLUMNS": "132",
        "LINES": "43",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    assert terminal is not None
    assert terminal.proc is proc
    assert terminal.session_id == "new-session"
    assert terminal.agent == "codex"
    assert terminal.graphics_protocol == "sixel"


def test_reader_preserves_utf8_and_reports_eof_after_buffer_drain():
    tid = pty_windows.spawn(["cmd.exe"], cwd="C:\\")
    proc = FakePtyProcess.calls[-1][2]
    proc.feed("héllo \x1b[31mred")
    proc.feed(EOFError())

    assert pty_windows.read_available(tid, max_bytes=5, timeout=1) == "héll".encode()
    assert pty_windows.read_available(tid, timeout=1) == b"o \x1b[31mred"
    assert pty_windows.read_available(tid, timeout=1) is None


def test_write_resize_and_runtime_state_match_host_contract():
    tid = pty_windows.spawn(["cmd.exe"], cwd="C:\\")
    proc = FakePtyProcess.calls[-1][2]

    assert pty_windows.write(tid, "snowman: ☃".encode()) is True
    assert proc.writes == ["snowman: ☃"]
    assert pty_windows.resize(tid, 55, 144) is True
    assert proc.sizes == [(55, 144)]
    terminal = pty_windows.get(tid)
    assert terminal is not None
    assert (terminal.rows, terminal.cols) == (55, 144)

    assert pty_windows.mark_turn_started(tid, (10, 20)) is True
    assert pty_windows.is_runtime_busy(tid) is True
    assert pty_windows.refresh_turn_state(tid, False, (10, 20)) is True
    assert pty_windows.refresh_turn_state(tid, False, (11, 21)) is False
    assert pty_windows.pause(tid, prewarm_seconds=0) is False
    assert pty_windows.resume(tid) is True
    assert pty_windows.get_runtime_state(tid) == "live"


def test_session_migration_and_kill_clean_up_conpty():
    tid = pty_windows.spawn(["cmd.exe"], cwd="C:\\")
    proc = FakePtyProcess.calls[-1][2]
    pty_windows.register_session("pseudo", tid)

    assert pty_windows.tid_for_session("pseudo") == tid
    assert pty_windows.migrate_session("pseudo", "real", tid) is True
    assert pty_windows.tid_for_session("pseudo") is None
    assert pty_windows.tid_for_session("real") == tid

    pty_windows.kill(tid)

    assert pty_windows.get(tid) is None
    assert pty_windows.tid_for_session("real") is None
    assert proc.terminated_with is True
    assert proc.closed_with is True
    assert pty_windows.write(tid, b"ignored") is False


def test_spawn_explains_the_pinned_dependency_when_unavailable(monkeypatch):
    monkeypatch.setattr(pty_windows, "_PtyProcess", None)

    with pytest.raises(RuntimeError, match="pywinpty==3.0.5"):
        pty_windows.spawn(["cmd.exe"], cwd="C:\\")
