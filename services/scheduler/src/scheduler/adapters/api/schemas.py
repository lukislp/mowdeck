"""Request and response bodies of the Scheduler API (``/api/v1/scheduler``)."""

from datetime import date, datetime, time
from typing import Any, ClassVar, Literal, Self
from uuid import UUID

from mowdeck_contracts.v1 import PrincipalRef, SchedulePattern, ScheduleRunOutcome
from pydantic import BaseModel, ConfigDict, Field, model_validator

from scheduler.application.angles import MowerAngles
from scheduler.application.schedules import Preview, WidgetOverview, WidgetRun
from scheduler.application.weather import WeatherAnswer
from scheduler.domain.models import Schedule, ScheduleRun
from scheduler.domain.rain import HourlyPoint, RainSettings
from scheduler.domain.rotation import STEP_RANGE
from scheduler.domain.rules import WEEKDAYS

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class In(BaseModel):
    """Base of request bodies: unknown fields are refused (no smuggled ``account_id``)."""

    model_config = ConfigDict(extra="forbid")


class SchedulePatternIn(SchedulePattern):
    """The start settings of a schedule (``SchedulePattern``): every key optional, null or left
    out falls back to the mower's mow defaults, then to the mower's own setting. Unknown keys,
    including ``start_progress_pct``, are refused."""

    model_config = ConfigDict(**{**SchedulePattern.model_config, "extra": "forbid"})


BladeHeight = Field(
    default=None,
    ge=10,
    le=120,
    description="Blade height in millimetres sent before each start (`set_blade_height`); "
    "null keeps the mower's own. Must lie within every target mower's "
    "`blade_height_range_mm`.",
)
RotationStep = Field(
    default=None,
    ge=STEP_RANGE[0],
    le=STEP_RANGE[1],
    description="Step rotation (ADR 0061): each run turns the last angle the mower mowed its "
    "areas with by this many degrees (modulo 180); null: off. Excludes `rotation_angles`.",
)
Speed = Field(
    default=None,
    gt=0,
    le=2,
    description="Mowing speed in m/s sent before each start (`set_speed`); null keeps the "
    "mower's own. Must lie within every target mower's speed range.",
)


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
    pattern: SchedulePatternIn | None = Field(
        default=None,
        description="Start settings of every run (`SchedulePattern`), laid over the mower's "
        "mow defaults. Settings beyond the angle and the border laps must be offered by every "
        "mower of the target (422 otherwise).",
    )
    blade_height_mm: int | None = BladeHeight
    speed_mps: float | None = Speed
    rotation_angles: list[float] = Field(
        default_factory=list,
        max_length=24,
        description="Angles used round robin, one per run (replace `pattern.angle_deg`).",
    )
    rotation_step_deg: float | None = RotationStep
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
    pattern: SchedulePatternIn | None = Field(default=None, description="null: none")
    blade_height_mm: int | None = BladeHeight
    speed_mps: float | None = Speed
    rotation_angles: list[float] | None = Field(default=None, max_length=24)
    rotation_step_deg: float | None = RotationStep
    rain_pause: RainPause | None = None
    pause_running_jobs_on_rain: bool | None = None
    return_to_dock_at_window_end: bool | None = None
    enabled: bool | None = None

    _NULLABLE: ClassVar[frozenset[str]] = frozenset(
        {
            "anchor_date",
            "interval_days",
            "timezone",
            "pattern",
            "blade_height_mm",
            "speed_mps",
            "rotation_step_deg",
        }
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
                case "pattern":
                    result[name] = value.keys() if value is not None else None
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
    pattern: SchedulePattern | None = Field(
        description="The start settings the schedule sets; keys left out come from the "
        "mower's mow defaults, then the mower's own setting."
    )
    blade_height_mm: int | None
    speed_mps: float | None
    rotation_angles: list[float]
    rotation_index: int
    rotation_step_deg: float | None = Field(
        description="Step rotation (ADR 0061): degrees each run turns the mower's last angle "
        "on its areas by; null: off.",
    )
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
            pattern=SchedulePattern.model_validate(item.pattern) if item.pattern else None,
            blade_height_mm=item.blade_height_mm,
            speed_mps=item.speed_mps,
            rotation_angles=item.rotation_angles,
            rotation_index=item.rotation_index,
            rotation_step_deg=item.rotation_step_deg,
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
    angle_deg: float | None = Field(
        default=None,
        description="For a started run: the mowing angle the start sent (0 up to below 180 "
        "degrees); null for a pattern without direction and for runs that did not start.",
    )

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
            angle_deg=run.angle_deg,
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
    angle_deg: float | None = Field(
        default=None,
        description="The mowing angle this firing will use on `angle_mower_id` (rotation "
        "applied, assuming every earlier firing starts); null when skipped, for a pattern "
        "without direction or without a target mower.",
    )


class PreviewOut(BaseModel):
    """``GET /schedules/{id}/preview``."""

    items: list[PreviewItemOut]
    weather: RainDecisionOut | None = Field(
        description="The rain decision for the first firing that is not skipped, with today's "
        "forecast (null without a target mower)."
    )
    weather_mower_id: UUID | None
    angle_mower_id: UUID | None = Field(
        default=None,
        description="The mower the angles are worked out for (a group: its first mower; each "
        "mower continues from its own last angle).",
    )

    @classmethod
    def of(cls, preview: Preview) -> "PreviewOut":
        """From the use case answer."""
        return cls(
            items=[
                PreviewItemOut(
                    at=i.at, window_end=i.window_end, skipped=i.skipped, angle_deg=i.angle_deg
                )
                for i in preview.items
            ],
            weather=RainDecisionOut.of(preview.weather) if preview.weather else None,
            weather_mower_id=preview.weather_mower_id,
            angle_mower_id=preview.angle_mower_id,
        )


class DevWeatherIn(In):
    """``PUT /dev/weather``: the answer of the static weather provider."""

    mode: Literal["dry", "rain", "unknown"]


class WidgetRunOut(BaseModel):
    """One upcoming run in the widget overview."""

    schedule_id: UUID
    name: str
    starts_at: datetime
    duration_minutes: int


class WidgetRain(BaseModel):
    """Rain facts for the widgets (everything null when no forecast is available)."""

    raining_now: bool | None = Field(
        description="The forecast hour that contains `at` has measurable precipitation."
    )
    expected_mm_next_hours: float | None = Field(
        description="Precipitation of the coming hours the rain pause looks at (its look-ahead)."
    )
    wet: bool = Field(description="The lawn is still wet from earlier rain.")
    wet_until: datetime | None
    pause_until: datetime | None = Field(description="When a rain pause ends, if one holds.")
    recent_mm_6h: float | None


class WidgetScheduleOut(BaseModel):
    """``GET /mowers/{mower_id}/widget``: what the schedule and weather widgets of the iOS app
    show for one mower (ADR 0059)."""

    mower_id: UUID
    at: datetime
    runs: list[WidgetRunOut] = Field(description="The next runs, soonest first (at most 3).")
    verdict: Literal["runs", "rain_pause", "unsure"] = Field(
        description="`runs`: the next run would start; `rain_pause`: the rain pause holds it; "
        "`unsure`: no forecast or no location, or the forecast is unknown."
    )
    rain: WidgetRain

    @classmethod
    def of(cls, mower_id: UUID, at: datetime, overview: WidgetOverview) -> "WidgetScheduleOut":
        """From the use case answer."""
        runs = [WidgetRunOut(**vars_of(r)) for r in overview.runs]
        answer = overview.answer
        if answer is None:
            return cls(
                mower_id=mower_id,
                at=at,
                runs=runs,
                verdict="unsure",
                rain=WidgetRain(
                    raining_now=None,
                    expected_mm_next_hours=None,
                    wet=False,
                    wet_until=None,
                    pause_until=None,
                    recent_mm_6h=None,
                ),
            )
        decision = answer.decision
        verdict: Literal["runs", "rain_pause", "unsure"]
        match decision.verdict.value:
            case "run":
                verdict = "runs"
            case "hold":
                verdict = "rain_pause"
            case _:
                verdict = "unsure"
        upcoming = [p for p in decision.points if decision.at is None or p.end > decision.at]
        current = next((p for p in upcoming), None)
        window = upcoming[: max(answer.settings.lookahead_hours, 1)]
        known = [p.precipitation_mm for p in window if p.precipitation_mm is not None]
        return cls(
            mower_id=mower_id,
            at=at,
            runs=runs,
            verdict=verdict,
            rain=WidgetRain(
                raining_now=(
                    None
                    if current is None or current.precipitation_mm is None
                    else current.precipitation_mm >= RAINING_MM
                ),
                expected_mm_next_hours=round(sum(known), 2) if known else None,
                wet=decision.wet,
                wet_until=decision.wet_until,
                pause_until=decision.pause_until,
                recent_mm_6h=decision.recent_mm_6h,
            ),
        )


RAINING_MM = 0.1
"""Precipitation per hour from which the widget says it is raining."""


def vars_of(run: WidgetRun) -> dict[str, Any]:
    """The fields of a :class:`WidgetRun` as keyword arguments."""
    return {
        "schedule_id": run.schedule_id,
        "name": run.name,
        "starts_at": run.starts_at,
        "duration_minutes": run.duration_minutes,
    }


class AreaAngleOut(BaseModel):
    """The last angle a job mowed one area with."""

    area_id: str
    angle_deg: float = Field(description="0 up to below 180 degrees.")
    used_at: datetime
    source: Literal["schedule", "manual"] = Field(
        description="`schedule`: a schedule's run; `manual`: a start from the start sheet with "
        "rotation chosen."
    )
    schedule_id: UUID | None


class MowerAnglesOut(BaseModel):
    """``GET /mowers/{mower_id}/angles``: where the angle rotation of a mower stands
    (ADR 0061)."""

    mower_id: UUID
    manual_step_deg: float | None = Field(
        description="The rotation step the start sheet offers for manual starts; null: off."
    )
    areas: list[AreaAngleOut] = Field(description="The last angle per area, by area id.")

    @classmethod
    def of(cls, angles: MowerAngles) -> "MowerAnglesOut":
        """From the use case answer."""
        return cls(
            mower_id=angles.mower_id,
            manual_step_deg=angles.manual_step_deg,
            areas=[
                AreaAngleOut(
                    area_id=a.area_id,
                    angle_deg=a.angle_deg,
                    used_at=a.used_at,
                    source="manual" if a.source == "manual" else "schedule",
                    schedule_id=a.schedule_id,
                )
                for a in angles.areas
            ],
        )


class RotationSettingsIn(In):
    """``PUT /mowers/{mower_id}/angles/settings``: the step of manual starts."""

    manual_step_deg: float | None = Field(
        ge=STEP_RANGE[0],
        le=STEP_RANGE[1],
        description="Degrees the start sheet turns the last angle by; null: no rotation.",
    )


class ManualAngleIn(In):
    """``POST /mowers/{mower_id}/angles``: a manual start chose rotation and sent this angle."""

    area_ids: list[str] = Field(min_length=1, max_length=50)
    angle_deg: float = Field(ge=0, le=180, description="Stored modulo 180.")
