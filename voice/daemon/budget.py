"""Interrupt budget manager — controls proactive message delivery.

Prevents Serena from being annoying by enforcing daily/hourly caps,
cooldown periods, quiet hours, and focus mode filtering.
"""

from __future__ import annotations

import enum
import logging
from datetime import datetime, timedelta

from serena.config import DaemonConfig

logger = logging.getLogger(__name__)


class Priority(enum.IntEnum):
    """Interrupt priority levels (lower value = higher priority)."""

    CRITICAL = 1  # Always deliver (safety, urgent alerts)
    HIGH = 2      # Notification + sound
    MEDIUM = 3    # Silent notification
    LOW = 4       # Queue for next user interaction


class InterruptBudget:
    """Tracks and enforces limits on proactive interrupts.

    Checks daily caps, hourly caps, cooldown timers, quiet hours,
    and focus mode before allowing a message through. CRITICAL priority
    always bypasses rate limits (but is still recorded for accounting).
    """

    def __init__(self, config: DaemonConfig) -> None:
        self._config = config
        self._focus_mode: bool = False

        # Timestamps of all delivered interrupts (pruned beyond 24h)
        self._history: list[datetime] = []

        logger.info(
            "InterruptBudget initialized — daily_cap=%d, hourly_cap=%d, "
            "cooldown=%d min, quiet=%s-%s",
            config.daily_message_cap,
            config.hourly_message_cap,
            config.cooldown_minutes,
            config.quiet_hours_start,
            config.quiet_hours_end,
        )

    @property
    def focus_mode(self) -> bool:
        """Whether focus mode is currently active."""
        return self._focus_mode

    def set_focus_mode(self, enabled: bool) -> None:
        """Toggle focus mode. When on, MEDIUM and LOW interrupts are blocked."""
        self._focus_mode = enabled
        logger.info("Focus mode %s", "enabled" if enabled else "disabled")

    def can_interrupt(self, priority: Priority) -> bool:
        """Check whether an interrupt at the given priority is allowed.

        CRITICAL always returns True. All other priorities are checked
        against quiet hours, focus mode, daily/hourly caps, and cooldown.

        Args:
            priority: The priority level of the proposed interrupt.

        Returns:
            True if the interrupt should be delivered, False otherwise.
        """
        now = datetime.now()
        self._prune_history(now)

        # CRITICAL bypasses all rate limits
        if priority == Priority.CRITICAL:
            return True

        # Quiet hours — only CRITICAL passes (already handled above)
        if self._in_quiet_hours(now):
            logger.debug(
                "Blocked %s interrupt during quiet hours (%s-%s)",
                priority.name,
                self._config.quiet_hours_start,
                self._config.quiet_hours_end,
            )
            return False

        # Focus mode — block MEDIUM and LOW
        if self._focus_mode and priority >= Priority.MEDIUM:
            logger.debug("Blocked %s interrupt — focus mode active", priority.name)
            return False

        # Daily cap
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        daily_count = sum(1 for ts in self._history if ts >= day_start)
        if daily_count >= self._config.daily_message_cap:
            logger.debug(
                "Blocked %s interrupt — daily cap reached (%d/%d)",
                priority.name,
                daily_count,
                self._config.daily_message_cap,
            )
            return False

        # Hourly cap
        hour_ago = now - timedelta(hours=1)
        hourly_count = sum(1 for ts in self._history if ts >= hour_ago)
        if hourly_count >= self._config.hourly_message_cap:
            logger.debug(
                "Blocked %s interrupt — hourly cap reached (%d/%d)",
                priority.name,
                hourly_count,
                self._config.hourly_message_cap,
            )
            return False

        # Cooldown — minimum time since last interrupt
        if self._history:
            elapsed = now - self._history[-1]
            cooldown = timedelta(minutes=self._config.cooldown_minutes)
            if elapsed < cooldown:
                remaining = cooldown - elapsed
                logger.debug(
                    "Blocked %s interrupt — cooldown active (%.0f s remaining)",
                    priority.name,
                    remaining.total_seconds(),
                )
                return False

        return True

    def record_interrupt(self, priority: Priority) -> None:
        """Record that an interrupt was delivered.

        Should be called after successful delivery regardless of priority,
        so accounting stays accurate.

        Args:
            priority: The priority level of the delivered interrupt.
        """
        now = datetime.now()
        self._history.append(now)
        logger.info(
            "Recorded %s interrupt at %s (today: %d, last hour: %d)",
            priority.name,
            now.strftime("%H:%M:%S"),
            self._daily_count(now),
            self._hourly_count(now),
        )

    def _in_quiet_hours(self, now: datetime) -> bool:
        """Check if the current time falls within the configured quiet window."""
        quiet_start = self._parse_time(self._config.quiet_hours_start)
        quiet_end = self._parse_time(self._config.quiet_hours_end)

        current = now.hour * 60 + now.minute

        if quiet_start <= quiet_end:
            # Same-day range (e.g., 13:00-17:00)
            return quiet_start <= current < quiet_end
        else:
            # Overnight range (e.g., 23:00-07:00)
            return current >= quiet_start or current < quiet_end

    def _prune_history(self, now: datetime) -> None:
        """Remove entries older than 24 hours."""
        cutoff = now - timedelta(hours=24)
        before = len(self._history)
        self._history = [ts for ts in self._history if ts >= cutoff]
        pruned = before - len(self._history)
        if pruned > 0:
            logger.debug("Pruned %d entries older than 24h from interrupt history", pruned)

    def _daily_count(self, now: datetime) -> int:
        """Count interrupts since midnight."""
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return sum(1 for ts in self._history if ts >= day_start)

    def _hourly_count(self, now: datetime) -> int:
        """Count interrupts in the last 60 minutes."""
        hour_ago = now - timedelta(hours=1)
        return sum(1 for ts in self._history if ts >= hour_ago)

    @staticmethod
    def _parse_time(time_str: str) -> int:
        """Parse 'HH:MM' string to minutes since midnight."""
        parts = time_str.split(":")
        return int(parts[0]) * 60 + int(parts[1])
