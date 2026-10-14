"""Schedules: create, list, read, change, delete, skip the next run, preview, run history.

A schedule's target (a mower or a group) guards it: ``can_view`` to see it,
``can_edit_schedule`` plus ``can_control`` to create or change it (a schedule nobody may run
would only skip), ``can_edit_schedule`` to delete it or skip a run. The account is always the
verified token's; schedules of other accounts and invisible targets are 404. Whoever creates
or changes a schedule becomes its owner: firings run on the owner's behalf and re-check its
``can_control``.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from mowdeck_authz import account
from mowdeck_contracts.v1 import Principal
from mowdeck_service_kit.entitlements import (
    Entitlements,
    EntitlementsClient,
    require_feature,
    require_limit,
)

from scheduler.application.access import Access, subject_of, target_object
from scheduler.application.context import SchedulerConfig
from scheduler.application.ports import Clock, FleetGateway, Store
from scheduler.application.runner import compute_next
from scheduler.application.weather import WeatherAnswer, WeatherService
from scheduler.domain.errors import ConflictError, ForbiddenError, NotFoundError
from scheduler.domain.models import Schedule, ScheduleRun
from scheduler.domain.rain import RainSettings
from scheduler.domain.recurrence import upcoming
from scheduler.domain.rules import (
    CAN_CONTROL,
    CAN_EDIT_SCHEDULE,
    CAN_VIEW,
    validate_schedule,
    window_end,
)

_NOT_FOUND = "schedule not found"

EDITABLE = frozenset(
    {
        "name",
        "target_type",
        "target_id",
        "kind",
        "days_of_week",
        "anchor_date",
        "interval_days",
        "start_time",
        "duration_minutes",
        "timezone_override",
        "area_ids",
        "pattern",
        "rotation_angles",
        "rain",
        "pause_running_jobs_on_rain",
        "return_to_dock_at_window_end",
        "enabled",
    }
)
"""Fields a client may set (create and update)."""


@dataclass(frozen=True, slots=True)
class ScheduleInput:
    """What a new schedule says (the API validated the shapes)."""

    name: str
    target_type: str
    target_id: UUID
    kind: str
    start_time: time
    duration_minutes: int = 120
    days_of_week: list[int] = field(default_factory=list)
    anchor_date: date | None = None
    interval_days: int | None = None
    timezone_override: str | None = None
    area_ids: list[str] = field(default_factory=list)
    pattern: dict[str, Any] | None = None
    rotation_angles: list[float] = field(default_factory=list)
    rain: dict[str, Any] = field(default_factory=lambda: RainSettings().to_json())
    pause_running_jobs_on_rain: bool = True
    return_to_dock_at_window_end: bool = False
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class PreviewItem:
    """One upcoming firing."""

    at: datetime
    window_end: datetime
    skipped: bool


@dataclass(frozen=True, slots=True)
class Preview:
    """Upcoming firings; the rain decision for the first one when a forecast exists."""

    items: list[PreviewItem]
    weather: WeatherAnswer | None
    weather_mower_id: UUID | None


class ScheduleService:
    """Use cases of schedules."""

    def __init__(
        self,
        store: Store,
        access: Access,
        fleet: FleetGateway,
        weather: WeatherService,
        config: SchedulerConfig,
        clock: Clock,
        entitlements: Entitlements | None = None,
    ) -> None:
        self.store = store
        self.access = access
        self.fleet = fleet
        self.weather = weather
        self.config = config
        self.clock = clock
        self.entitlements: Entitlements = (
            entitlements if entitlements is not None else EntitlementsClient(None)
        )

    # --- guards --------------------------------------------------------------------------

    async def _may_edit(self, principal: Principal, target_type: str, target_id: UUID) -> None:
        obj = target_object(target_type, target_id)
        await self.access.require(principal, CAN_EDIT_SCHEDULE, obj)
        await self.access.require(principal, CAN_CONTROL, obj)

    async def _target_timezone(self, account_id: UUID, target_type: str, target_id: UUID) -> str:
        reply = await self.fleet.resolve(account_id, target_type, target_id)
        if not reply.found or reply.timezone is None:
            raise NotFoundError(f"{target_type} not found")
        return reply.timezone

    async def _load(self, account_id: UUID, schedule_id: UUID) -> Schedule:
        async with self.store.transaction() as tx:
            found = await tx.get_schedule(schedule_id, account_id)
        if found is None:
            raise NotFoundError(_NOT_FOUND)
        return found

    async def _visible(
        self, principal: Principal, account_id: UUID, schedule_id: UUID, relation: str
    ) -> Schedule:
        """The schedule if ``principal`` has ``relation`` on its target (404 when the target is
        invisible, 403 when visible without the relation).

        The same relation on the whole account also admits it, so an owner or admin still sees
        (and can delete) a schedule whose mower or group was removed in Fleet.
        """
        found = await self._load(account_id, schedule_id)
        obj = target_object(found.target_type, found.target_id)
        try:
            await self.access.require(principal, relation, obj, what="schedule")
        except (NotFoundError, ForbiddenError):
            if not await self.access.allowed(subject_of(principal), relation, account(account_id)):
                raise
        return found

    # --- reading -------------------------------------------------------------------------

    async def visible(self, principal: Principal, account_id: UUID) -> list[Schedule]:
        """Schedules of the account whose target the principal may view."""
        visible = await self.access.visible_targets(principal, account_id)
        async with self.store.transaction() as tx:
            rows = await tx.list_schedules(account_id)
        return [
            s
            for s in rows
            if visible is None or str(target_object(s.target_type, s.target_id)) in visible
        ]

    async def get(self, principal: Principal, account_id: UUID, schedule_id: UUID) -> Schedule:
        """One schedule (``can_view`` on its target)."""
        return await self._visible(principal, account_id, schedule_id, CAN_VIEW)

    async def runs(
        self,
        principal: Principal,
        account_id: UUID,
        schedule_id: UUID,
        *,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[ScheduleRun]:
        """Run records, newest first (``can_view`` on the target)."""
        await self._visible(principal, account_id, schedule_id, CAN_VIEW)
        async with self.store.transaction() as tx:
            return await tx.list_runs(schedule_id, account_id, limit=limit, before=before)

    async def preview(
        self, principal: Principal, account_id: UUID, schedule_id: UUID, count: int
    ) -> Preview:
        """The next ``count`` firings and the rain decision for the first one."""
        found = await self._visible(principal, account_id, schedule_id, CAN_VIEW)
        now = self.clock()
        times = upcoming(found.recurrence, ZoneInfo(found.timezone), now, count)
        items = [
            PreviewItem(
                at=t,
                window_end=window_end(t, found.duration_minutes),
                skipped=found.skip_until is not None and t <= found.skip_until,
            )
            for t in times
        ]
        answer, mower_id = None, None
        first = next((i for i in items if not i.skipped), None)
        if first is not None and found.enabled:
            answer, mower_id = await self._weather_for(found, first.at)
        return Preview(items, answer, mower_id)

    async def _weather_for(
        self, schedule: Schedule, at: datetime
    ) -> tuple[WeatherAnswer | None, UUID | None]:
        try:
            target = await self.fleet.resolve(
                schedule.account_id, schedule.target_type, schedule.target_id
            )
        except Exception:
            return None, None
        for mower in target.mowers:
            location = await self.weather.locate(mower.mower_id, mower)
            if location is not None:
                answer = await self.weather.decide(schedule.rain_settings, location, at)
                return answer, mower.mower_id
        if target.mowers:
            first = target.mowers[0]
            return await self.weather.decide(schedule.rain_settings, None, at), first.mower_id
        return None, None

    # --- changes -------------------------------------------------------------------------

    async def create(self, principal: Principal, account_id: UUID, data: ScheduleInput) -> Schedule:
        """A new schedule owned by ``principal``.

        Plans (ADR 0028): the feature ``schedules`` (403 ``feature-not-in-plan``) and the limit
        ``max_schedules`` (409 ``plan-limit``).
        """
        await self._may_edit(principal, data.target_type, data.target_id)
        await require_feature(self.entitlements, account_id, "schedules")
        timezone = data.timezone_override or await self._target_timezone(
            account_id, data.target_type, data.target_id
        )
        now = self.clock()
        item = Schedule(
            id=uuid4(),
            account_id=account_id,
            name=data.name,
            target_type=data.target_type,
            target_id=data.target_id,
            kind=data.kind,
            start_time=data.start_time,
            duration_minutes=data.duration_minutes,
            timezone=timezone,
            owner=principal.ref().model_dump(mode="json"),
            owner_subject=principal.subject,
            created_at=now,
            updated_at=now,
            days_of_week=list(data.days_of_week),
            anchor_date=data.anchor_date,
            interval_days=data.interval_days,
            timezone_override=data.timezone_override,
            area_ids=list(data.area_ids),
            pattern=data.pattern,
            rotation_angles=list(data.rotation_angles),
            rain=dict(data.rain),
            pause_running_jobs_on_rain=data.pause_running_jobs_on_rain,
            return_to_dock_at_window_end=data.return_to_dock_at_window_end,
            enabled=data.enabled,
        )
        validate_schedule(item)
        item.next_run_at = compute_next(item, now) if item.enabled else None
        async with self.store.transaction() as tx:
            existing = await tx.count_schedules(account_id)
            if existing >= self.config.max_schedules_per_account:
                limit = self.config.max_schedules_per_account
                raise ConflictError(f"the account has reached its limit of {limit} schedules")
            await require_limit(self.entitlements, account_id, "max_schedules", existing)
            await tx.add_schedule(item)
        return item

    async def update(
        self,
        principal: Principal,
        account_id: UUID,
        schedule_id: UUID,
        changes: Mapping[str, Any],
    ) -> Schedule:
        """Change the given fields; the editor becomes the owner, the next run is recomputed."""
        found = await self._visible(principal, account_id, schedule_id, CAN_EDIT_SCHEDULE)
        await self._may_edit(principal, found.target_type, found.target_id)
        unknown = set(changes) - EDITABLE
        if unknown:  # pragma: no cover - the API refuses unknown fields
            raise NotFoundError(f"unknown fields {sorted(unknown)}")
        target_changed = {"target_type", "target_id"} & set(changes)
        new_type = changes.get("target_type", found.target_type)
        new_id = changes.get("target_id", found.target_id)
        if target_changed:
            await self._may_edit(principal, new_type, new_id)
        timezone = found.timezone
        if target_changed or "timezone_override" in changes:
            override = changes.get("timezone_override", found.timezone_override)
            timezone = override or await self._target_timezone(account_id, new_type, new_id)
        now = self.clock()
        async with self.store.transaction() as tx:
            item = await tx.get_schedule(schedule_id, account_id, for_update=True)
            if item is None:
                raise NotFoundError(_NOT_FOUND)
            was_enabled = item.enabled
            for name, value in changes.items():
                setattr(item, name, value)
            if target_changed and new_type == "group" and "area_ids" not in changes:
                item.area_ids = []
            item.timezone = timezone
            item.owner = principal.ref().model_dump(mode="json")
            item.owner_subject = principal.subject
            validate_schedule(item)
            if item.enabled and not was_enabled:
                item.permission_skips = 0
                item.disabled_reason = None
            item.next_run_at = compute_next(item, now) if item.enabled else None
            if item.skip_until is not None and (
                item.next_run_at is None or item.skip_until < item.next_run_at
            ):
                item.skip_until = None
            item.updated_at = now
            await tx.update_schedule(item)
        return item

    async def delete(self, principal: Principal, account_id: UUID, schedule_id: UUID) -> None:
        """Delete a schedule and its runs (``can_edit_schedule`` on the target)."""
        await self._visible(principal, account_id, schedule_id, CAN_EDIT_SCHEDULE)
        async with self.store.transaction() as tx:
            if not await tx.delete_schedule(schedule_id, account_id):
                raise NotFoundError(_NOT_FOUND)

    async def skip_next(
        self, principal: Principal, account_id: UUID, schedule_id: UUID
    ) -> Schedule:
        """Skip the next firing (it is recorded as ``skipped_manual``)."""
        await self._visible(principal, account_id, schedule_id, CAN_EDIT_SCHEDULE)
        async with self.store.transaction() as tx:
            item = await tx.get_schedule(schedule_id, account_id, for_update=True)
            if item is None:
                raise NotFoundError(_NOT_FOUND)
            if not item.enabled or item.next_run_at is None:
                raise ConflictError("the schedule has no next run")
            item.skip_until = item.next_run_at
            item.updated_at = self.clock()
            await tx.update_schedule(item)
        return item

    async def manual_target(
        self, principal: Principal, account_id: UUID, schedule_id: UUID
    ) -> Schedule:
        """The schedule of a manual run: ``can_control`` on its target (404 when invisible)."""
        return await self._visible(principal, account_id, schedule_id, CAN_CONTROL)

    async def weather_of_mower(
        self, account_id: UUID, mower_id: UUID, settings: RainSettings
    ) -> WeatherAnswer:
        """The current rain decision for one mower (the route checked ``can_view``)."""
        reply = await self.fleet.resolve(account_id, "mower", mower_id)
        if not reply.found:
            raise NotFoundError("mower not found")
        target = reply.mowers[0] if reply.mowers else None
        location = await self.weather.locate(mower_id, target)
        return await self.weather.decide(settings, location, self.clock())
