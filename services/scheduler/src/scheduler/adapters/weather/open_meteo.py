"""Open-Meteo forecast API (https://open-meteo.com/en/docs), the default weather provider.

One request per location returns hourly ``precipitation`` (mm, sum of the preceding hour),
``precipitation_probability`` (%, chance of more than 0.1 mm in the preceding hour) and ``rain``
(mm) for yesterday, today and tomorrow (``past_days=1``, ``forecast_days=2``) with Unix
timestamps. No API key is needed for non-commercial use (limits: 600 calls per minute, 5 000
per hour, 10 000 per day; data CC BY 4.0, attribution "Weather data by Open-Meteo.com").

Answers are cached per location (rounded to 0.01 degrees, about 1 km) for
``cache_seconds`` (default 30 minutes), so even hundreds of schedules stay far below the limits;
failures are cached for ``failure_cache_seconds`` so an outage is not hammered. Any failure
(timeout, HTTP error, unexpected JSON) means "unknown weather" (None), never an exception.
"""

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from mowdeck_service_kit import get_logger

from scheduler.domain.rain import Forecast, HourlyPoint

_log = get_logger("scheduler.weather")

DEFAULT_BASE_URL = "https://api.open-meteo.com"
HOURLY = "precipitation,precipitation_probability,rain"
ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0)"


def _number(values: Any, index: int) -> float | None:
    if not isinstance(values, list) or index >= len(values):
        return None
    value = values[index]
    return float(value) if isinstance(value, int | float) else None


def parse_forecast(body: Mapping[str, Any], fetched_at: datetime) -> Forecast:
    """A :class:`Forecast` from an Open-Meteo response with ``timeformat=unixtime``.

    Raises ``ValueError``, ``KeyError`` or ``TypeError`` for an answer it cannot use (a number
    beyond what a float or a datetime holds is a ``ValueError``).
    """
    try:
        return _parse_forecast(body, fetched_at)
    except (OverflowError, OSError) as exc:
        msg = "forecast value out of range"
        raise ValueError(msg) from exc


def _parse_forecast(body: Mapping[str, Any], fetched_at: datetime) -> Forecast:
    hourly = body["hourly"]
    times = hourly["time"]
    if not isinstance(times, list):
        msg = "hourly.time is not a list"
        raise TypeError(msg)
    points = tuple(
        HourlyPoint(
            end=datetime.fromtimestamp(int(stamp), UTC),
            precipitation_mm=_number(hourly.get("precipitation"), index),
            probability_percent=_number(hourly.get("precipitation_probability"), index),
            rain_mm=_number(hourly.get("rain"), index),
        )
        for index, stamp in enumerate(times)
    )
    return Forecast(
        latitude=float(body["latitude"]),
        longitude=float(body["longitude"]),
        points=points,
        source="open_meteo",
        fetched_at=fetched_at,
    )


class OpenMeteoProvider:
    """``WeatherProvider`` over the Open-Meteo forecast API."""

    name = "open_meteo"

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 5.0,
        cache_seconds: float = 1800.0,
        failure_cache_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.cache_seconds = cache_seconds
        self.failure_cache_seconds = failure_cache_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self._cache: dict[tuple[float, float], tuple[datetime, Forecast | None]] = {}
        self._lock = asyncio.Lock()
        self.requests = 0

    async def forecast(self, latitude: float, longitude: float) -> Forecast | None:
        """The cached or freshly fetched forecast; None when Open-Meteo cannot answer."""
        key = (round(latitude, 2), round(longitude, 2))
        async with self._lock:  # one request per location at a time
            now = self.clock()
            cached = self._cache.get(key)
            if cached is not None and cached[0] > now:
                return cached[1]
            found = await self._fetch(*key)
            ttl = self.cache_seconds if found is not None else self.failure_cache_seconds
            self._cache[key] = (now + timedelta(seconds=ttl), found)
            if len(self._cache) > 10_000:  # bounded: drop expired entries
                self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            return found

    async def _fetch(self, latitude: float, longitude: float) -> Forecast | None:
        self.requests += 1
        params: dict[str, str | int | float] = {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": HOURLY,
            "past_days": 1,
            "forecast_days": 2,
            "timeformat": "unixtime",
            "timezone": "GMT",
        }
        try:
            response = await self.http.get(
                f"{self.base_url}/v1/forecast", params=params, timeout=self.timeout_seconds
            )
            response.raise_for_status()
            return parse_forecast(response.json(), self.clock())
        # RecursionError: JSON nested too deeply for response.json().
        except (httpx.HTTPError, ValueError, KeyError, TypeError, RecursionError) as exc:
            _log.warning(
                "open-meteo forecast failed, weather unknown",
                latitude=latitude,
                longitude=longitude,
                error=repr(exc),
            )
            return None
