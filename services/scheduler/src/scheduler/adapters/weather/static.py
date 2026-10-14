"""A weather provider with a fixed answer, for tests and development.

``dry`` reports no rain at all, ``rain`` 2 mm per hour at 90 % for every hour from a day ago to
two days ahead, ``unknown`` no forecast (like a provider outage). The mode can be changed at
runtime (``PUT /api/v1/scheduler/dev/weather`` when ``MOWDECK_SCHEDULER_DEV_ENDPOINTS`` is on).
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, get_args

from scheduler.domain.rain import Forecast, HourlyPoint

StaticMode = Literal["dry", "rain", "unknown"]
STATIC_MODES: tuple[str, ...] = get_args(StaticMode)


class StaticWeatherProvider:
    """``WeatherProvider`` with a fixed answer."""

    name = "static"

    def __init__(
        self, mode: StaticMode = "dry", clock: Callable[[], datetime] | None = None
    ) -> None:
        self.mode: StaticMode = mode
        self.clock = clock or (lambda: datetime.now(UTC))

    async def forecast(self, latitude: float, longitude: float) -> Forecast | None:
        """The fixed answer around now."""
        if self.mode == "unknown":
            return None
        now = self.clock()
        start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=24)
        wet = self.mode == "rain"
        points = tuple(
            HourlyPoint(
                end=start + timedelta(hours=hour),
                precipitation_mm=2.0 if wet else 0.0,
                probability_percent=90.0 if wet else 0.0,
                rain_mm=2.0 if wet else 0.0,
            )
            for hour in range(24 * 3 + 1)
        )
        return Forecast(latitude, longitude, points, "static", now)
