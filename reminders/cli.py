#!/usr/bin/env python3
"""CLI for managing reminders.

Usage:
    remind add "get chili flakes and parmesan" --when payment
    remind add "take out the trash" --at "7pm"
    remind add "call mom" --in "20 minutes"
    remind add "pick up groceries"  (fires immediately)
    remind list
    remind list --pending
    remind cancel 3
"""

import argparse
import sys
from datetime import datetime, timezone

import dateparser

from db import add_reminder, list_reminders, cancel_reminder
from outputs import fire_reminder
from db import mark_fired


def cmd_add(args):
    message = args.message
    trigger_type = "immediate"
    trigger_at = None

    if args.when:
        trigger_type = args.when  # 'payment' etc
    elif args.at:
        trigger_at = dateparser.parse(
            args.at,
            settings={
                "PREFER_DATES_FROM": "future",
                "RETURN_AS_TIMEZONE_AWARE": True,
                "TIMEZONE": "America/Toronto",
                "TO_TIMEZONE": "UTC",
            },
        )
        if not trigger_at:
            print(f"Could not parse time: {args.at}")
            sys.exit(1)
        trigger_type = "time"
    elif args.in_time:
        trigger_at = dateparser.parse(
            f"in {args.in_time}",
            settings={
                "PREFER_DATES_FROM": "future",
                "RETURN_AS_TIMEZONE_AWARE": True,
                "TIMEZONE": "America/Toronto",
                "TO_TIMEZONE": "UTC",
            },
        )
        if not trigger_at:
            print(f"Could not parse duration: {args.in_time}")
            sys.exit(1)
        trigger_type = "time"

    rid = add_reminder(message=message, trigger_type=trigger_type, trigger_at=trigger_at, source="cli")

    if trigger_type == "immediate":
        fire_reminder(message)
        mark_fired(rid)
        print(f"Reminder #{rid} fired immediately: {message}")
    elif trigger_type == "time":
        local_time = trigger_at.astimezone()
        print(f"Reminder #{rid} set for {local_time.strftime('%I:%M %p %b %d')}: {message}")
    else:
        print(f"Reminder #{rid} will fire on {trigger_type}: {message}")


def cmd_list(args):
    status = "pending" if args.pending else None
    reminders = list_reminders(status)

    if not reminders:
        print("No reminders found.")
        return

    for r in reminders:
        trigger_info = r["trigger_type"]
        if r["trigger_at"]:
            t = datetime.fromisoformat(r["trigger_at"]).astimezone()
            trigger_info = t.strftime("%I:%M %p %b %d")

        status_icon = {"pending": "⏳", "fired": "✅", "cancelled": "❌"}.get(r["status"], "?")
        print(f"  {status_icon} #{r['id']}  {r['message']}  [{trigger_info}]  ({r['source']})")


def cmd_cancel(args):
    if cancel_reminder(args.id):
        print(f"Reminder #{args.id} cancelled.")
    else:
        print(f"Reminder #{args.id} not found or already fired.")


def main():
    parser = argparse.ArgumentParser(prog="remind", description="Manage reminders")
    sub = parser.add_subparsers(dest="command", required=True)

    # add
    add_parser = sub.add_parser("add", help="Add a reminder")
    add_parser.add_argument("message", help="Reminder message")
    add_parser.add_argument("--when", help="Event trigger (e.g., 'payment')")
    add_parser.add_argument("--at", help="Time trigger (e.g., '7pm', 'tomorrow 3pm')")
    add_parser.add_argument("--in", dest="in_time", help="Duration (e.g., '20 minutes', '2 hours')")
    add_parser.set_defaults(func=cmd_add)

    # list
    list_parser = sub.add_parser("list", help="List reminders")
    list_parser.add_argument("--pending", action="store_true", help="Show only pending")
    list_parser.set_defaults(func=cmd_list)

    # cancel
    cancel_parser = sub.add_parser("cancel", help="Cancel a reminder")
    cancel_parser.add_argument("id", type=int, help="Reminder ID")
    cancel_parser.set_defaults(func=cmd_cancel)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
