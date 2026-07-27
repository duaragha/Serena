from __future__ import annotations

import signal
from pathlib import Path

from voice.desktop.supervisor import Component, VoiceAppSupervisor, components


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
