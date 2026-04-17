"""Proactive briefing generators for Serena's daemon.

Generates morning briefings, pre-meeting prep, and evening summaries by
gathering data from weather and calendar tools and passing it through
Claude for natural-language synthesis.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from serena.brain.claude import ClaudeBrain
from serena.config import SerenaConfig
from serena.tools.calendar_tool import CalendarTool
from serena.tools.weather import WeatherTool

logger = logging.getLogger(__name__)


def _format_event_for_prompt(event: dict[str, Any]) -> str:
    """Format a calendar event dict into a readable string for Claude."""
    summary = event.get("summary", "(No title)")
    start = event.get("start_display", event.get("start", ""))
    location = event.get("location", "")
    attendees = event.get("attendees", [])

    parts = [f"- {summary}"]
    if event.get("all_day"):
        parts.append("(all day)")
    elif start:
        parts.append(f"at {start}")
    if location:
        parts.append(f"at {location}")
    if attendees:
        parts.append(f"with {', '.join(attendees)}")
    return " ".join(parts)


def _format_events_block(events: list[dict[str, Any]]) -> str:
    """Format a list of calendar events into a prompt-ready block."""
    if not events:
        return "No events scheduled."
    return "\n".join(_format_event_for_prompt(e) for e in events)


def _format_forecast_for_prompt(forecast: dict[str, Any]) -> str:
    """Format a single forecast day dict into a readable string."""
    high = round(forecast.get("high", 0))
    low = round(forecast.get("low", 0))
    conditions = forecast.get("conditions", "unknown")
    precip = forecast.get("precipitation_chance")
    text = f"High {high}, low {low}, {conditions}."
    if precip and precip > 30:
        text += f" {precip}% chance of precipitation."
    return text


class BriefingGenerator:
    """Generates proactive briefings by combining tool data with Claude synthesis.

    Gathers raw data from weather and calendar tools directly (bypassing
    Claude tool_use for speed), then passes assembled data to Claude for
    natural-language generation.
    """

    def __init__(self, config: SerenaConfig, brain: ClaudeBrain) -> None:
        self._config = config
        self._brain = brain
        self._weather = WeatherTool(config.weather)
        self._calendar = CalendarTool(config.calendar)

    async def close(self) -> None:
        """Release underlying resources."""
        await self._weather.close()

    # ------------------------------------------------------------------
    # Morning Briefing (Task 3.3)
    # ------------------------------------------------------------------

    async def generate_morning_briefing(self) -> str:
        """Generate a concise morning briefing with weather and today's calendar.

        Gathers weather and calendar data, then asks Claude to synthesize
        a natural spoken-style greeting. Degrades gracefully if either
        data source fails.
        """
        weather_text = await self._fetch_weather_summary()
        calendar_text = await self._fetch_today_calendar()

        data_parts: list[str] = []
        if weather_text:
            data_parts.append(f"Weather: {weather_text}")
        if calendar_text:
            data_parts.append(f"Today's calendar:\n{calendar_text}")

        if not data_parts:
            # Both sources failed -- still produce something useful
            data_block = "(No weather or calendar data available right now.)"
        else:
            data_block = "\n\n".join(data_parts)

        prompt = (
            f"Generate a concise morning briefing for Raghav with this data:\n\n"
            f"{data_block}\n\n"
            f"Make it natural and spoken, like a personal assistant greeting "
            f"in the morning. 2-3 sentences. Don't use markdown."
        )

        return await self._synthesize(prompt, fallback="Good morning, Raghav.")

    # ------------------------------------------------------------------
    # Pre-Meeting Prep (Task 3.4)
    # ------------------------------------------------------------------

    async def generate_meeting_prep(self, event: dict[str, Any]) -> str:
        """Generate a brief prep summary for an upcoming meeting.

        Args:
            event: Calendar event dict with keys like summary, start,
                   start_display, attendees, location, etc.
        """
        summary = event.get("summary", "(No title)")
        start = event.get("start_display", event.get("start", ""))
        location = event.get("location", "")
        attendees = event.get("attendees", [])

        details_parts = [f"Meeting: {summary}"]
        if start:
            details_parts.append(f"Time: {start}")
        if location:
            details_parts.append(f"Location: {location}")
        if attendees:
            details_parts.append(f"Attendees: {', '.join(attendees)}")

        details = "\n".join(details_parts)

        prompt = (
            f"Raghav has a meeting coming up:\n\n{details}\n\n"
            f"Give a brief prep summary — who's attending, what it's "
            f"about, any relevant context. 2-3 sentences, spoken style. "
            f"Don't use markdown."
        )

        return await self._synthesize(
            prompt,
            fallback=f"You have {summary} coming up at {start}.",
        )

    async def check_upcoming_meetings(self) -> list[tuple[dict[str, Any], str]]:
        """Check for meetings in the next N minutes and generate prep for each.

        Looks ahead by ``config.daemon.meeting_prep_minutes`` (default 15).

        Returns:
            List of (event_dict, briefing_text) tuples for each upcoming
            meeting. Empty list if no meetings are approaching or if the
            calendar is unavailable.
        """
        prep_minutes = self._config.daemon.meeting_prep_minutes
        try:
            # Fetch events within the prep window. get_upcoming takes hours,
            # so convert minutes to a fractional hour (round up to avoid
            # missing events right at the boundary).
            hours = max(1, (prep_minutes + 59) // 60)
            events = await self._calendar.get_upcoming(hours=hours)
        except Exception:
            logger.exception("Failed to fetch upcoming events for meeting prep")
            return []

        if not events:
            return []

        now = datetime.now().astimezone()
        window_end = now + timedelta(minutes=prep_minutes)

        results: list[tuple[dict[str, Any], str]] = []
        for event in events:
            if event.get("all_day"):
                continue

            try:
                event_start = datetime.fromisoformat(event["start"])
            except (KeyError, ValueError):
                continue

            # Only include events that start within the prep window and
            # haven't already started.
            if now < event_start <= window_end:
                briefing = await self.generate_meeting_prep(event)
                results.append((event, briefing))

        return results

    # ------------------------------------------------------------------
    # Evening Summary (Task 3.7)
    # ------------------------------------------------------------------

    async def generate_evening_summary(self) -> str:
        """Generate a brief end-of-day summary with tomorrow's preview.

        Gathers tomorrow's calendar and weather forecast, then asks Claude
        to wrap up the day naturally. Degrades gracefully if data sources
        fail.
        """
        tomorrow_calendar = await self._fetch_tomorrow_calendar()
        tomorrow_weather = await self._fetch_tomorrow_forecast()

        data_parts: list[str] = []
        if tomorrow_calendar:
            data_parts.append(f"Tomorrow's schedule:\n{tomorrow_calendar}")
        if tomorrow_weather:
            data_parts.append(f"Tomorrow's weather: {tomorrow_weather}")

        if not data_parts:
            data_block = "(No calendar or weather data available for tomorrow.)"
        else:
            data_block = "\n\n".join(data_parts)

        prompt = (
            f"Generate a brief end-of-day summary for Raghav.\n\n"
            f"{data_block}\n\n"
            f"Wrap up naturally — mention if tomorrow is busy or light, "
            f"any early meetings to prepare for. 2-3 sentences. "
            f"Don't use markdown."
        )

        return await self._synthesize(prompt, fallback="That's a wrap for today, Raghav. Have a good evening.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _synthesize(self, prompt: str, *, fallback: str) -> str:
        """Pass a prompt through Claude and return the response text.

        On failure, logs the error and returns the fallback string so the
        caller always gets a usable briefing.
        """
        try:
            response = await self._brain.think(prompt)
            return response.strip()
        except Exception:
            logger.exception("Claude synthesis failed for briefing")
            return fallback

    async def _fetch_weather_summary(self) -> str:
        """Fetch current weather summary, returning empty string on failure."""
        try:
            return await self._weather.get_summary()
        except Exception:
            logger.exception("Failed to fetch weather summary for briefing")
            return ""

    async def _fetch_today_calendar(self) -> str:
        """Fetch today's calendar events formatted for a prompt."""
        try:
            events = await self._calendar.get_today()
            return _format_events_block(events)
        except Exception:
            logger.exception("Failed to fetch today's calendar for briefing")
            return ""

    async def _fetch_tomorrow_calendar(self) -> str:
        """Fetch tomorrow's calendar events formatted for a prompt."""
        try:
            # get_upcoming looks ahead N hours from now. To cover tomorrow
            # fully, fetch 48 hours and filter to only tomorrow's date.
            events = await self._calendar.get_upcoming(hours=48)
            tomorrow = (datetime.now().astimezone() + timedelta(days=1)).date()

            tomorrow_events: list[dict[str, Any]] = []
            for event in events:
                try:
                    raw_start = event.get("start", "")
                    # Timed events have full ISO datetime, all-day events
                    # have a bare date string (YYYY-MM-DD).
                    event_date = datetime.fromisoformat(raw_start).date()
                except (ValueError, TypeError):
                    # Bare date string from an all-day event
                    try:
                        event_date = datetime.strptime(raw_start, "%Y-%m-%d").date()
                    except (ValueError, TypeError):
                        continue

                if event_date == tomorrow:
                    tomorrow_events.append(event)

            return _format_events_block(tomorrow_events)
        except Exception:
            logger.exception("Failed to fetch tomorrow's calendar for briefing")
            return ""

    async def _fetch_tomorrow_forecast(self) -> str:
        """Fetch tomorrow's weather forecast as a readable string."""
        try:
            # Request a 2-day forecast: index 0 = today, index 1 = tomorrow
            forecast = await self._weather.get_forecast(days=2)
            if len(forecast) >= 2:
                return _format_forecast_for_prompt(forecast[1])
            return ""
        except Exception:
            logger.exception("Failed to fetch tomorrow's forecast for briefing")
            return ""
