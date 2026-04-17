import json
import logging
import httpx
from config import NTFY_INPUT_TOPIC
from parser import parse_reminder
from db import add_reminder, get_pending_by_trigger
from outputs import fire_reminder
from db import mark_fired

log = logging.getLogger(__name__)

NTFY_URL = "https://ntfy.sh"


def listen_for_events():
    """Subscribe to ntfy input topic via SSE. Receives events from Tasker.

    Event types from Tasker:
    - reminder: new reminder text to parse and store
    - payment: payment event detected, fire all pending payment reminders
    """
    if not NTFY_INPUT_TOPIC:
        log.warning("ntfy input topic not configured, skipping listener")
        return

    url = f"{NTFY_URL}/{NTFY_INPUT_TOPIC}/json"
    log.info(f"Listening for Tasker events on ntfy topic: {NTFY_INPUT_TOPIC}")

    while True:
        try:
            with httpx.stream("GET", url, timeout=None) as response:
                for line in response.iter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if data.get("event") == "keepalive":
                        continue

                    _handle_event(data)

        except (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            log.warning(f"ntfy connection lost ({e}), reconnecting in 5s...")
            import time
            time.sleep(5)
        except Exception as e:
            log.error(f"ntfy listener error: {e}")
            import time
            time.sleep(5)


def _handle_event(data: dict):
    message = data.get("message", "").strip()
    title = data.get("title", "").strip().lower()

    if not message:
        return

    if title == "payment":
        # Payment event from Tasker — fire all pending payment reminders
        log.info("Payment event received")
        pending = get_pending_by_trigger("payment")
        for reminder in pending:
            log.info(f"Firing payment reminder #{reminder['id']}: {reminder['message']}")
            fire_reminder(reminder["message"])
            mark_fired(reminder["id"])
        if not pending:
            log.info("No pending payment reminders")
    else:
        # New reminder from Tasker (SMS interception or voice input)
        log.info(f"New reminder from Tasker: {message}")
        parsed = parse_reminder(message)
        rid = add_reminder(
            message=parsed.message,
            trigger_type=parsed.trigger_type,
            trigger_at=parsed.trigger_at,
            source="sms",
        )
        log.info(f"Reminder #{rid} created: {parsed.trigger_type} — {parsed.message}")

        # If immediate, fire right away
        if parsed.trigger_type == "immediate":
            fire_reminder(parsed.message)
            mark_fired(rid)
