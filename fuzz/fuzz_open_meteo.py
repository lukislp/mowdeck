"""The Open-Meteo forecast answer (services/scheduler, rain pause).

The scheduler fetches the forecast from a third-party API and must treat any answer it cannot
use as "weather unknown": ``OpenMeteoProvider`` catches ``ValueError``, ``KeyError``,
``TypeError`` and ``RecursionError`` (JSON nested too deeply) around ``response.json()`` and
``parse_forecast``. Properties:

* decoding any body as JSON and parsing it raises nothing but those four;
* an accepted forecast keeps the fetch time and every precipitation value is a float or
  unknown.
"""

import json
import math
import sys
from datetime import UTC, datetime

import atheris

with atheris.instrument_imports():
    from scheduler.adapters.weather.open_meteo import parse_forecast

from harness import Input

FETCHED = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def _check(body: bytes) -> None:
    try:
        # What OpenMeteoProvider._fetch does: response.json(), then parse_forecast.
        forecast = parse_forecast(json.loads(body), FETCHED)
    except (ValueError, KeyError, TypeError, RecursionError):
        return
    assert forecast.fetched_at == FETCHED
    for point in forecast.points:
        for value in (point.precipitation_mm, point.probability_percent, point.rain_mm):
            assert value is None or isinstance(value, float)


def test_one_input(data: bytes) -> None:
    inp = Input(data)
    _check(inp.rest())


def seeds() -> list[bytes]:
    """An answer in the shape Open-Meteo sends (``timeformat=unixtime``)."""
    start = int(FETCHED.timestamp())
    body = {
        "latitude": 48.14,
        "longitude": 11.58,
        "hourly": {
            "time": [start + 3600 * i for i in range(4)],
            "precipitation": [0.0, 0.4, None, 1.2],
            "precipitation_probability": [5, 60, 80, 100],
            "rain": [0.0, 0.3, 0.0, 1.1],
        },
    }
    nan = math.nan
    return [
        json.dumps(body).encode(),
        json.dumps(body | {"hourly": {"time": [start], "rain": [nan]}}).encode(),
        b'{"error":true,"reason":"Cannot initialize WeatherVariable from invalid String value"}',
    ]


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
