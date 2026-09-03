import logging
import httpx
from config import NTFY_ALERT_TOPIC

log = logging.getLogger(__name__)

NTFY_URL = "https://ntfy.sh"


def send_alert(message: str):
    if not NTFY_ALERT_TOPIC:
        log.warning("ntfy alert topic not configured, skipping notification")
        return

    try:
        resp = httpx.post(
            f"{NTFY_URL}/{NTFY_ALERT_TOPIC}",
            content=message,
            headers={
                "Title": "Reminder",
                "Priority": "5",
                "Tags": "bell,warning",
            },
            timeout=10,
        )
        resp.raise_for_status()
        log.info(f"ntfy alert sent: {message[:50]}")
    except Exception as e:
        log.error(f"ntfy alert failed: {e}")
