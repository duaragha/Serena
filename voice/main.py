"""Serena -- Always-on voice AI desktop assistant.

Wires all components: voice loop, proactive daemon, briefing generator,
event triggers, IPC server, and tool registry into a single asyncio
application with graceful shutdown on SIGINT/SIGTERM.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import subprocess
from pathlib import Path

from serena.config import load_config

PROJECT_ROOT = Path(__file__).parent.parent

_electron_proc: subprocess.Popen | None = None


def _start_electron() -> None:
    """Launch the Electron overlay as a child process."""
    global _electron_proc
    ui_dir = PROJECT_ROOT / "ui"
    if not (ui_dir / "node_modules").exists():
        logger.warning("Electron node_modules not found — skipping overlay")
        return
    try:
        # Capture electron output to a log file for debugging
        electron_log = open("/tmp/serena_electron.log", "w")
        _electron_proc = subprocess.Popen(
            ["npx", "electron", ".", "--enable-logging"],
            cwd=str(ui_dir),
            stdout=electron_log,
            stderr=electron_log,
        )
        logger.info("Electron overlay started (PID %d)", _electron_proc.pid)
    except Exception:
        logger.warning("Failed to start Electron overlay", exc_info=True)


def _stop_electron() -> None:
    """Kill the Electron overlay."""
    global _electron_proc
    if _electron_proc and _electron_proc.poll() is None:
        _electron_proc.terminate()
        try:
            _electron_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _electron_proc.kill()
        logger.info("Electron overlay stopped")
    _electron_proc = None

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quieten chatty libraries
    for name in ("httpx", "anthropic", "faster_whisper", "websockets"):
        logging.getLogger(name).setLevel(logging.WARNING)


async def run() -> None:
    config = load_config(PROJECT_ROOT / "config.yaml")

    print()
    print("  \u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557")
    print("  \u2551            S E R E N A               \u2551")
    print("  \u2551     Always-On Voice Assistant         \u2551")
    print("  \u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d")
    print()

    # -- Lazy imports to keep startup fast and isolated from import errors ---
    from serena.brain.claude import ClaudeBrain
    from serena.daemon.briefings import BriefingGenerator
    from serena.daemon.budget import InterruptBudget, Priority
    from serena.daemon.scheduler import ProactiveDaemon
    from serena.daemon.triggers import EventTriggers
    from serena.ipc.server import IPCServer
    from serena.tools.registry import create_tool_registry
    from serena.voice.loop import VoiceLoop
    from serena.voice.tts import TextToSpeech

    # 1. Tool registry + brain
    tool_registry, code_tool = create_tool_registry(config)
    brain = ClaudeBrain(config.llm, tool_registry=tool_registry)

    # 2. TTS instance (shared between voice loop and proactive daemon)
    tts = TextToSpeech(config.tts)

    # 3. Interrupt budget
    budget = InterruptBudget(config.daemon)

    # 4. IPC server (Electron overlay communication)
    ipc = IPCServer()

    # 4.5. Wire up CodeTool narration now that brain/tts/ipc exist
    code_tool.set_narration(brain, tts, ipc)

    # 5. Proactive daemon
    daemon = ProactiveDaemon(config, brain, tts, budget)

    # 6. Briefing generator -- wire callbacks into the daemon
    briefings = BriefingGenerator(config, brain)

    daemon.set_morning_briefing_callback(briefings.generate_morning_briefing)
    daemon.set_evening_summary_callback(briefings.generate_evening_summary)

    async def calendar_poll_callback() -> str | None:
        """Poll for upcoming meetings and deliver prep for each one."""
        results = await briefings.check_upcoming_meetings()
        if not results:
            return None
        # Deliver each meeting prep individually via the daemon, return
        # the first one as the callback result (the rest are delivered
        # directly so they each get their own budget check).
        first_message: str | None = None
        for _event, briefing_text in results:
            if first_message is None:
                first_message = briefing_text
            else:
                await daemon._deliver(briefing_text, Priority.HIGH)
        return first_message

    daemon.set_calendar_poll_callback(calendar_poll_callback)

    # 7. Event triggers (screen unlock, email monitoring)
    async def on_event(message: str, priority: Priority) -> None:
        await daemon._deliver(message, priority)

    triggers = EventTriggers(config, on_event)

    # 8. Voice loop with IPC integration
    voice = VoiceLoop(config, brain, ipc=ipc)

    # 9. Wire CodeTool into voice loop for voice commands (stop, status, etc.)
    voice.set_code_tool(code_tool)

    # -- Shutdown orchestration -----------------------------------------------

    shutdown = asyncio.Event()

    def handle_signal() -> None:
        print("\n  Shutting down...")
        shutdown.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    # -- Start everything -----------------------------------------------------

    try:
        await ipc.start()
        _start_electron()
        await daemon.start()
        await triggers.start()
        await voice.start()

        logger.info("All systems online")
        await shutdown.wait()

    finally:
        await voice.stop()
        await triggers.stop()
        await daemon.stop()
        await briefings.close()
        _stop_electron()
        await ipc.stop()
        print("  Goodbye.\n")


def main() -> None:
    setup_logging()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
