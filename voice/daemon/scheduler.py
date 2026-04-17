"""Proactive daemon scheduler — runs scheduled and event-driven jobs.

Manages APScheduler within the application's asyncio event loop,
registering cron-based and interval-based jobs for morning briefings,
evening summaries, calendar polling, and other proactive behaviors.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from serena.brain.claude import ClaudeBrain
from serena.config import SerenaConfig
from serena.daemon.budget import InterruptBudget, Priority
from serena.voice.tts import TextToSpeech

logger = logging.getLogger(__name__)

# Type alias for job callbacks: async functions returning a message string
# (or None to skip delivery).
JobCallback = Callable[[], Awaitable[str | None]]


class ProactiveDaemon:
    """Daemon that proactively initiates interactions on schedules and events.

    Embeds into the main asyncio event loop via APScheduler's
    AsyncIOScheduler (v3.x). Jobs produce messages that are routed
    through the interrupt budget before delivery via TTS or logging.
    """

    def __init__(
        self,
        config: SerenaConfig,
        brain: ClaudeBrain,
        tts: TextToSpeech,
        budget: InterruptBudget,
    ) -> None:
        self._config = config
        self._brain = brain
        self._tts = tts
        self._budget = budget

        self._scheduler = AsyncIOScheduler()

        # Pluggable callbacks for each job. Defaults are placeholders that
        # return a static message. Override via set_*_callback() before or
        # after start() — the jobs always call the current callback reference.
        self._morning_briefing_cb: JobCallback = self._default_morning_briefing
        self._evening_summary_cb: JobCallback = self._default_evening_summary
        self._calendar_poll_cb: JobCallback = self._default_calendar_poll

        logger.info("ProactiveDaemon initialized")

    # -- Callback setters --------------------------------------------------

    def set_morning_briefing_callback(self, cb: JobCallback) -> None:
        """Replace the morning briefing job implementation."""
        self._morning_briefing_cb = cb

    def set_evening_summary_callback(self, cb: JobCallback) -> None:
        """Replace the evening summary job implementation."""
        self._evening_summary_cb = cb

    def set_calendar_poll_callback(self, cb: JobCallback) -> None:
        """Replace the calendar poll job implementation."""
        self._calendar_poll_cb = cb

    # -- Lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Register all jobs and start the scheduler.

        Safe to call from a running asyncio event loop — the scheduler
        attaches to the current loop via AsyncIOScheduler.
        """
        self._register_jobs()
        self._scheduler.start()
        logger.info("ProactiveDaemon started — %d jobs registered", len(self._scheduler.get_jobs()))

    async def stop(self) -> None:
        """Gracefully shut down the scheduler.

        Waits for any currently-executing jobs to finish before returning.
        """
        self._scheduler.shutdown(wait=True)
        logger.info("ProactiveDaemon stopped")

    # -- Job registration --------------------------------------------------

    def _register_jobs(self) -> None:
        """Set up all scheduled jobs from config."""
        daemon_cfg = self._config.daemon

        # Morning briefing — cron trigger at configured HH:MM
        hour, minute = self._parse_hhmm(daemon_cfg.morning_briefing)
        self._scheduler.add_job(
            self._run_morning_briefing,
            trigger=CronTrigger(hour=hour, minute=minute),
            id="morning_briefing",
            name="Morning briefing",
            replace_existing=True,
        )
        logger.info("Registered morning briefing at %02d:%02d", hour, minute)

        # Evening summary — cron trigger at configured HH:MM
        hour, minute = self._parse_hhmm(daemon_cfg.evening_summary)
        self._scheduler.add_job(
            self._run_evening_summary,
            trigger=CronTrigger(hour=hour, minute=minute),
            id="evening_summary",
            name="Evening summary",
            replace_existing=True,
        )
        logger.info("Registered evening summary at %02d:%02d", hour, minute)

        # Calendar poll — interval trigger
        poll_minutes = self._config.calendar.poll_interval_minutes
        self._scheduler.add_job(
            self._run_calendar_poll,
            trigger=IntervalTrigger(minutes=poll_minutes),
            id="calendar_poll",
            name="Calendar poll",
            replace_existing=True,
        )
        logger.info("Registered calendar poll every %d min", poll_minutes)

    # -- Job runners -------------------------------------------------------

    async def _run_morning_briefing(self) -> None:
        """Execute the morning briefing callback and deliver the result."""
        logger.info("Running morning briefing job")
        try:
            message = await self._morning_briefing_cb()
            if message:
                await self._deliver(message, Priority.HIGH)
        except Exception:
            logger.exception("Morning briefing job failed")

    async def _run_evening_summary(self) -> None:
        """Execute the evening summary callback and deliver the result."""
        logger.info("Running evening summary job")
        try:
            message = await self._evening_summary_cb()
            if message:
                await self._deliver(message, Priority.MEDIUM)
        except Exception:
            logger.exception("Evening summary job failed")

    async def _run_calendar_poll(self) -> None:
        """Execute the calendar poll callback and deliver any result."""
        logger.debug("Running calendar poll job")
        try:
            message = await self._calendar_poll_cb()
            if message:
                await self._deliver(message, Priority.HIGH)
        except Exception:
            logger.exception("Calendar poll job failed")

    # -- Delivery ----------------------------------------------------------

    async def _deliver(self, message: str, priority: Priority) -> None:
        """Check budget and deliver a proactive message.

        CRITICAL and HIGH priorities are spoken via TTS.
        MEDIUM and LOW are logged only (notification delivery will be
        added when the UI layer is integrated).

        Args:
            message: The text to deliver.
            priority: Delivery priority level.
        """
        if not self._budget.can_interrupt(priority):
            logger.info(
                "Proactive message suppressed by budget (priority=%s): %.80s...",
                priority.name,
                message,
            )
            return

        self._budget.record_interrupt(priority)

        if priority <= Priority.HIGH:
            # Speak it aloud
            logger.info("Delivering %s message via TTS: %.80s...", priority.name, message)
            try:
                await self._tts.speak(message)
            except Exception:
                logger.exception("TTS delivery failed for %s message", priority.name)
        else:
            # MEDIUM / LOW — log for now; UI notification layer will handle these
            logger.info(
                "Queued %s message (silent): %.120s...",
                priority.name,
                message,
            )

    # -- Default placeholder callbacks -------------------------------------

    @staticmethod
    async def _default_morning_briefing() -> str | None:
        """Placeholder: returns a static message until the real briefing is wired."""
        logger.debug("Morning briefing callback not set — using placeholder")
        return "Good morning! Your briefing is not configured yet."

    @staticmethod
    async def _default_evening_summary() -> str | None:
        """Placeholder: returns a static message until the real summary is wired."""
        logger.debug("Evening summary callback not set — using placeholder")
        return "Good evening! Your daily summary is not configured yet."

    @staticmethod
    async def _default_calendar_poll() -> str | None:
        """Placeholder: returns None (no message) until calendar polling is wired."""
        logger.debug("Calendar poll callback not set — using placeholder")
        return None

    # -- Helpers -----------------------------------------------------------

    @staticmethod
    def _parse_hhmm(time_str: str) -> tuple[int, int]:
        """Parse 'HH:MM' into (hour, minute) integers."""
        parts = time_str.split(":")
        return int(parts[0]), int(parts[1])
