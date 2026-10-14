"""The rain pause: does the forecast hold a run?

A forecast is a list of hourly points. Like Open-Meteo, a point's values cover the hour that
*ends* at its time (``precipitation`` is the sum of the preceding hour, the probability is the
chance of more than 0.1 mm in it). A run at ``at`` is held when

* an hour overlapping ``[at, at + lookahead]`` has a precipitation probability at or above the
  threshold, or precipitation at or above the millimetre threshold (it will rain), or
* the lawn is still wet (:func:`wet_lawn`): the last hour with precipitation at or above the
  millimetre threshold ended less than the drying time ago. The drying time is the cooldown,
  times 1.5 (at most 6 h) when 5 mm or more fell in the last 6 hours.

Without a forecast (provider failure) the verdict is ``unknown`` and the schedule's setting
decides whether to run or hold; without a location it is ``no_location`` and the run goes ahead.

A ``hold`` also says when it ends (:func:`pause_end`): the first time at which the same rule
would let a run go ahead, found by walking the forecast. Clients display that value; they do
not re-derive it from the hours.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class RainSettings:
    """Rain pause settings of a schedule."""

    enabled: bool = True
    probability_threshold: int = 60
    """Percent; a forecast hour at or above holds the run."""
    precipitation_mm_threshold: float = 0.5
    """Millimetres per hour; a forecast or past hour at or above holds the run."""
    lookahead_hours: int = 3
    cooldown_hours: int = 2
    run_when_unknown: bool = True
    """Without a forecast (provider down): True runs, False holds."""

    def to_json(self) -> dict[str, object]:
        """Stored form."""
        return {
            "enabled": self.enabled,
            "probability_threshold": self.probability_threshold,
            "precipitation_mm_threshold": self.precipitation_mm_threshold,
            "lookahead_hours": self.lookahead_hours,
            "cooldown_hours": self.cooldown_hours,
            "run_when_unknown": self.run_when_unknown,
        }

    @classmethod
    def from_json(cls, value: dict[str, object]) -> "RainSettings":
        """From the stored form (unknown keys ignored, missing keys default)."""
        known = {k: v for k, v in value.items() if k in cls.__dataclass_fields__}
        return cls(**known)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class HourlyPoint:
    """One forecast hour, ending at ``end`` (UTC)."""

    end: datetime
    precipitation_mm: float | None = None
    probability_percent: float | None = None
    rain_mm: float | None = None

    @property
    def start(self) -> datetime:
        """Start of the hour the values cover."""
        return self.end - timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class Forecast:
    """Hourly points of one location from one provider."""

    latitude: float
    longitude: float
    points: tuple[HourlyPoint, ...]
    source: str
    fetched_at: datetime


class Verdict(StrEnum):
    """Outcome of the rain decision."""

    RUN = "run"
    HOLD = "hold"
    UNKNOWN_RUN = "unknown_run"
    """No forecast; the schedule runs anyway (``run_when_unknown``)."""
    UNKNOWN_HOLD = "unknown_hold"
    """No forecast; the schedule holds (``run_when_unknown`` off)."""
    NO_LOCATION = "no_location"
    """Neither site coordinates nor a map origin nor a position; the run goes ahead."""
    DISABLED = "disabled"

    @property
    def holds(self) -> bool:
        """True when the run must not start."""
        return self in {Verdict.HOLD, Verdict.UNKNOWN_HOLD}


@dataclass(frozen=True, slots=True)
class RainDecision:
    """The verdict with its reason and the forecast hours that were looked at."""

    verdict: Verdict
    reason: str
    points: tuple[HourlyPoint, ...] = field(default_factory=tuple)
    at: datetime | None = None
    """The time the decision is for (hours ending at or before it are past)."""
    wet: bool = False
    """The lawn is still wet from earlier rain (needs ``cooldown_hours`` > 0)."""
    wet_until: datetime | None = None
    recent_mm_6h: float | None = None
    recent_mm_24h: float | None = None
    pause_until: datetime | None = None
    """When a ``hold`` ends (:func:`pause_end`); None when not holding or open-ended."""


PAST_HOURS_SHOWN = 12
HEAVY_RAIN_MM_6H = 5.0
HEAVY_RAIN_FACTOR = 1.5
MAX_DRYING = timedelta(hours=6)


def recent_mm(points: tuple[HourlyPoint, ...], at: datetime, hours: int) -> float:
    """Precipitation of the hours that ended within ``hours`` before ``at``."""
    since = at - timedelta(hours=hours)
    return sum((p.precipitation_mm or 0.0) for p in points if since < p.end <= at)


def wet_lawn(
    settings: RainSettings, points: tuple[HourlyPoint, ...], at: datetime
) -> tuple[bool, datetime | None]:
    """``(wet, wet_until)``: end of the last wet hour that has started plus the drying time."""
    if settings.cooldown_hours <= 0:
        return False, None
    mm = settings.precipitation_mm_threshold
    wet_hours = [p for p in points if p.start < at and (p.precipitation_mm or 0) >= mm]
    if not wet_hours:
        return False, None
    drying = timedelta(hours=settings.cooldown_hours)
    if recent_mm(points, at, 6) >= HEAVY_RAIN_MM_6H:
        drying = min(drying * HEAVY_RAIN_FACTOR, max(MAX_DRYING, drying))
    until = max(p.end for p in wet_hours) + drying
    return until > at, until


def _overlaps(point: HourlyPoint, start: datetime, end: datetime) -> bool:
    return point.start < end and point.end > start


def _rainy(settings: RainSettings, point: HourlyPoint) -> bool:
    """The hour reaches the probability or the millimetre threshold."""
    return (point.probability_percent or 0) >= settings.probability_threshold or (
        point.precipitation_mm or 0
    ) >= settings.precipitation_mm_threshold


def pause_end(
    settings: RainSettings, points: tuple[HourlyPoint, ...], at: datetime
) -> datetime | None:
    """When a hold at ``at`` ends: the first time at or after ``at`` at which the rule lets a
    run go ahead. From ``at`` the walk jumps to the end of the last rainy hour in the lookahead,
    then past the drying time, and repeats until neither holds. None when the forecast runs out
    first (the pause is open-ended as far as the forecast knows)."""
    lookahead = timedelta(hours=settings.lookahead_hours)
    now = at
    for _ in range(2 * len(points) + 2):
        ahead = [p for p in points if _overlaps(p, now, now + lookahead)]
        if not ahead:
            return None
        rainy = [p.end for p in ahead if _rainy(settings, p)]
        if rainy:
            now = max(rainy)
            continue
        wet, until = wet_lawn(settings, points, now)
        if wet and until is not None:
            now = until
            continue
        return now
    return None


def decide(settings: RainSettings, forecast: Forecast | None, at: datetime) -> RainDecision:
    """Hold or run at ``at`` (see the module docstring)."""
    if not settings.enabled:
        return RainDecision(Verdict.DISABLED, "rain pause is off")
    if forecast is None:
        if settings.run_when_unknown:
            return RainDecision(Verdict.UNKNOWN_RUN, "no forecast available, running anyway")
        return RainDecision(Verdict.UNKNOWN_HOLD, "no forecast available, holding")
    ahead_end = at + timedelta(hours=settings.lookahead_hours)
    shown_start = at - timedelta(hours=max(settings.cooldown_hours, PAST_HOURS_SHOWN))
    ahead = tuple(p for p in forecast.points if _overlaps(p, at, ahead_end))
    behind = tuple(p for p in forecast.points if _overlaps(p, shown_start, at))
    used = tuple(sorted({*behind, *ahead}, key=lambda p: p.end))
    wet, wet_until = wet_lawn(settings, forecast.points, at)
    recent_6h = recent_mm(forecast.points, at, 6)
    recent_24h = recent_mm(forecast.points, at, 24)

    def out(verdict: Verdict, reason: str) -> RainDecision:
        until = pause_end(settings, forecast.points, at) if verdict is Verdict.HOLD else None
        return RainDecision(
            verdict, reason, used, at, wet, wet_until, recent_6h, recent_24h, pause_until=until
        )

    mm = settings.precipitation_mm_threshold
    for point in ahead:
        if (point.probability_percent or 0) >= settings.probability_threshold:
            return out(
                Verdict.HOLD,
                f"rain probability {point.probability_percent:.0f} % at "
                f"{point.start:%H:%M} UTC (threshold {settings.probability_threshold} %)",
            )
        if (point.precipitation_mm or 0) >= mm:
            return out(
                Verdict.HOLD,
                f"{point.precipitation_mm:.1f} mm expected at {point.start:%H:%M} UTC "
                f"(threshold {mm:g} mm)",
            )
    if wet and wet_until is not None:
        return out(
            Verdict.HOLD,
            f"{recent_6h:.1f} mm fell in the last 6 h, cooling down until {wet_until:%H:%M} UTC",
        )
    if not ahead:
        if settings.run_when_unknown:
            return out(Verdict.UNKNOWN_RUN, "the forecast does not cover the run")
        return out(Verdict.UNKNOWN_HOLD, "the forecast does not cover the run")
    return out(Verdict.RUN, "dry")
