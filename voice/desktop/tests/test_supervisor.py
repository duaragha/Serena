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
