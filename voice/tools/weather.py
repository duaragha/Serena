"""Weather tool using the Open-Meteo API (free, no API key required)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from serena.config import WeatherConfig

logger = logging.getLogger(__name__)

API_URL = "https://api.open-meteo.com/v1/forecast"

# WMO Weather Interpretation Codes (WW)
# https://open-meteo.com/en/docs#weathervariables
WMO_CODES: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snowfall",
    73: "moderate snowfall",
    75: "heavy snowfall",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


def _describe_weather_code(code: int) -> str:
    """Convert a WMO weather code to a human-readable description."""
    return WMO_CODES.get(code, "unknown conditions")


class WeatherTool:
    """Fetches current weather and forecasts from Open-Meteo."""

    def __init__(self, config: WeatherConfig | None = None) -> None:
        self._config = config or WeatherConfig()
        self._client = httpx.AsyncClient(timeout=10.0)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    def _base_params(self) -> dict[str, Any]:
        """Build common query parameters for the API."""
        params: dict[str, Any] = {
            "latitude": self._config.latitude,
            "longitude": self._config.longitude,
            "timezone": "auto",
        }
        if self._config.units == "fahrenheit":
            params["temperature_unit"] = "fahrenheit"
            params["wind_speed_unit"] = "mph"
        # Celsius + km/h are the API defaults, no param needed.
        return params

    @property
    def _unit_label(self) -> str:
        return "F" if self._config.units == "fahrenheit" else "C"

    @property
    def _speed_label(self) -> str:
        return "mph" if self._config.units == "fahrenheit" else "km/h"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_current(self) -> dict[str, Any]:
        """Return current weather conditions.

        Returns a dict with keys: temperature, feels_like, conditions,
        humidity, wind_speed, weather_code.
        """
        params = self._base_params()
        params["current"] = (
            "temperature_2m,apparent_temperature,weather_code,"
            "relative_humidity_2m,wind_speed_10m"
        )

        logger.debug("Fetching current weather for (%.2f, %.2f)", self._config.latitude, self._config.longitude)
        resp = await self._client.get(API_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

        current = data["current"]
        code = int(current["weather_code"])

        return {
            "temperature": current["temperature_2m"],
            "feels_like": current["apparent_temperature"],
            "conditions": _describe_weather_code(code),
            "humidity": current["relative_humidity_2m"],
            "wind_speed": current["wind_speed_10m"],
            "weather_code": code,
        }

    async def get_forecast(self, days: int = 3) -> list[dict[str, Any]]:
        """Return a daily forecast for the next *days* days.

        Each entry: date, high, low, conditions, precipitation_chance, weather_code.
        """
        days = max(1, min(days, 16))  # API supports 1-16

        params = self._base_params()
        params["daily"] = (
            "weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max"
        )
        params["forecast_days"] = days

        logger.debug("Fetching %d-day forecast", days)
        resp = await self._client.get(API_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

        daily = data["daily"]
        forecast: list[dict[str, Any]] = []
        for i in range(len(daily["time"])):
            code = int(daily["weather_code"][i])
            forecast.append({
                "date": daily["time"][i],
                "high": daily["temperature_2m_max"][i],
                "low": daily["temperature_2m_min"][i],
                "conditions": _describe_weather_code(code),
                "precipitation_chance": daily["precipitation_probability_max"][i],
                "weather_code": code,
            })

        return forecast

    async def get_summary(self) -> str:
        """Return a natural-language weather summary suitable for voice output."""
        current = await self.get_current()
        forecast = await self.get_forecast(days=1)

        unit = self._unit_label
        temp = round(current["temperature"])
        conditions = current["conditions"]
        humidity = current["humidity"]
        wind = round(current["wind_speed"])
        speed_label = self._speed_label

        parts = [
            f"It's currently {temp} degrees {unit} and {conditions}.",
        ]

        if forecast:
            today = forecast[0]
            high = round(today["high"])
            low = round(today["low"])
            parts.append(f"Today's high is {high}, low is {low}.")
            if today["precipitation_chance"] and today["precipitation_chance"] > 30:
                parts.append(
                    f"There's a {today['precipitation_chance']}% chance of precipitation."
                )

        parts.append(f"Humidity is {humidity}% with wind at {wind} {speed_label}.")

        return " ".join(parts)
