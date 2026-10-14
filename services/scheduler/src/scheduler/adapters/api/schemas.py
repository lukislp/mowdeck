"""Request and response bodies of the Scheduler API (``/api/v1/scheduler``)."""

from datetime import date, datetime, time
from typing import Any, ClassVar, Literal, Self
from uuid import UUID

from mowdeck_contracts.v1 import PrincipalRef, ScheduleRunOutcome
from pydantic import BaseModel, ConfigDict, Field, model_validator

from scheduler.application.schedules import Preview
from scheduler.application.weather import WeatherAnswer
from scheduler.domain.models import Schedule, ScheduleRun
from scheduler.domain.rain import HourlyPoint, RainSettings
from scheduler.domain.rules import WEEKDAYS

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class In(BaseModel):
    """Base of request bodies: unknown fields are refused (no smuggled ``account_id``)."""

    model_config = ConfigDict(extra="forbid")


class RainPause(In):
    """Rain pause settings of a schedule."""

    enabled: bool = True
    probability_threshold: int = Field(
        default=60, ge=0, le=100, description="Percent; a forecast hour at or above holds the run."
    )
    precipitation_mm_threshold: float = Field(
        default=0.5, ge=0, le=100, description="Millimetres per hour; at or above holds the run."
    )
    lookahead_hours: int = Field(default=3, ge=0, le=24)
    cooldown_hours: int = Field(
        default=2, ge=0, le=24, description="Hold while rain fell within this many hours."
    )
    run_when_unknown: bool = Field(
        default=True, description="Without a forecast (provider down): run (true) or hold."
    )

    def settings(self) -> RainSettings:
        """The domain value."""
        return RainSettings(**self.model_dump())

    @classmethod
    def of(cls, value: RainSettings) -> "RainPause":
        """From the domain value."""
        return cls.model_validate(value.to_json())


class ScheduleIn(In):
    """``POST /schedules``.

    ``kind=weekly`` needs ``days_of_week``; ``kind=every_n_days`` needs ``anchor_date`` (the first
    day it fires) and ``interval_days``. ``start_time`` is wall-clock time in the schedule's time
    zone (the target's site, or ``timezone``). ``area_ids`` empty means every area of the
    mower's map; a group target always mows every area of each mower.
    """

    name: str = Field(min_length=1, max_length=100)
    target_type: Literal["mower", "group"]
    target_id: UUID
    kind: Literal["weekly", "every_n_days"]
    days_of_week: list[Weekday] = Field(default_factory=list, max_length=7)
    anchor_date: date | None = None
    interval_days: int | None = Field(default=None, ge=1, le=365)
    start_time: time
    duration_minutes: int = Field(
        default=120, ge=1, le=1440, description="The window: no start after it closed."
    )
    timezone: str | None = Field(
        default=None, max_length=64, description="IANA name overriding the site's time zone."
    )
    area_ids: list[str] = Field(default_factory=list, max_length=50)
    pattern: dict[str, Any] | None = Field(
        default=None, description="Native pattern settings passed to `StartMow.pattern`."
    )
    rotation_angles: list[float] = Field(
        default_factory=list,
        max_length=24,
        description="Angles used round robin, one per run (sets `pattern.angle`).",
    )
    rain_pause: RainPause = Field(default_factory=RainPause)
    pause_running_jobs_on_rain: bool = Field(
        default=True, description="Send a running scheduled job back to the dock when it rains."
    )
    return_to_dock_at_window_end: bool = False
    enabled: bool = True

    @model_validator(mode="after")
    def _recurrence(self) -> Self:
        if self.kind == "weekly" and not self.days_of_week:
            msg = "a weekly schedule needs days_of_week"
            raise ValueError(msg)
        if self.kind == "every_n_days" and (self.anchor_date is None or not self.interval_days):
            msg = "an every_n_days schedule needs anchor_date and interval_days"
            raise ValueError(msg)
        return self


class SchedulePatch(In):
    """``PATCH /schedules/{id}``: only the given fields change; the caller becomes the owner."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    target_type: Literal["mower", "group"] | None = None
    target_id: UUID | None = None
    kind: Literal["weekly", "every_n_days"] | None = None
    days_of_week: list[Weekday] | None = Field(default=None, max_length=7)
    anchor_date: date | None = None
    interval_days: int | None = Field(default=None, ge=1, le=365)
    start_time: time | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    timezone: str | None = Field(default=None, max_length=64, description="null: the site's")
    area_ids: list[str] | None = Field(default=None, max_length=50)
    pattern: dict[str, Any] | None = None
    rotation_angles: list[float] | None = Field(default=None, max_length=24)
    rain_pause: RainPause | None = None
    pause_running_jobs_on_rain: bool | None = None
    return_to_dock_at_window_end: bool | None = None
    enabled: bool | None = None

    _NULLABLE: ClassVar[frozenset[str]] = frozenset(
        {"anchor_date", "interval_days", "timezone", "pattern"}
    )

    def changes(self) -> dict[str, Any]:
        """The domain changes of the given fields (``null`` only where it means "none")."""
        result: dict[str, Any] = {}
        for name in self.model_fields_set:
            value = getattr(self, name)
            if value is None and name not in self._NULLABLE:
                continue
            match name:
                case "days_of_week":
                    result[name] = [WEEKDAYS.index(d) for d in value]
                case "timezone":
                    result["timezone_override"] = value
                case "rain_pause":
                    result["rain"] = value.settings().to_json()
                case _:
                    result[name] = value
        return result


class ScheduleOut(BaseModel):
    """A schedule."""

    id: UUID
    name: str
    target_type: str
    target_id: UUID
    kind: str
    days_of_week: list[str]
    anchor_date: date | None
    interval_days: int | None
    start_time: time
    duration_minutes: int
    timezone: str = Field(description="The effective time zone (override or the site's).")
    timezone_override: str | None
    area_ids: list[str]
    pattern: dict[str, Any] | None
    rotation_angles: list[float]
    rotation_index: int
    rain_pause: RainPause
    pause_running_jobs_on_rain: bool
    return_to_dock_at_window_end: bool
    enabled: bool
    disabled_reason: str | None
    permission_skips: int
    skip_until: datetime | None
    last_run_at: datetime | None
    next_run_at: datetime | None
    owner: PrincipalRef = Field(description="The subject the schedule runs on behalf of.")
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, item: Schedule) -> "ScheduleOut":
        """From the entity."""
        return cls(
            id=item.id,
            name=item.name,
            target_type=item.target_type,
            target_id=item.target_id,
            kind=item.kind,
            days_of_week=[WEEKDAYS[d] for d in item.days_of_week],
            anchor_date=item.anchor_date,
            interval_days=item.interval_days,
            start_time=item.start_time,
            duration_minutes=item.duration_minutes,
            timezone=item.timezone,
            timezone_override=item.timezone_override,
            area_ids=item.area_ids,
            pattern=item.pattern,
            rotation_angles=item.rotation_angles,
            rotation_index=item.rotation_index,
            rain_pause=RainPause.of(item.rain_settings),
            pause_running_jobs_on_rain=item.pause_running_jobs_on_rain,
            return_to_dock_at_window_end=item.return_to_dock_at_window_end,
            enabled=item.enabled,
            disabled_reason=item.disabled_reason,
            permission_skips=item.permission_skips,
            skip_until=item.skip_until,
            last_run_at=item.last_run_at,
            next_run_at=item.next_run_at,
            owner=PrincipalRef.model_validate(item.owner),
            created_at=item.created_at,
            updated_at=item.updated_at,
        )


class ScheduleList(BaseModel):
    """``GET /schedules``."""

    items: list[ScheduleOut]


class RunOut(BaseModel):
    """One run of a schedule for one mower."""

    id: UUID
    schedule_id: UUID
    mower_id: UUID | None
    trigger: Literal["schedule", "manual"]
    outcome: ScheduleRunOutcome
    details: str | None
    command_id: UUID | None
    scheduled_for: datetime
    started_at: datetime
    window_end_at: datetime | None
    followup: str | None = Field(
        description="After a start: `rain_dock`, `window_dock`, `finished`, `closed` or null "
        "while the job is followed."
    )
    followup_at: datetime | None

    @classmethod
    def of(cls, run: ScheduleRun) -> "RunOut":
        """From the entity."""
        return cls(
            id=run.id,
            schedule_id=run.schedule_id,
            mower_id=run.mower_id,
            trigger="manual" if run.trigger == "manual" else "schedule",
            outcome=ScheduleRunOutcome(run.outcome),
            details=run.details,
            command_id=run.command_id,
            scheduled_for=run.scheduled_for,
            started_at=run.started_at,
            window_end_at=run.window_end_at,
            followup=run.followup,
            followup_at=run.followup_at,
        )


class RunList(BaseModel):
    """``GET /schedules/{id}/runs`` (newest first) and ``POST .../run-now``."""

    items: list[RunOut]
    next_before: datetime | None = None


class HourOut(BaseModel):
    """One forecast hour (values cover the hour ending at ``end``)."""

    start: datetime
    end: datetime
    precipitation_mm: float | None
    probability_percent: float | None
    rain_mm: float | None
    past: bool = Field(default=False, description="The hour ended before the decision time.")

    @classmethod
    def of(cls, point: HourlyPoint, at: datetime | None = None) -> "HourOut":
        """From the domain value; ``at`` is the decision time (past hours ended at or before)."""
        return cls(
            past=at is not None and point.end <= at,
            start=point.start,
            end=point.end,
            precipitation_mm=point.precipitation_mm,
            probability_percent=point.probability_percent,
            rain_mm=point.rain_mm,
        )


class RainDecisionOut(BaseModel):
    """A rain decision with what it was based on."""

    verdict: Literal["run", "hold", "unknown_run", "unknown_hold", "no_location", "disabled"]
    holds: bool
    reason: str
    provider: str
    location_source: Literal["site", "map", "position"] | None
    latitude: float | None
    longitude: float | None
    hours: list[HourOut] = Field(description="The forecast hours the decision looked at.")
    attribution: str | None = None
    wet: bool = Field(default=False, description="The lawn is still wet from earlier rain.")
    wet_until: datetime | None = Field(default=None, description="Estimated dry time.")
    recent_mm_6h: float | None = None
    recent_mm_24h: float | None = None
    pause_until: datetime | None = Field(
        default=None,
        description="When a `hold` ends, by the Scheduler's rule; null when not holding or when "
        "the forecast does not reach that time.",
    )
    settings: RainPause = Field(
        description="The rain pause settings the decision applied (the schedule's, or the "
        "query parameters and defaults)."
    )

    @classmethod
    def of(cls, answer: WeatherAnswer) -> "RainDecisionOut":
        """From the use case answer."""
        location = answer.location
        return cls(
            pause_until=answer.decision.pause_until,
            settings=RainPause.of(answer.settings),
            verdict=answer.decision.verdict.value,
            holds=answer.decision.verdict.holds,
            reason=answer.decision.reason,
            provider=answer.provider,
            location_source=location.source if location else None,
            latitude=location.latitude if location else None,
            longitude=location.longitude if location else None,
            hours=[HourOut.of(p, answer.decision.at) for p in answer.decision.points],
            wet=answer.decision.wet,
            wet_until=answer.decision.wet_until,
            recent_mm_6h=answer.decision.recent_mm_6h,
            recent_mm_24h=answer.decision.recent_mm_24h,
            attribution=(
                "Weather data by Open-Meteo.com (CC BY 4.0)"
                if answer.provider == "open_meteo"
                else None
            ),
        )


class WeatherOut(BaseModel):
    """``GET /weather/{mower_id}``: the rain decision now, with the default settings unless
    query parameters change them."""

    mower_id: UUID
    at: datetime
    decision: RainDecisionOut


class PreviewItemOut(BaseModel):
    """One upcoming firing."""

    at: datetime
    window_end: datetime
    skipped: bool = Field(description="Skipped by `skip-next`.")


class PreviewOut(BaseModel):
    """``GET /schedules/{id}/preview``."""

    items: list[PreviewItemOut]
    weather: RainDecisionOut | None = Field(
        description="The rain decision for the first firing that is not skipped, with today's "
        "forecast (null without a target mower)."
    )
    weather_mower_id: UUID | None

    @classmethod
    def of(cls, preview: Preview) -> "PreviewOut":
        """From the use case answer."""
        return cls(
            items=[
                PreviewItemOut(at=i.at, window_end=i.window_end, skipped=i.skipped)
                for i in preview.items
            ],
            weather=RainDecisionOut.of(preview.weather) if preview.weather else None,
            weather_mower_id=preview.weather_mower_id,
        )


class DevWeatherIn(In):
    """``PUT /dev/weather``: the answer of the static weather provider."""

    mode: Literal["dry", "rain", "unknown"]
