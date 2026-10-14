"""Fetch one Open-Meteo forecast for a real location and show the rain decision.

Not part of the test suite (it needs the internet); the tests use the recorded answer in
``services/scheduler/tests/data/open_meteo_berlin.json``. Run it by hand to check that the API
still answers the way the client expects, or to record a new answer::

    uv run python services/scheduler/scripts/open_meteo_live.py --live
    uv run python services/scheduler/scripts/open_meteo_live.py --live --lat 48.14 --lon 11.58 \\
        --record services/scheduler/tests/data/open_meteo_munich.json

Without ``--live`` it only prints this help, so it never touches the network by accident.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from scheduler.adapters.weather.open_meteo import DEFAULT_BASE_URL, HOURLY, OpenMeteoProvider
from scheduler.domain.rain import RainSettings, decide


async def fetch(latitude: float, longitude: float, record: Path | None) -> int:
    """Fetch, print the decision for now and the next hours; optionally record the raw JSON."""
    async with httpx.AsyncClient(headers={"User-Agent": "MowDeck-Scheduler"}) as http:
        if record is not None:
            response = await http.get(
                f"{DEFAULT_BASE_URL}/v1/forecast",
                params={
                    "latitude": round(latitude, 2),
                    "longitude": round(longitude, 2),
                    "hourly": HOURLY,
                    "past_days": 1,
                    "forecast_days": 2,
                    "timeformat": "unixtime",
                    "timezone": "GMT",
                },
                timeout=10,
            )
            response.raise_for_status()
            await asyncio.to_thread(record.write_text, response.text, encoding="utf-8")
            print(f"recorded {record}")
        forecast = await OpenMeteoProvider(http).forecast(latitude, longitude)
    if forecast is None:
        print("open-meteo did not answer: the rain pause would treat the weather as unknown")
        return 1
    now = datetime.now(UTC)
    decision = decide(RainSettings(), forecast, now)
    print(f"{len(forecast.points)} hours for {forecast.latitude}, {forecast.longitude}")
    print(f"decision now ({now:%Y-%m-%d %H:%M} UTC): {decision.verdict.value}: {decision.reason}")
    for point in decision.points:
        print(
            json.dumps(
                {
                    "end": point.end.isoformat(),
                    "precipitation_mm": point.precipitation_mm,
                    "probability_percent": point.probability_percent,
                }
            )
        )
    print("Weather data by Open-Meteo.com (CC BY 4.0)")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="really call api.open-meteo.com")
    parser.add_argument("--lat", type=float, default=52.52, help="latitude (default Berlin)")
    parser.add_argument("--lon", type=float, default=13.41, help="longitude (default Berlin)")
    parser.add_argument("--record", type=Path, help="also write the raw answer to this file")
    args = parser.parse_args(argv)
    if not args.live:
        parser.print_help()
        return 0
    return asyncio.run(fetch(args.lat, args.lon, args.record))


if __name__ == "__main__":
    sys.exit(main())
