"""The rain pause: does the forecast hold a run?

A forecast is a list of hourly points. Like Open-Meteo, a point's values cover the hour that
*ends* at its time (``precipitation`` is the sum of the preceding hour, the probability is the
chance of more than 0.1 mm in it). A run at ``at`` is held when

* an hour overlapping ``[at, at + lookahead]`` has a precipitation probability at or above the
  threshold, or precipitation at or above the millimetre threshold (it will rain), or
* an hour overlapping ``[at - cooldown, at]`` had precipitation at or above the millimetre
  threshold (the grass is still wet).

Without a forecast (provider failure) the verdict is ``unknown`` and the schedule's setting
decides whether to run or hold; without a location it is ``no_location`` and the run goes ahead.
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


def _overlaps(point: HourlyPoint, start: datetime, end: datetime) -> bool:
    return point.start < end and point.end > start


def decide(settings: RainSettings, forecast: Forecast | None, at: datetime) -> RainDecision:
    """Hold or run at ``at`` (see the module docstring)."""
    if not settings.enabled:
        return RainDecision(Verdict.DISABLED, "rain pause is off")
    if forecast is None:
        if settings.run_when_unknown:
            return RainDecision(Verdict.UNKNOWN_RUN, "no forecast available, running anyway")
        return RainDecision(Verdict.UNKNOWN_HOLD, "no forecast available, holding")
    ahead_end = at + timedelta(hours=settings.lookahead_hours)
    behind_start = at - timedelta(hours=settings.cooldown_hours)
    ahead = tuple(p for p in forecast.points if _overlaps(p, at, ahead_end))
    behind = tuple(
        p for p in forecast.points if settings.cooldown_hours > 0 and _overlaps(p, behind_start, at)
    )
    used = tuple(sorted({*behind, *ahead}, key=lambda p: p.end))
    mm = settings.precipitation_mm_threshold
    for point in ahead:
        if (point.probability_percent or 0) >= settings.probability_threshold:
            return RainDecision(
                Verdict.HOLD,
                f"rain probability {point.probability_percent:.0f} % at "
                f"{point.start:%H:%M} UTC (threshold {settings.probability_threshold} %)",
                used,
            )
        if (point.precipitation_mm or 0) >= mm:
            return RainDecision(
                Verdict.HOLD,
                f"{point.precipitation_mm:.1f} mm expected at {point.start:%H:%M} UTC "
                f"(threshold {mm:g} mm)",
                used,
            )
    for point in behind:
        if (point.precipitation_mm or 0) >= mm:
            return RainDecision(
                Verdict.HOLD,
                f"{point.precipitation_mm:.1f} mm fell at {point.start:%H:%M} UTC, cooling down "
                f"for {settings.cooldown_hours} h",
                used,
            )
    if not ahead:
        if settings.run_when_unknown:
            return RainDecision(Verdict.UNKNOWN_RUN, "the forecast does not cover the run", used)
        return RainDecision(Verdict.UNKNOWN_HOLD, "the forecast does not cover the run", used)
    return RainDecision(Verdict.RUN, "dry", used)
