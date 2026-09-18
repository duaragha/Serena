from __future__ import annotations

import signal
from pathlib import Path

import pytest

from voice.desktop.supervisor import (
    Component,
    VoiceAppSupervisor,
    components,
    display_is_listening,
    session_display_environment,
    wait_for_display,
)


@pytest.fixture(autouse=True)
def _screen_is_already_there(monkeypatch):
    """Every test but the display ones assumes a session that can draw."""

    monkeypatch.setattr(
        "voice.desktop.supervisor.wait_for_display", lambda environment, **_: True
    )


class FakeProcess:
    next_pid = 4100

    def __init__(self, command, *, cwd, env, start_new_session):
        self.command = command
        self.cwd = cwd
        self.env = env
        self.start_new_session = start_new_session
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.return_code = None
        self.waited = False

    def poll(self):
        return self.return_code

    def wait(self, timeout):
        self.waited = True
        self.return_code = -signal.SIGTERM
        return self.return_code


def test_supervisor_starts_every_component_in_its_own_process_group(
    tmp_path: Path,
    monkeypatch,
) -> None:
    spawned: list[FakeProcess] = []

    def fake_popen(*args, **kwargs):
        process = FakeProcess(*args, **kwargs)
        spawned.append(process)
        return process

    items = (
        Component("brain-bridge", ("bridge",), tmp_path),
        Component("desk-voice", ("desk",), tmp_path),
        Component("dot-display", ("display",), tmp_path),
    )
    monkeypatch.setattr("voice.desktop.supervisor.subprocess.Popen", fake_popen)
    monkeypatch.setattr("voice.desktop.supervisor.time.sleep", lambda _seconds: None)
    supervisor = VoiceAppSupervisor(items)

    supervisor.start()

    assert [process.command[0] for process in spawned] == ["bridge", "desk", "display"]
    assert all(process.start_new_session for process in spawned)


def test_dot_display_waits_for_voice_first_start_window(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sleeps: list[float] = []
    items = (
        Component("desk-voice", ("desk",), tmp_path),
        Component("dot-display", ("display",), tmp_path),
    )
    monkeypatch.setattr("voice.desktop.supervisor.subprocess.Popen", FakeProcess)
    monkeypatch.setattr(
        "voice.desktop.supervisor.time.sleep", lambda seconds: sleeps.append(seconds)
    )
    supervisor = VoiceAppSupervisor(items)

    supervisor.start()

    assert sleeps == [0.1, 1.0, 0.1]


def test_production_order_opens_awake_voice_before_display() -> None:
    configured = components()

    assert [item.name for item in configured] == [
        "desk-voice",
        "brain-bridge",
        "dot-display",
    ]
    desk = next(item for item in configured if item.name == "desk-voice")
    assert "--start-awake" in desk.command


def test_stopping_one_lifecycle_terminates_every_component(
    tmp_path: Path,
    monkeypatch,
) -> None:
    signals: list[tuple[int, signal.Signals]] = []
    items = (
        Component("brain-bridge", ("bridge",), tmp_path),
        Component("desk-voice", ("desk",), tmp_path),
        Component("dot-display", ("display",), tmp_path),
    )
    monkeypatch.setattr("voice.desktop.supervisor.subprocess.Popen", FakeProcess)
    monkeypatch.setattr("voice.desktop.supervisor.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "voice.desktop.supervisor.os.killpg",
        lambda pid, signum: signals.append((pid, signum)),
    )
    supervisor = VoiceAppSupervisor(items)
    supervisor.start()

    supervisor.stop()

    assert len([entry for entry in signals if entry[1] == signal.SIGTERM]) == 3
    assert supervisor.processes == {}


def test_session_close_only_reaches_desk_voice(
    tmp_path: Path,
    monkeypatch,
) -> None:
    signals: list[tuple[int, signal.Signals]] = []
    items = (
        Component("brain-bridge", ("bridge",), tmp_path),
        Component("desk-voice", ("desk",), tmp_path),
        Component("dot-display", ("display",), tmp_path),
    )
    monkeypatch.setattr("voice.desktop.supervisor.subprocess.Popen", FakeProcess)
    monkeypatch.setattr("voice.desktop.supervisor.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "voice.desktop.supervisor.os.killpg",
        lambda pid, signum: signals.append((pid, signum)),
    )
    supervisor = VoiceAppSupervisor(items)
    supervisor.start()

    supervisor.forward_session_close()

    assert signals == [(supervisor.processes["desk-voice"].pid, signal.SIGUSR1)]


def _fake_popen_factory(spawned: list) -> object:
    def fake_popen(*args, **kwargs):
        process = FakeProcess(*args, **kwargs)
        spawned.append(process)
        return process

    return fake_popen


def test_a_finished_conversation_puts_the_microphone_back_on_the_wake_word(
    monkeypatch,
) -> None:
    """The app launches awake, has one conversation, and used to go deaf.

    Nothing else rearms it: the standalone wake listener only takes over once
    the whole unit stops, and the unit deliberately stays up for the type bar.
    """
    spawned: list[FakeProcess] = []
    monkeypatch.setattr("voice.desktop.supervisor.subprocess.Popen", _fake_popen_factory(spawned))
    monkeypatch.setattr("voice.desktop.supervisor.time.sleep", lambda _seconds: None)

    supervisor = VoiceAppSupervisor()
    supervisor.start()
    assert supervisor.rearm_wake() is True

    rearmed = spawned[-1]
    assert "voice.desk.client" in rearmed.command
    assert "--start-awake" not in rearmed.command
    assert supervisor.processes["desk-voice"] is rearmed


def test_a_flapping_wake_client_stops_being_relaunched(monkeypatch) -> None:
    """A rearm that dies instantly must not become a spawn loop."""
    spawned: list[FakeProcess] = []
    monkeypatch.setattr("voice.desktop.supervisor.subprocess.Popen", _fake_popen_factory(spawned))

    supervisor = VoiceAppSupervisor()
    # The first conversation is launched awake and is meant to end.
    assert supervisor._rearm_is_flapping() is False

    for _ in range(2):
        supervisor.rearm_wake()
        assert supervisor._rearm_is_flapping() is False
    supervisor.rearm_wake()
    assert supervisor._rearm_is_flapping() is True


def test_a_healthy_wake_client_resets_the_failure_count(monkeypatch) -> None:
    spawned: list[FakeProcess] = []
    monkeypatch.setattr("voice.desktop.supervisor.subprocess.Popen", _fake_popen_factory(spawned))
    clock = {"now": 1_000.0}
    monkeypatch.setattr("voice.desktop.supervisor.time.monotonic", lambda: clock["now"])

    supervisor = VoiceAppSupervisor()
    supervisor.rearm_wake()
    supervisor._rearm_is_flapping()
    assert supervisor.rearm_failures == 1

    supervisor.rearm_wake()
    clock["now"] += 600.0  # she listened happily for ten minutes, then timed out
    assert supervisor._rearm_is_flapping() is False
    assert supervisor.rearm_failures == 0


class _Screen:
    """A session whose X server shows up partway through the wait."""

    def __init__(self, appears_after: int = 0, display: str = ":0") -> None:
        self.appears_after = appears_after
        self.display = display
        self.polls = 0

    def listening(self, display: str) -> bool:
        self.polls += 1
        return display == self.display and self.polls > self.appears_after


def test_the_display_address_is_taken_from_the_session_that_published_it(
    monkeypatch,
) -> None:
    """The unit starts at login, before the desktop exports DISPLAY.

    This is the whole crash: Electron inherited an empty DISPLAY, aborted with
    "Missing X server", and took her voice down with it for five days.
    """

    screen = _Screen()
    monkeypatch.setattr(
        "voice.desktop.supervisor.session_display_environment",
        lambda: {"DISPLAY": ":0", "XAUTHORITY": "/home/raghav/.Xauthority"},
    )
    monkeypatch.setattr(
        "voice.desktop.supervisor.display_is_listening", screen.listening)
    environment: dict[str, str] = {}

    assert wait_for_display(environment) is True
    assert environment["DISPLAY"] == ":0"
    assert environment["XAUTHORITY"] == "/home/raghav/.Xauthority"


def test_a_desktop_that_is_still_booting_is_waited_out(monkeypatch) -> None:
    screen = _Screen(appears_after=3)
    slept: list[float] = []
    monkeypatch.setattr(
        "voice.desktop.supervisor.session_display_environment", lambda: {})
    monkeypatch.setattr(
        "voice.desktop.supervisor.display_is_listening", screen.listening)
    monkeypatch.setattr(
        "voice.desktop.supervisor.time.sleep", lambda seconds: slept.append(seconds))

    assert wait_for_display({"DISPLAY": ":0", "XAUTHORITY": "/x"}) is True
    assert len(slept) == 3


def test_a_session_with_no_screen_at_all_gives_up_instead_of_hanging(
    monkeypatch,
) -> None:
    clock = iter([0.0, 0.0, 1.0, 99.0])
    monkeypatch.setattr(
        "voice.desktop.supervisor.session_display_environment", lambda: {})
    monkeypatch.setattr(
        "voice.desktop.supervisor.display_is_listening", lambda _display: False)
    monkeypatch.setattr("voice.desktop.supervisor.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "voice.desktop.supervisor.time.monotonic", lambda: next(clock))

    assert wait_for_display({"DISPLAY": ":0", "XAUTHORITY": "/x"}, timeout=5.0) is False


def test_an_empty_display_is_never_probed() -> None:
    assert display_is_listening("") is False
    assert display_is_listening("localhost") is False


def test_the_session_environment_reader_ignores_everything_else(monkeypatch) -> None:
    class _Result:
        stdout = "LANG=en_CA.UTF-8\nDISPLAY=:1\nXAUTHORITY=/run/x\nPATH=/usr/bin\n"

    monkeypatch.setattr(
        "voice.desktop.supervisor.subprocess.run", lambda *a, **k: _Result())

    assert session_display_environment() == {"DISPLAY": ":1", "XAUTHORITY": "/run/x"}


def test_a_screenless_session_still_gets_her_voice(tmp_path: Path, monkeypatch) -> None:
    """No window is a cosmetic loss. Silence is not."""

    spawned: list[FakeProcess] = []
    items = (
        Component("desk-voice", ("desk",), tmp_path),
        Component("brain-bridge", ("bridge",), tmp_path),
        Component("dot-display", ("display",), tmp_path),
    )
    monkeypatch.setattr(
        "voice.desktop.supervisor.subprocess.Popen", _fake_popen_factory(spawned))
    monkeypatch.setattr("voice.desktop.supervisor.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "voice.desktop.supervisor.wait_for_display", lambda environment, **_: False)
    supervisor = VoiceAppSupervisor(items)

    supervisor.start()

    assert [process.command[0] for process in spawned] == ["desk", "bridge"]
    assert set(supervisor.processes) == {"desk-voice", "brain-bridge"}


def test_a_crashed_overlay_is_reopened_and_leaves_the_voice_alone(
    tmp_path: Path,
    monkeypatch,
) -> None:
    spawned: list[FakeProcess] = []
    items = (
        Component("desk-voice", ("desk",), tmp_path),
        Component("dot-display", ("display",), tmp_path),
    )
    monkeypatch.setattr(
        "voice.desktop.supervisor.subprocess.Popen", _fake_popen_factory(spawned))
    monkeypatch.setattr("voice.desktop.supervisor.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "voice.desktop.supervisor.DOT_DISPLAY_RESTART_BACKOFF_SECONDS", 0.0)
    supervisor = VoiceAppSupervisor(items)
    supervisor.start()
    # SIGTRAP is exactly how the Electron binary died on this laptop.
    spawned[-1].return_code = -signal.SIGTRAP

    assert supervisor.restart_dot_display() is True
    assert [process.command[0] for process in spawned] == ["desk", "display", "display"]
    assert supervisor.processes["desk-voice"] is spawned[0]


def test_an_overlay_that_keeps_crashing_stops_being_reopened(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Bounded, so a broken Electron cannot spin the laptop's fans all night."""

    spawned: list[FakeProcess] = []
    items = (Component("dot-display", ("display",), tmp_path),)
    monkeypatch.setattr(
        "voice.desktop.supervisor.subprocess.Popen", _fake_popen_factory(spawned))
    monkeypatch.setattr("voice.desktop.supervisor.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "voice.desktop.supervisor.DOT_DISPLAY_RESTART_BACKOFF_SECONDS", 0.0)
    supervisor = VoiceAppSupervisor(items)

    attempts = [supervisor.restart_dot_display() for _ in range(5)]

    assert attempts == [True, True, True, False, False]


class _QuitsAfterStartup(FakeProcess):
    """Alive when start() checks it, gone by the time run() looks again."""

    def __init__(self, command, **kwargs):
        super().__init__(command, **kwargs)
        self.polls = 0

    def poll(self):
        self.polls += 1
        if self.command[0] == "display" and self.polls > 1:
            return 0
        return self.return_code


def test_the_tray_quit_still_ends_the_whole_app(tmp_path: Path, monkeypatch) -> None:
    """Exit zero is Raghav choosing Quit, and that must still stop everything."""

    items = (
        Component("desk-voice", ("desk",), tmp_path),
        Component("dot-display", ("display",), tmp_path),
    )
    monkeypatch.setattr(
        "voice.desktop.supervisor.subprocess.Popen", _QuitsAfterStartup)
    monkeypatch.setattr("voice.desktop.supervisor.time.sleep", lambda _seconds: None)
    supervisor = VoiceAppSupervisor(items)

    assert supervisor.run() == 0


def test_a_crash_in_the_overlay_never_takes_the_app_down_with_it(
    tmp_path: Path, monkeypatch
) -> None:
    """The unit stayed dead for five days because this path returned early."""

    class _Crashes(_QuitsAfterStartup):
        def poll(self):
            self.polls += 1
            if self.command[0] == "display" and self.polls > 1:
                return -signal.SIGTRAP
            return self.return_code

    items = (
        Component("desk-voice", ("desk",), tmp_path),
        Component("dot-display", ("display",), tmp_path),
    )
    monkeypatch.setattr("voice.desktop.supervisor.subprocess.Popen", _Crashes)
    monkeypatch.setattr("voice.desktop.supervisor.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "voice.desktop.supervisor.DOT_DISPLAY_RESTART_BACKOFF_SECONDS", 0.0)
    supervisor = VoiceAppSupervisor(items)

    def give_up_then_let_the_test_end(_environment, **_kwargs):
        supervisor.stopping.set()
        return False

    monkeypatch.setattr(
        "voice.desktop.supervisor.wait_for_display", give_up_then_let_the_test_end)

    assert supervisor.run() == 0
    assert "desk-voice" in supervisor.processes
