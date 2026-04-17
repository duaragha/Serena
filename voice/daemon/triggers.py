"""Event-driven triggers — screen unlock greeting, email monitoring."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

from serena.config import SerenaConfig
from serena.daemon.budget import Priority

logger = logging.getLogger(__name__)

# Minimum gap between screen-unlock greetings (seconds).
_GREETING_COOLDOWN = 2 * 60 * 60  # 2 hours

# IMAP IDLE renewal interval.  RFC 2177 requires renewal before 29 min;
# most servers drop after 10 min.  Renew at 9 min to stay safe.
_IDLE_RENEW_SECONDS = 9 * 60


class EventTriggers:
    """Monitors system events and fires a callback when something interesting happens.

    Parameters
    ----------
    config:
        Full Serena configuration (uses ``config.email`` for IMAP).
    on_event:
        ``async (message: str, priority: Priority) -> None`` — called when a
        trigger fires.
    """

    def __init__(
        self,
        config: SerenaConfig,
        on_event: Callable[[str, Priority], Awaitable[None]],
    ) -> None:
        self._config = config
        self._on_event = on_event
        self._tasks: list[asyncio.Task] = []
        self._last_greeting_ts: float = 0.0
        self._imap_client = None  # aioimaplib IMAP4_SSL instance
        self._dbus_bus = None     # dbus_next MessageBus connection

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch all event monitors as background tasks."""
        self._tasks.append(asyncio.create_task(self._run_screen_monitor()))
        self._tasks.append(asyncio.create_task(self._run_email_monitor()))
        logger.info("Event triggers started")

    async def stop(self) -> None:
        """Cancel monitors and release connections."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        if self._dbus_bus:
            try:
                self._dbus_bus.disconnect()
            except Exception:
                pass
            self._dbus_bus = None

        if self._imap_client:
            try:
                await self._imap_client.logout()
            except Exception:
                pass
            self._imap_client = None

        logger.info("Event triggers stopped")

    # ------------------------------------------------------------------
    # Screen unlock detection (Task 3.5)
    # ------------------------------------------------------------------

    async def start_screen_monitor(self) -> None:
        """Public entry for screen monitoring (delegates to internal task)."""
        await self._run_screen_monitor()

    async def _run_screen_monitor(self) -> None:
        """Listen for screen unlock events via D-Bus.

        Tries two sources:
        1. GNOME ScreenSaver — ``org.gnome.ScreenSaver.ActiveChanged`` on the
           session bus (``False`` = screen unlocked).
        2. logind — ``org.freedesktop.login1.Session.Lock`` / ``Unlock`` on the
           system bus.

        On unlock, emits a time-of-day greeting at MEDIUM priority, rate-limited
        to once every 2 hours.
        """
        try:
            from dbus_next.aio import MessageBus
            from dbus_next import BusType, Message, MessageType
        except ImportError:
            logger.warning("dbus-next not installed — screen unlock trigger disabled")
            return

        # Try GNOME ScreenSaver on the session bus first (most common desktop).
        try:
            bus = await MessageBus(bus_type=BusType.SESSION).connect()
            self._dbus_bus = bus
            logger.info("Connected to D-Bus session bus for screen-unlock monitoring")

            # Subscribe to ActiveChanged on the GNOME ScreenSaver interface.
            reply = await bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="AddMatch",
                    signature="s",
                    body=[
                        "type='signal',"
                        "interface='org.gnome.ScreenSaver',"
                        "member='ActiveChanged'"
                    ],
                )
            )

            if reply.message_type == MessageType.ERROR:
                logger.debug("Could not subscribe to GNOME ScreenSaver: %s", reply.body)
                raise RuntimeError("GNOME ScreenSaver match failed")

            # Also try logind session unlock on the system bus as a fallback signal.
            # We add a match for the Unlock signal on any session path.
            try:
                await bus.call(
                    Message(
                        destination="org.freedesktop.DBus",
                        path="/org/freedesktop/DBus",
                        interface="org.freedesktop.DBus",
                        member="AddMatch",
                        signature="s",
                        body=[
                            "type='signal',"
                            "interface='org.freedesktop.login1.Session',"
                            "member='Unlock'"
                        ],
                    )
                )
            except Exception:
                # Not critical — GNOME signal is the primary one.
                pass

            def on_message(msg: Message) -> None:
                if msg.member == "ActiveChanged" and msg.body:
                    # body[0] is bool: True = screen locked, False = unlocked
                    if not msg.body[0]:
                        asyncio.get_event_loop().create_task(self._on_screen_unlock())
                elif msg.member == "Unlock":
                    asyncio.get_event_loop().create_task(self._on_screen_unlock())

            bus.add_message_handler(on_message)

            # Keep the connection alive — wait forever (cancelled on stop()).
            await asyncio.Event().wait()

        except Exception as exc:
            if self._dbus_bus:
                try:
                    self._dbus_bus.disconnect()
                except Exception:
                    pass
                self._dbus_bus = None
            logger.info("GNOME ScreenSaver unavailable (%s), trying logind on system bus", exc)

        # Fallback: logind Unlock signal on the system bus.
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            self._dbus_bus = bus
            logger.info("Connected to D-Bus system bus for logind unlock monitoring")

            await bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="AddMatch",
                    signature="s",
                    body=[
                        "type='signal',"
                        "interface='org.freedesktop.login1.Session',"
                        "member='Unlock'"
                    ],
                )
            )

            def on_logind_message(msg: Message) -> None:
                if msg.member == "Unlock":
                    asyncio.get_event_loop().create_task(self._on_screen_unlock())

            bus.add_message_handler(on_logind_message)
            await asyncio.Event().wait()

        except Exception as exc:
            logger.warning("D-Bus screen-unlock monitoring unavailable: %s", exc)

    async def _on_screen_unlock(self) -> None:
        """Handle a screen unlock — greet if enough time has passed."""
        now = time.monotonic()
        if now - self._last_greeting_ts < _GREETING_COOLDOWN:
            logger.debug("Skipping unlock greeting (%.0f min since last)",
                         (now - self._last_greeting_ts) / 60)
            return

        self._last_greeting_ts = now
        greeting = self._time_of_day_greeting()
        logger.info("Screen unlocked — greeting: %s", greeting)
        await self._on_event(greeting, Priority.MEDIUM)

    @staticmethod
    def _time_of_day_greeting() -> str:
        hour = datetime.now().hour
        if 5 <= hour < 12:
            return "good morning"
        if 12 <= hour < 17:
            return "good afternoon"
        if 17 <= hour < 21:
            return "good evening"
        return "hey, still up?"

    # ------------------------------------------------------------------
    # Email monitoring via IMAP IDLE (Task 3.6)
    # ------------------------------------------------------------------

    async def start_email_monitor(self) -> None:
        """Public entry for email monitoring (delegates to internal task)."""
        await self._run_email_monitor()

    async def _run_email_monitor(self) -> None:
        """Watch for new emails using IMAP IDLE.

        Connects to the configured IMAP server, selects INBOX, and enters
        IDLE mode.  When new mail arrives, fetches subject + sender and
        fires the event callback.  Renews IDLE every 9 minutes per RFC 2177.

        If email is not configured (empty server or username), exits silently.
        """
        email_cfg = self._config.email
        if not email_cfg.imap_server or not email_cfg.username:
            logger.debug("Email not configured — IMAP monitor disabled")
            return

        try:
            from aioimaplib import IMAP4_SSL
        except ImportError:
            logger.warning("aioimaplib not installed — email monitor disabled")
            return

        while True:
            try:
                await self._imap_idle_loop(email_cfg)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("IMAP connection lost — reconnecting in 30s")
                self._imap_client = None
                await asyncio.sleep(30)

    async def _imap_idle_loop(self, email_cfg) -> None:
        """Core IMAP IDLE loop — connect, select inbox, idle, process."""
        from aioimaplib import IMAP4_SSL

        logger.info("Connecting to IMAP server %s...", email_cfg.imap_server)
        client = IMAP4_SSL(host=email_cfg.imap_server)
        await client.wait_hello_from_server()
        self._imap_client = client

        await client.login(email_cfg.username, email_cfg.app_password)
        await client.select("INBOX")
        logger.info("IMAP connected — monitoring %s", email_cfg.username)

        while True:
            # Enter IDLE and wait for server push or our own timeout.
            idle_task = await client.idle_start(timeout=_IDLE_RENEW_SECONDS)
            msg = await client.wait_server_push()

            # Stop the IDLE command cleanly before processing.
            client.idle_done()
            await asyncio.wait_for(idle_task, timeout=10)

            # Check if we got a new-mail notification.
            if self._has_new_mail(msg):
                await self._process_new_emails(client)

    @staticmethod
    def _has_new_mail(push_messages: list) -> bool:
        """Check IMAP server push responses for EXISTS (new mail indicator)."""
        for item in push_messages:
            if isinstance(item, bytes):
                item = item.decode("utf-8", errors="replace")
            if isinstance(item, str) and "EXISTS" in item.upper():
                return True
        return False

    async def _process_new_emails(self, client) -> None:
        """Fetch the latest unseen email and fire the event callback."""
        try:
            status, data = await client.search("UNSEEN")
            if status != "OK" or not data or not data[0]:
                return

            # data[0] is a space-separated list of message UIDs.
            uids_str = data[0] if isinstance(data[0], str) else data[0].decode()
            uids = uids_str.strip().split()
            if not uids:
                return

            # Fetch the most recent unseen message's envelope.
            latest_uid = uids[-1]
            status, msg_data = await client.fetch(
                latest_uid, "(BODY[HEADER.FIELDS (FROM SUBJECT)])"
            )
            if status != "OK" or not msg_data:
                return

            sender, subject = self._parse_email_header(msg_data)
            message = f"new email from {sender}: {subject}" if sender else f"new email: {subject}"
            logger.info("New email detected — %s", message)
            await self._on_event(message, Priority.MEDIUM)

        except Exception:
            logger.exception("Failed to process new email notification")

    @staticmethod
    def _parse_email_header(msg_data) -> tuple[str, str]:
        """Extract From and Subject from a FETCH response."""
        sender = ""
        subject = ""

        # msg_data is a list of response lines; find the header block.
        raw = ""
        for part in msg_data:
            if isinstance(part, bytes):
                raw += part.decode("utf-8", errors="replace")
            elif isinstance(part, str):
                raw += part

        for line in raw.splitlines():
            lower = line.lower()
            if lower.startswith("from:"):
                sender = line[5:].strip()
            elif lower.startswith("subject:"):
                subject = line[8:].strip()

        return sender, subject
