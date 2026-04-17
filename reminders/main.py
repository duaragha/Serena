#!/usr/bin/env python3
"""Reminder daemon — polls for due reminders, listens for events, fires alerts."""

import logging
import sys
import threading
import time

from db import get_due_reminders, mark_fired, get_pending_by_trigger
from inputs.google_tasks import poll_google_tasks
from inputs.ntfy_listener import listen_for_events
from outputs import fire_reminder
from config import POLL_INTERVAL, NTFY_INPUT_TOPIC

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("daemon")


def scheduler_loop():
    """Check for due time-based reminders every POLL_INTERVAL seconds."""
    log.info(f"Scheduler started (checking every {POLL_INTERVAL}s)")
    while True:
        try:
            due = get_due_reminders()
            for reminder in due:
                log.info(f"Reminder #{reminder['id']} is due: {reminder['message']}")
                fire_reminder(reminder["message"])
                mark_fired(reminder["id"])
        except Exception as e:
            log.error(f"Scheduler error: {e}")
        time.sleep(POLL_INTERVAL)


def google_tasks_loop():
    """Poll Google Tasks every POLL_INTERVAL seconds."""
    log.info(f"Google Tasks poller started (every {POLL_INTERVAL}s)")
    while True:
        try:
            poll_google_tasks()
        except Exception as e:
            log.error(f"Google Tasks poller error: {e}")
        time.sleep(POLL_INTERVAL)


def main():
    log.info("=== Reminder Daemon Starting ===")

    # Start scheduler (checks for due time-based reminders)
    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True, name="scheduler")
    scheduler_thread.start()

    # Start Google Tasks poller
    gtasks_thread = threading.Thread(target=google_tasks_loop, daemon=True, name="gtasks")
    gtasks_thread.start()

    # Start ntfy listener for Tasker events (runs in foreground since it's blocking)
    if NTFY_INPUT_TOPIC:
        ntfy_thread = threading.Thread(target=listen_for_events, daemon=True, name="ntfy-listener")
        ntfy_thread.start()
        log.info("ntfy listener started")
    else:
        log.warning("NTFY_INPUT_TOPIC not set — Tasker event listener disabled")

    log.info("All components running. Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        sys.exit(0)


if __name__ == "__main__":
    main()
