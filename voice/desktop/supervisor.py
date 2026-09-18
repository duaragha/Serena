"""Run Serena's desk voice loop and dot display as one lifecycle."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_ROOT = REPO_ROOT / "voice" / "desktop"
DESK_PYTHON = REPO_ROOT / "voice" / ".venv-wake" / "bin" / "python"
ELECTRON = DESKTOP_ROOT / "node_modules" / ".bin" / "electron"
MANIFEST = Path.home() / ".config" / "serena" / "wakeword-acceptance.json"
DOT_DISPLAY_DELAY_SECONDS = 1.0
# --start-awake runs exactly one conversation and returns. Dropping the flag
# puts the same client back on the wake word instead.
START_AWAKE = "--start-awake"
REARM_MIN_UPTIME_SECONDS = 5.0
REARM_MAX_CONSECUTIVE_FAILURES = 3
# Cinnamon publishes DISPLAY into the user manager only once the desktop is
# up, which is strictly after wireplumber pulls this unit in at login. So the
# app can be started into a session that has a perfectly healthy X server it
# simply cannot see yet, and Electron aborts on sight. Waiting is the whole
# fix; two minutes is far longer than a cold boot to desktop.
DISPLAY_WAIT_SECONDS = 120.0
DISPLAY_POLL_SECONDS = 1.0
DISPLAY_CONNECT_TIMEOUT_SECONDS = 2.0
DOT_DISPLAY_MAX_RESTARTS = 3
DOT_DISPLAY_RESTART_BACKOFF_SECONDS = 5.0


def session_display_environment() -> dict[str, str]:
    """DISPLAY and XAUTHORITY exactly as the desktop session published them.

    The session pushes them into the systemd user manager when it comes up.
    Reading them back is how a unit that started before the desktop finds the
    screen it is supposed to draw on, instead of inheriting an empty DISPLAY
    for the rest of its life.
    """

    try:
        result = subprocess.run(
            ("systemctl", "--user", "show-environment"),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    published: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key.strip() in ("DISPLAY", "XAUTHORITY") and value.strip():
            published[key.strip()] = value.strip()
    return published


def display_is_listening(display: str) -> bool:
    """True when something actually answers on that display.

    An exported DISPLAY says the session means to have an X server, not that
    it has one yet. PrivateTmp hides /tmp/.X11-unix from this unit, so the
    abstract socket is the one that can be reached from in here; the path is
    tried anyway for the sandboxes that do pass it through.
    """

    number = display.strip().partition(":")[2].partition(".")[0]
    if not number.isdigit():
        return False
    for address in (f"\0/tmp/.X11-unix/X{number}", f"/tmp/.X11-unix/X{number}"):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(DISPLAY_CONNECT_TIMEOUT_SECONDS)
            probe.connect(address)
            return True
        except OSError:
            continue
        finally:
            probe.close()
    return False


def wait_for_display(
    environment: dict[str, str], *, timeout: float = DISPLAY_WAIT_SECONDS
) -> bool:
    """Block until a window can actually be opened, filling in the address.

    ``environment`` is updated in place, so the caller hands the resolved
    DISPLAY and XAUTHORITY straight to Electron.
    """

    deadline = time.monotonic() + timeout
    while True:
        if not (environment.get("DISPLAY") or "").strip() or not (
                environment.get("XAUTHORITY") or "").strip():
            for key, value in session_display_environment().items():
                if not (environment.get(key) or "").strip():
                    environment[key] = value
        display = (environment.get("DISPLAY") or "").strip()
        if display and display_is_listening(display):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(DISPLAY_POLL_SECONDS)


@dataclass(frozen=True, slots=True)
class Component:
    name: str
    command: tuple[str, ...]
    cwd: Path


def components() -> tuple[Component, ...]:
    return (
        Component(
            "desk-voice",
            (
                str(DESK_PYTHON),
                "-u",
                "-m",
                "voice.desk.client",
                "--manifest",
                str(MANIFEST),
                "--start-awake",
            ),
            REPO_ROOT,
        ),
        Component(
            "brain-bridge",
            (sys.executable, "-u", "-m", "voice.brain_bridge"),
            REPO_ROOT,
        ),
        Component(
            "dot-display",
            (str(ELECTRON), ".", "--ozone-platform-hint=auto"),
            DESKTOP_ROOT,
        ),
    )


def validate_components(items: tuple[Component, ...] | None = None) -> None:
    selected = items or components()
    required = (DESK_PYTHON, ELECTRON, MANIFEST)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("missing Serena voice runtime asset: " + ", ".join(missing))
    for item in selected:
        if not item.cwd.is_dir():
            raise FileNotFoundError(f"missing working directory for {item.name}: {item.cwd}")


class VoiceAppSupervisor:
    def __init__(self, items: tuple[Component, ...] | None = None) -> None:
        self.items = items or components()
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.stopping = threading.Event()
        self.rearm_failures = 0
        self._rearmed_at = 0.0
        self.environment = os.environ.copy()
        self.environment["PYTHONUNBUFFERED"] = "1"
        self.dot_restarts = 0

    def start(self) -> None:
        environment = self.environment
        for item in self.items:
            if item.name == "dot-display":
                # Electron can monopolize the laptop during its cold start.
                # Let the awake voice process reach first PCM before opening it.
                time.sleep(DOT_DISPLAY_DELAY_SECONDS)
                if not wait_for_display(environment):
                    # Her voice is the point; the dot field is how it looks.
                    # A headless or still-booting session costs the overlay,
                    # never the ears.
                    print(
                        "[voice-app] no X server to draw on; running voice and "
                        "the bridge without the dot field",
                        flush=True,
                    )
                    continue
            process = subprocess.Popen(
                item.command,
                cwd=item.cwd,
                env=environment,
                start_new_session=True,
            )
            self.processes[item.name] = process
            print(f"[voice-app] started {item.name} pid={process.pid}", flush=True)
            time.sleep(0.1)
            return_code = process.poll()
            if return_code is not None:
                raise RuntimeError(
                    f"{item.name} exited during startup with status {return_code}"
                )

    def _rearm_is_flapping(self) -> bool:
        """A wake-armed client that keeps dying instantly must not spin.

        Only a rearmed client counts. The first conversation is launched awake
        and is supposed to end, so it never looks like a failure.
        """
        if not self._rearmed_at:
            return False
        if time.monotonic() - self._rearmed_at >= REARM_MIN_UPTIME_SECONDS:
            self.rearm_failures = 0
            return False
        self.rearm_failures += 1
        return self.rearm_failures >= REARM_MAX_CONSECUTIVE_FAILURES

    def rearm_wake(self) -> bool:
        """Put the microphone back on the wake word after a conversation.

        Without this the app launches awake, has exactly one conversation, and
        is then deaf for as long as it stays up: the voice client has returned,
        and the standalone wake listener only takes over once the whole unit
        stops, which it no longer does. Two turns in and she stops answering.
        """
        item = next((entry for entry in self.items if entry.name == "desk-voice"), None)
        if item is None:
            return False
        command = tuple(part for part in item.command if part != START_AWAKE)
        environment = self.environment
        try:
            process = subprocess.Popen(
                command, cwd=item.cwd, env=environment, start_new_session=True
            )
        except OSError as exc:
            print(f"[voice-app] could not rearm the wake word: {exc}", flush=True)
            return False
        self.processes["desk-voice"] = process
        self._rearmed_at = time.monotonic()
        print(
            f"[voice-app] listening for 'hey serena' again pid={process.pid}",
            flush=True,
        )
        return True

    def restart_dot_display(self) -> bool:
        """Reopen the overlay after a crash, bounded so it cannot spin.

        A crashed overlay used to take her voice down with it, which is the
        wrong trade every time: the ears work with or without a window.
        """

        item = next(
            (entry for entry in self.items if entry.name == "dot-display"), None)
        if item is None or self.dot_restarts >= DOT_DISPLAY_MAX_RESTARTS:
            return False
        self.dot_restarts += 1
        if self.stopping.wait(DOT_DISPLAY_RESTART_BACKOFF_SECONDS):
            return False
        if not wait_for_display(self.environment):
            return False
        try:
            process = subprocess.Popen(
                item.command,
                cwd=item.cwd,
                env=self.environment,
                start_new_session=True,
            )
        except OSError as exc:
            print(f"[voice-app] could not reopen the dot field: {exc}", flush=True)
            return False
        self.processes["dot-display"] = process
        print(f"[voice-app] reopened the dot field pid={process.pid}", flush=True)
        return True

    def forward_session_close(self) -> None:
        process = self.processes.get("desk-voice")
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGUSR1)
        except ProcessLookupError:
            return

    def stop(self) -> None:
        self.stopping.set()
        alive = [process for process in self.processes.values() if process.poll() is None]
        for process in alive:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
        deadline = time.monotonic() + 10.0
        for process in alive:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
        self.processes.clear()

    def run(self) -> int:
        self.start()
        while not self.stopping.wait(0.1):
            for name, process in self.processes.items():
                return_code = process.poll()
                if return_code is None:
                    continue
                if name == "desk-voice" and return_code == 0:
                    # A spoken conversation ending is not the app ending. The
                    # overlay carries the type bar, which is exactly what is
                    # needed when the microphone is the thing misbehaving, so
                    # tearing it down here would remove the fallback at the
                    # only moment it matters.
                    self.processes.pop(name, None)
                    if self._rearm_is_flapping():
                        print(
                            "[voice-app] wake rearm keeps failing; keeping the "
                            "overlay and bridge up for typing only",
                            flush=True,
                        )
                        break
                    if not self.rearm_wake():
                        print(
                            "[voice-app] keeping the overlay and bridge up for typing",
                            flush=True,
                        )
                    break
                if name == "dot-display" and return_code != 0:
                    # Exit zero is the tray's own Quit and still ends the app.
                    # Anything else is a crash, and a crash is the overlay's
                    # problem alone.
                    self.processes.pop(name, None)
                    if not self.restart_dot_display():
                        print(
                            "[voice-app] the dot field stays down; voice and "
                            "the bridge keep running",
                            flush=True,
                        )
                    break
                print(
                    f"[voice-app] {name} exited with status {return_code}; "
                    "stopping the paired voice app",
                    flush=True,
                )
                return return_code if return_code else 0
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the installed voice runtime without starting it",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_components()
    if args.check:
        print("Serena voice app runtime is ready")
        return 0

    supervisor = VoiceAppSupervisor()

    def request_stop(_signum: int, _frame: object) -> None:
        supervisor.stopping.set()

    def request_session_close(_signum: int, _frame: object) -> None:
        supervisor.forward_session_close()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGUSR1, request_session_close)
    try:
        return supervisor.run()
    finally:
        supervisor.stop()


if __name__ == "__main__":
    raise SystemExit(main())
