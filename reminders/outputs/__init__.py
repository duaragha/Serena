import logging
import threading
from outputs.twilio_call import call_with_reminder
from outputs.ntfy_alert import send_alert

log = logging.getLogger(__name__)


def fire_reminder(message: str):
    """Fire both Twilio call and ntfy notification simultaneously."""
    log.info(f"Firing reminder: {message}")

    call_thread = threading.Thread(target=call_with_reminder, args=(message,), daemon=True)
    ntfy_thread = threading.Thread(target=send_alert, args=(message,), daemon=True)

    call_thread.start()
    ntfy_thread.start()

    call_thread.join(timeout=15)
    ntfy_thread.join(timeout=15)
