"""Rain decision (thresholds, cooldown, unknown) and the weather providers.

The Open-Meteo client is tested against a recorded answer (``tests/data/open_meteo_berlin.json``,
Berlin, fetched on 2026-10-09); nothing here touches the network. To fetch once for a real
location run ``uv run python services/scheduler/scripts/open_meteo_live.py --live``.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from scheduler.adapters.weather.open_meteo import OpenMeteoProvider, parse_forecast
from scheduler.adapters.weather.static import StaticWeatherProvider
from scheduler.domain.rain import Forecast, HourlyPoint, RainSettings, Verdict, decide

DATA = Path(__file__).parent / "data" / "open_meteo_berlin.json"
AT = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)


def hours(*values: tuple[int, float, float]) -> Forecast:
    """Points ending ``offset`` hours after ``AT`` with mm and probability."""
    points = tuple(
        HourlyPoint(end=AT + timedelta(hours=h), precipitation_mm=mm, probability_percent=p)
        for h, mm, p in values
    )
    return Forecast(52.5, 13.4, points, "test", AT)


DRY = [(h, 0.0, 10.0) for h in range(-3, 6)]


def test_scheduler_rain_dry_forecast_runs() -> None:
    decision = decide(RainSettings(), hours(*DRY), AT)
    assert decision.verdict == Verdict.RUN and not decision.verdict.holds
    # cooldown 2 h and lookahead 3 h: the hours ending -1 .. +3 (and the one ending at AT)
    assert [int((p.end - AT).total_seconds() // 3600) for p in decision.points] == [
        -1,
        0,
        1,
        2,
        3,
    ]


def test_scheduler_rain_probability_threshold_holds() -> None:
    forecast = hours(*DRY[:5], (2, 0.0, 60.0), *DRY[6:])
    held = decide(RainSettings(probability_threshold=60), forecast, AT)
    assert held.verdict == Verdict.HOLD and "probability 60 %" in held.reason
    assert decide(RainSettings(probability_threshold=61), forecast, AT).verdict == Verdict.RUN


def test_scheduler_rain_millimetre_threshold_holds() -> None:
    forecast = hours(*DRY[:6], (3, 0.6, 20.0), *DRY[7:])
    assert decide(RainSettings(precipitation_mm_threshold=0.5), forecast, AT).verdict.holds
    assert decide(RainSettings(precipitation_mm_threshold=1.0), forecast, AT).verdict == (
        Verdict.RUN
    )
    beyond = decide(RainSettings(lookahead_hours=1), forecast, AT)
    assert beyond.verdict == Verdict.RUN  # the wet hour is outside the lookahead


def test_scheduler_rain_cooldown_after_rain_holds() -> None:
    forecast = hours((-2, 0.0, 0.0), (-1, 0.0, 0.0), (0, 1.2, 100.0), *DRY[4:])
    held = decide(RainSettings(cooldown_hours=2), forecast, AT)
    assert held.verdict == Verdict.HOLD and "cooling down" in held.reason
    assert decide(RainSettings(cooldown_hours=0), forecast, AT).verdict == Verdict.RUN


def test_scheduler_rain_unknown_disabled_and_uncovered() -> None:
    assert decide(RainSettings(), None, AT).verdict == Verdict.UNKNOWN_RUN
    held = decide(RainSettings(run_when_unknown=False), None, AT)
    assert held.verdict == Verdict.UNKNOWN_HOLD and held.verdict.holds
    assert decide(RainSettings(enabled=False), None, AT).verdict == Verdict.DISABLED
    old = hours((-30, 0.0, 0.0))
    assert decide(RainSettings(), old, AT).verdict == Verdict.UNKNOWN_RUN
    assert decide(RainSettings(run_when_unknown=False), old, AT).verdict.holds


def test_scheduler_rain_settings_round_trip() -> None:
    settings = RainSettings(probability_threshold=40, cooldown_hours=0, run_when_unknown=False)
    assert RainSettings.from_json({**settings.to_json(), "unknown": 1}) == settings
    assert RainSettings.from_json({}) == RainSettings()


def recorded() -> dict[str, Any]:
    body: dict[str, Any] = json.loads(DATA.read_text(encoding="utf-8"))
    return body


def test_scheduler_open_meteo_parses_the_recorded_answer() -> None:
    forecast = parse_forecast(recorded(), AT)
    assert len(forecast.points) == 72
    assert forecast.points[0].end == datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    wettest = max(forecast.points, key=lambda p: p.precipitation_mm or 0)
    assert wettest.precipitation_mm == 1.7 and wettest.probability_percent == 100
    assert wettest.end == datetime(2026, 10, 8, 15, 0, tzinfo=UTC)
    settings = RainSettings()
    assert decide(settings, forecast, AT).verdict == Verdict.RUN  # a dry Friday morning
    afternoon = decide(settings, forecast, datetime(2026, 10, 9, 15, 30, tzinfo=UTC))
    assert afternoon.verdict == Verdict.HOLD and "0.7 mm" in afternoon.reason
    after_rain = decide(settings, forecast, datetime(2026, 10, 8, 17, 0, tzinfo=UTC))
    assert after_rain.verdict == Verdict.HOLD and "cooling down" in after_rain.reason


async def test_scheduler_open_meteo_caches_per_location_and_survives_failures() -> None:
    calls: list[httpx.Request] = []
    status = {"code": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if status["code"] != 200:
            return httpx.Response(status["code"])
        return httpx.Response(200, json=recorded())

    now = {"t": AT}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = OpenMeteoProvider(
            http, base_url="https://weather.example.test/", clock=lambda: now["t"]
        )
        first = await provider.forecast(52.5201, 13.4049)
        again = await provider.forecast(52.5199, 13.4041)  # same 0.01 degree cell
        assert first is not None and again is first and len(calls) == 1
        params = calls[0].url.params
        assert calls[0].url.host == "weather.example.test"
        assert params["hourly"] == "precipitation,precipitation_probability,rain"
        assert params["timeformat"] == "unixtime" and params["past_days"] == "1"
        assert params["latitude"] == "52.52"
        now["t"] += timedelta(minutes=31)  # the cache expired
        status["code"] = 500
        assert await provider.forecast(52.52, 13.405) is None
        assert await provider.forecast(52.52, 13.405) is None  # failure cached, not hammered
        assert len(calls) == 2
        now["t"] += timedelta(minutes=2)
        status["code"] = 200
        assert await provider.forecast(52.52, 13.405) is not None
        assert provider.requests == 3


async def test_scheduler_open_meteo_unexpected_json_is_unknown_weather() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": True, "reason": "bad"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await OpenMeteoProvider(http).forecast(1.0, 2.0) is None

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
        assert await OpenMeteoProvider(http).forecast(1.0, 2.0) is None
    assert (
        parse_forecast(
            {"latitude": 1, "longitude": 2, "hourly": {"time": [0], "precipitation": [None]}}, AT
        )
        .points[0]
        .precipitation_mm
        is None
    )


async def test_scheduler_static_provider_modes() -> None:
    provider = StaticWeatherProvider("dry", clock=lambda: AT)
    dry = await provider.forecast(1, 2)
    assert dry is not None and decide(RainSettings(), dry, AT).verdict == Verdict.RUN
    provider.mode = "rain"
    wet = await provider.forecast(1, 2)
    assert wet is not None and decide(RainSettings(), wet, AT).verdict == Verdict.HOLD
    provider.mode = "unknown"
    assert await provider.forecast(1, 2) is None
    assert StaticWeatherProvider().name == "static"
