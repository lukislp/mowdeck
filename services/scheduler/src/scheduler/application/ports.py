"""Ports: what the use cases need from the outside world, as protocols.

Two stores implement :class:`Store`: PostgreSQL (``adapters.db``) and memory
(``adapters.memory``, tests). The due schedules and the open runs are claimed with
``FOR UPDATE SKIP LOCKED``, so replicas never fire the same schedule twice. Authorization
questions go to ``mowdeck_authz.Authorizer`` directly (a library port of its own).
"""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from mowdeck_contracts.v1 import (
    IssueCommandReply,
    IssueCommandRequest,
    MowerCapabilities,
    ResolveTargetReply,
    ScheduleDisabled,
    ScheduleRunRecorded,
)

from scheduler.domain.models import MowerPlace, MowerStatus, Schedule, ScheduleRun
from scheduler.domain.rain import Forecast
from scheduler.domain.rotation import AreaAngle, MowerRotation


class Clock(Protocol):
    """Returns the current time (timezone-aware)."""

    def __call__(self) -> datetime:
        """The current time."""
        ...


class Tx(Protocol):
    """One transaction: committed together or not at all."""

    async def add_schedule(self, schedule: Schedule) -> None:
        """Insert a schedule."""
        ...

    async def get_schedule(
        self, schedule_id: UUID, account_id: UUID | None = None, *, for_update: bool = False
    ) -> Schedule | None:
        """One schedule (of ``account_id`` when given); ``for_update`` locks the row."""
        ...

    async def list_schedules(self, account_id: UUID) -> list[Schedule]:
        """Every schedule of the account, by name."""
        ...

    async def count_schedules(self, account_id: UUID) -> int:
        """Number of schedules of the account."""
        ...

    async def update_schedule(self, schedule: Schedule) -> None:
        """Write back a changed schedule."""
        ...

    async def delete_schedule(self, schedule_id: UUID, account_id: UUID) -> bool:
        """Delete a schedule and its runs; False if it did not exist."""
        ...

    async def claim_due(self, now: datetime) -> Schedule | None:
        """One enabled schedule with ``next_run_at <= now``, locked (``SKIP LOCKED``)."""
        ...

    async def add_run(self, run: ScheduleRun) -> None:
        """Insert a run record."""
        ...

    async def update_run(self, run: ScheduleRun) -> None:
        """Write back a changed run record."""
        ...

    async def list_runs(
        self, schedule_id: UUID, account_id: UUID, *, limit: int, before: datetime | None
    ) -> list[ScheduleRun]:
        """Runs of a schedule, newest first (keyset on ``started_at``)."""
        ...

    async def claim_open_runs(self, limit: int) -> list[ScheduleRun]:
        """Started runs without a follow-up yet, locked (``SKIP LOCKED``)."""
        ...

    async def put_status(self, status: MowerStatus) -> bool:
        """Store the latest activity unless a newer one is stored; True if stored."""
        ...

    async def get_status(self, mower_id: UUID) -> MowerStatus | None:
        """The latest activity of a mower."""
        ...

    async def put_place(self, place: MowerPlace) -> None:
        """Store the map facts of a mower (replaces older ones)."""
        ...

    async def get_place(self, mower_id: UUID) -> MowerPlace | None:
        """The map facts of a mower."""
        ...

    async def put_capabilities(
        self,
        mower_id: UUID,
        account_id: UUID,
        observed_at: datetime,
        capabilities: MowerCapabilities,
    ) -> bool:
        """Store a mower's capabilities unless newer ones are stored; True if stored."""
        ...

    async def get_capabilities(self, mower_id: UUID) -> MowerCapabilities | None:
        """The latest capabilities of a mower; None when its gateway never reported any."""
        ...

    async def get_area_angles(
        self, mower_id: UUID, area_ids: Sequence[str] | None = None
    ) -> list[AreaAngle]:
        """The last angles of a mower's areas (of ``area_ids`` when given), by area id."""
        ...

    async def put_area_angles(self, angles: Sequence[AreaAngle]) -> None:
        """Store the last angle per mower and area (replaces the stored one)."""
        ...

    async def get_rotation(self, mower_id: UUID) -> MowerRotation | None:
        """The manual rotation step of a mower; None when never set."""
        ...

    async def put_rotation(self, rotation: MowerRotation) -> None:
        """Store the manual rotation step of a mower."""
        ...

    async def purge_runs(self, before: datetime) -> int:
        """Delete finished runs that started before ``before``."""
        ...

    async def try_lock(self, key: int) -> bool:
        """Take a lock held until the transaction ends; False if another holder has it."""
        ...


class Store(Protocol):
    """Opens transactions. Leaving the context commits; an exception rolls back."""

    def transaction(self) -> AbstractAsyncContextManager[Tx]:
        """A new transaction."""
        ...


class FleetGateway(Protocol):
    """The internal requests to Fleet (core NATS)."""

    async def resolve(
        self, account_id: UUID, target_type: str, target_id: UUID
    ) -> ResolveTargetReply:
        """The mowers of a target; raises ``Unavailable`` when Fleet does not answer."""
        ...

    async def issue(self, request: IssueCommandRequest) -> IssueCommandReply:
        """Issue a command on behalf of a subject; raises ``Unavailable`` without an answer."""
        ...


class WeatherProvider(Protocol):
    """Hourly precipitation forecast for a location (the weather adapter port)."""

    @property
    def name(self) -> str:
        """Provider name for logs and responses (``open_meteo``, ``static``)."""
        ...

    async def forecast(self, latitude: float, longitude: float) -> Forecast | None:
        """The forecast around now, or None when it is not available (unknown weather)."""
        ...


class EventPublisher(Protocol):
    """Publishes the Scheduler's events; failures are logged, never raised."""

    async def run_recorded(self, run: ScheduleRunRecorded) -> None:
        """``scheduler.run.recorded``."""
        ...

    async def schedule_disabled(self, event: ScheduleDisabled) -> None:
        """``scheduler.schedule.disabled``."""
        ...


class RunObserver(Protocol):
    """Counts runs per outcome (metrics)."""

    def recorded(self, trigger: str, outcome: str) -> None:
        """One run record."""
        ...
