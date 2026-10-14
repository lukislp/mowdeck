"""Entities of the Scheduler. Field names equal the column names in ``adapters.db.tables``."""

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Literal
from uuid import UUID

from scheduler.domain.rain import RainSettings
from scheduler.domain.recurrence import Recurrence, RecurrenceKind

TargetType = Literal["mower", "group"]
Trigger = Literal["schedule", "manual"]


@dataclass(slots=True)
class Schedule:
    """A mowing schedule of one account for a mower or a group.

    ``owner`` is the creating subject (a ``PrincipalRef`` as JSON): every firing re-checks that
    it still has ``can_control`` on each target mower, and commands are issued on its behalf.
    """

    id: UUID
    account_id: UUID
    name: str
    target_type: str
    target_id: UUID
    kind: str
    start_time: time
    duration_minutes: int
    timezone: str
    owner: dict[str, Any]
    owner_subject: UUID
    created_at: datetime
    updated_at: datetime
    days_of_week: list[int] = field(default_factory=list)
    anchor_date: date | None = None
    interval_days: int | None = None
    timezone_override: str | None = None
    area_ids: list[str] = field(default_factory=list)
    pattern: dict[str, Any] | None = None
    rotation_angles: list[float] = field(default_factory=list)
    rotation_index: int = 0
    rain: dict[str, Any] = field(default_factory=dict)
    pause_running_jobs_on_rain: bool = True
    return_to_dock_at_window_end: bool = False
    enabled: bool = True
    disabled_reason: str | None = None
    permission_skips: int = 0
    skip_until: datetime | None = None
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None

    @property
    def recurrence(self) -> Recurrence:
        """The recurrence rule."""
        kind: RecurrenceKind = "weekly" if self.kind == "weekly" else "every_n_days"
        return Recurrence(
            kind=kind,
            at=self.start_time,
            days=frozenset(self.days_of_week),
            anchor=self.anchor_date,
            interval_days=self.interval_days,
        )

    @property
    def rain_settings(self) -> RainSettings:
        """The rain pause settings."""
        return RainSettings.from_json(self.rain)


@dataclass(slots=True)
class ScheduleRun:
    """One attempt of a schedule for one mower (``mower_id`` None when the mowers of the target
    were not known, for example when Fleet did not answer)."""

    id: UUID
    schedule_id: UUID
    account_id: UUID
    trigger: str
    scheduled_for: datetime
    started_at: datetime
    outcome: str
    mower_id: UUID | None = None
    details: str | None = None
    command_id: UUID | None = None
    issued_by: dict[str, Any] | None = None
    """The subject the start was issued for (the owner, or the caller of a manual run)."""
    window_end_at: datetime | None = None
    followup: str | None = None
    """For started runs: what happened afterwards (``rain_dock``, ``window_dock``,
    ``finished``, ``closed``); None while the job may still need a return to the dock."""
    followup_at: datetime | None = None


@dataclass(slots=True)
class MowerStatus:
    """The latest activity of a mower (from ``telemetry.mower.*``), for the busy check."""

    mower_id: UUID
    account_id: UUID
    activity: str
    observed_at: datetime
    latitude: float | None = None
    longitude: float | None = None


@dataclass(slots=True)
class MowerPlace:
    """What the map snapshot of a mower says: its geo origin and area ids."""

    mower_id: UUID
    account_id: UUID
    observed_at: datetime
    latitude: float | None = None
    longitude: float | None = None
    area_ids: list[str] = field(default_factory=list)
