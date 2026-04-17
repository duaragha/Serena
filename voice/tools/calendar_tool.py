"""Google Calendar integration for Serena.

Provides read-only access to the user's Google Calendar via OAuth2.
Credentials are stored locally and tokens are auto-refreshed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from serena.config import CalendarConfig

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_PATH = Path.home() / ".config" / "serena" / "token.json"


def _format_time_12h(dt: datetime) -> str:
    """Format a datetime as 12-hour time, e.g. '2:30 PM'."""
    return dt.strftime("%-I:%M %p")


def _build_service(credentials_path: Path):
    """Build and return a Google Calendar API service object.

    Returns None if credentials are missing or invalid.
    """
    # Import here to avoid import errors when google libs aren't installed
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds: Credentials | None = None

    # Load existing token
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    # Refresh or report missing credentials
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
            logger.info("Google Calendar token refreshed")
        except Exception:
            logger.exception("Failed to refresh Google Calendar token")
            return None
    elif not creds or not creds.valid:
        if not credentials_path.exists():
            logger.warning(
                "Google Calendar credentials not found at %s. "
                "Run 'python -m serena.scripts.setup_google_oauth' to set up authentication.",
                credentials_path,
            )
        else:
            logger.warning(
                "Google Calendar token is invalid or missing. "
                "Run 'python -m serena.scripts.setup_google_oauth' to re-authenticate.",
            )
        return None

    return build("calendar", "v3", credentials=creds)


def _parse_event(event: dict[str, Any]) -> dict[str, Any]:
    """Parse a Google Calendar API event into a clean dict.

    Handles both all-day events (date) and timed events (dateTime).
    """
    start_raw = event.get("start", {})
    end_raw = event.get("end", {})

    all_day = "date" in start_raw and "dateTime" not in start_raw

    if all_day:
        start_str = start_raw["date"]
        end_str = end_raw.get("date", start_str)
        start_display = "All day"
        end_display = ""
    else:
        start_dt = datetime.fromisoformat(start_raw["dateTime"])
        end_dt = datetime.fromisoformat(end_raw["dateTime"])
        start_str = start_raw["dateTime"]
        end_str = end_raw["dateTime"]
        start_display = _format_time_12h(start_dt)
        end_display = _format_time_12h(end_dt)

    attendees_raw = event.get("attendees", [])
    attendees = [
        a.get("displayName") or a.get("email", "")
        for a in attendees_raw
        if not a.get("self", False)
    ]

    return {
        "summary": event.get("summary", "(No title)"),
        "start": start_str,
        "end": end_str,
        "start_display": start_display,
        "end_display": end_display,
        "all_day": all_day,
        "location": event.get("location", ""),
        "attendees": attendees,
    }


class CalendarTool:
    """Read-only Google Calendar integration.

    Uses OAuth2 credentials stored locally. If credentials are not
    configured, methods return empty results and log a setup message
    rather than crashing.
    """

    def __init__(self, config: CalendarConfig | None = None) -> None:
        self._config = config or CalendarConfig()
        self._credentials_path = Path(self._config.credentials_path).expanduser()
        self._service = None
        self._service_checked = False

    def _get_service(self):
        """Lazily build the Calendar API service."""
        if not self._service_checked:
            self._service = _build_service(self._credentials_path)
            self._service_checked = True
        return self._service

    async def get_upcoming(self, hours: int = 24) -> list[dict[str, Any]]:
        """Fetch events in the next N hours.

        Args:
            hours: Number of hours ahead to look (default 24).

        Returns:
            List of event dicts with keys: summary, start, end,
            start_display, end_display, all_day, location, attendees.
        """
        service = await asyncio.to_thread(self._get_service)
        if service is None:
            return []

        now = datetime.now(timezone.utc)
        time_min = now.isoformat()
        time_max = (now + timedelta(hours=hours)).isoformat()

        try:
            result = await asyncio.to_thread(
                lambda: service.events()
                .list(
                    calendarId="primary",
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=50,
                )
                .execute()
            )
        except Exception:
            logger.exception("Failed to fetch upcoming calendar events")
            return []

        events = result.get("items", [])
        return [_parse_event(e) for e in events]

    async def get_today(self) -> list[dict[str, Any]]:
        """Fetch all of today's events (midnight to midnight local time).

        Returns:
            List of event dicts for today.
        """
        service = await asyncio.to_thread(self._get_service)
        if service is None:
            return []

        now = datetime.now().astimezone()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(days=1)

        try:
            result = await asyncio.to_thread(
                lambda: service.events()
                .list(
                    calendarId="primary",
                    timeMin=start_of_day.isoformat(),
                    timeMax=end_of_day.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=50,
                )
                .execute()
            )
        except Exception:
            logger.exception("Failed to fetch today's calendar events")
            return []

        events = result.get("items", [])
        return [_parse_event(e) for e in events]

    async def get_summary(self) -> str:
        """Generate a voice-friendly summary of today's events.

        Returns:
            A natural-language string like "You have 3 events today.
            First up is standup at 9:30 AM, then..."
        """
        events = await self.get_today()

        if not events:
            return "You have no events scheduled for today."

        count = len(events)
        event_word = "event" if count == 1 else "events"
        parts = [f"You have {count} {event_word} today."]

        for i, event in enumerate(events):
            name = event["summary"]
            if event["all_day"]:
                time_desc = "all day"
            else:
                time_desc = f"at {event['start_display']}"

            if i == 0:
                prefix = "First up is"
            elif i == len(events) - 1:
                prefix = "and finally"
            else:
                prefix = "then"

            parts.append(f"{prefix} {name} {time_desc}")

            # Add location for non-all-day events if present
            if event["location"] and not event["all_day"]:
                parts[-1] += f" at {event['location']}"

            parts[-1] += "."

        return " ".join(parts)
