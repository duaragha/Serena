"""ntfy.sh mobile notifications for critical proactive alerts."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_NTFY_BASE = "https://ntfy.sh"
_TIMEOUT = 10.0  # seconds


class NtfyNotifier:
    """Send push notifications to a phone via ntfy.sh.

    If the topic is empty or unset, all send calls are silently skipped.
    """

    def __init__(self, topic: str = "") -> None:
        self._topic = topic.strip()
        if self._topic:
            logger.info("NtfyNotifier ready — topic=%s", self._topic)
        else:
            logger.info("NtfyNotifier disabled — no topic configured")

    @property
    def enabled(self) -> bool:
        return bool(self._topic)

    async def send(self, title: str, message: str, priority: int = 3) -> None:
        """Push a notification to ntfy.sh.

        Args:
            title: Notification title (shown as heading on the phone).
            message: Body text.
            priority: 1 (min) to 5 (max).  Default 3 (normal).

        Silently skips if no topic is configured.  Logs but does not
        raise on HTTP errors so callers never crash from notification
        failures.
        """
        if not self._topic:
            return

        priority = max(1, min(5, priority))
        url = f"{_NTFY_BASE}/{self._topic}"

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    url,
                    content=message.encode("utf-8"),
                    headers={
                        "Title": title,
                        "Priority": str(priority),
                    },
                )
                resp.raise_for_status()
                logger.info(
                    "ntfy notification sent — title=%r, priority=%d",
                    title,
                    priority,
                )
        except httpx.HTTPStatusError as exc:
            logger.error(
                "ntfy HTTP error %d: %s",
                exc.response.status_code,
                exc.response.text[:200],
            )
        except httpx.HTTPError:
            logger.exception("Failed to send ntfy notification")
