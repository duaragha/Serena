"""Run Serena's desk voice loop and dot display as one lifecycle."""

from __future__ import annotations

import argparse
import os
import signal
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

    def start(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        for item in self.items:
            if item.name == "dot-display":
                # Electron can monopolize the laptop during its cold start.
                # Let the awake voice process reach first PCM before opening it.
                time.sleep(DOT_DISPLAY_DELAY_SECONDS)
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
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
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
