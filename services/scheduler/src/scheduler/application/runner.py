"""Firing schedules: the ticker's work, manual runs and the follow-up of started jobs.

Every replica ticks (``MOWDECK_SCHEDULER_TICK_SECONDS``). A tick claims due schedules one at a
time (``FOR UPDATE SKIP LOCKED``), fires each inside its own transaction and stores the next
occurrence, so two replicas never fire the same schedule and a crash before the commit fires it
again on the next tick instead of losing it.

One firing, per target mower (see ADR 0024):

1. a manual skip (``skip_until``) or a closed window ends the run for every mower;
2. the owner (the creating subject) must still have ``can_control`` on the mower
   (``skipped_permission``; after ``max_permission_skips`` firings in a row where every mower
   was skipped for this reason the schedule is disabled and ``scheduler.schedule.disabled`` is
   published);
3. a mower that is mowing, paused or returning is not started (``skipped_mower_busy``);
4. the rain pause may hold the run (``skipped_rain``);
5. the schedule's blade height and speed go out first (``set_blade_height``, ``set_speed``;
   the controller runs a mower's commands in arrival order), then ``start_mow`` through Fleet
   (``fleet.commands.issue``) on behalf of the owner, with the schedule's areas (all map areas
   when none are set) and the settings of this run: schedule over the mower's mow defaults over
   the mower's own setting, the angle by rotation (the list, or the step from the mower's last
   angle on these areas, ADR 0061), fitted to the mower's capabilities
   (``rules.run_settings``). A started run stores its angle per mower and area.

Started runs are followed until their job ends: when it rains while the mower is still mowing
(``pause_running_jobs_on_rain``) or the window closes (``return_to_dock_at_window_end``) the
mower is sent back to its dock, again through Fleet.
"""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from mowdeck_authz import mower
from mowdeck_contracts.v1 import (
    CommandRequest,
    IssueCommandErrorCode,
    IssueCommandRequest,
    PrincipalRef,
    ResolveTargetReply,
    ReturnToDock,
    ScheduleDisabled,
    ScheduleRunOutcome,
    ScheduleRunRecorded,
    StartMow,
    TargetMower,
)
from mowdeck_service_kit import get_logger

from scheduler.application.access import Access, subject_of_ref
from scheduler.application.context import SchedulerConfig
from scheduler.application.ports import (
    Clock,
    EventPublisher,
    FleetGateway,
    RunObserver,
    Store,
    Tx,
)
from scheduler.application.weather import WeatherService
from scheduler.domain.errors import NotFoundError, Unavailable
from scheduler.domain.models import Schedule, ScheduleRun
from scheduler.domain.recurrence import next_after
from scheduler.domain.rotation import AreaAngle
from scheduler.domain.rules import (
    BUSY_ACTIVITIES,
    CAN_CONTROL,
    FINISHED_ACTIVITIES,
    RUNNING_ACTIVITIES,
    run_angle,
    run_settings,
    step_angle,
    window_end,
)

_log = get_logger("scheduler.runner")

PURGE_LOCK = 0x5363_6865_6452_756E  # "SchedRun": one replica purges at a time
Outcome = ScheduleRunOutcome


@dataclass(slots=True)
class Firing:
    """What one firing produced (for the events after the commit)."""

    runs: list[ScheduleRun] = field(default_factory=list)
    disabled: ScheduleDisabled | None = None


def recorded(run: ScheduleRun) -> ScheduleRunRecorded:
    """The event payload of a run."""
    return ScheduleRunRecorded(
        run_id=run.id,
        schedule_id=run.schedule_id,
        account_id=run.account_id,
        mower_id=run.mower_id,
        trigger="manual" if run.trigger == "manual" else "schedule",
        outcome=ScheduleRunOutcome(run.outcome),
        details=run.details[:500] if run.details else None,
        command_id=run.command_id,
        scheduled_for=run.scheduled_for,
        started_at=run.started_at,
        angle_deg=run.angle_deg,
    )


def _started_details(
    area_ids: list[str],
    pattern: dict[str, object],
    dropped: list[str],
) -> str:
    """The details of a started run: areas, angle, what the mower did not offer."""
    angle = pattern.get("angle_deg")
    text = f"areas {', '.join(area_ids)}"
    if isinstance(angle, int | float):
        text += f", angle {angle:g}"
    if dropped:
        text += f", left out (not offered by the mower): {', '.join(dropped)}"
    return text


def compute_next(schedule: Schedule, after: datetime) -> datetime | None:
    """The next occurrence of ``schedule`` strictly after ``after`` (a day fires at most once,
    also when its time exists twice on a fall-back day, see :mod:`scheduler.domain.recurrence`)."""
    return next_after(schedule.recurrence, ZoneInfo(schedule.timezone), after, schedule.last_run_at)


class Runner:
    """The firing use cases."""

    def __init__(
        self,
        store: Store,
        fleet: FleetGateway,
        access: Access,
        weather: WeatherService,
        events: EventPublisher,
        config: SchedulerConfig,
        clock: Clock,
        observer: RunObserver | None = None,
    ) -> None:
        self.store = store
        self.fleet = fleet
        self.access = access
        self.weather = weather
        self.events = events
        self.config = config
        self.clock = clock
        self.observer = observer

    # --- the ticker ----------------------------------------------------------------------

    async def tick(self) -> int:
        """Fire due schedules (at most one batch) and follow open runs; returns the firings."""
        fired = 0
        for _ in range(self.config.batch_size):
            firing: Firing | None = None
            async with self.store.transaction() as tx:
                schedule = await tx.claim_due(self.clock())
                if schedule is None:
                    break
                firing = await self._fire(tx, schedule)
            fired += 1
            await self._announce(firing)
        await self.follow_up()
        return fired

    async def _fire(self, tx: Tx, schedule: Schedule) -> Firing:
        now = self.clock()
        occurrence = schedule.next_run_at or now
        firing = Firing()
        closes = window_end(occurrence, schedule.duration_minutes)
        if schedule.skip_until is not None and occurrence <= schedule.skip_until:
            schedule.skip_until = None
            firing.runs.append(
                self._run(schedule, occurrence, Outcome.SKIPPED_MANUAL, "skipped by a user")
            )
        elif now >= closes:
            firing.runs.append(
                self._run(
                    schedule,
                    occurrence,
                    Outcome.SKIPPED_WINDOW,
                    f"the window closed at {closes.isoformat()} before the run could start",
                )
            )
        else:
            await self._fire_mowers(tx, schedule, occurrence, closes, firing)
        schedule.last_run_at = now
        schedule.next_run_at = (
            compute_next(schedule, max(now, occurrence)) if schedule.enabled else None
        )
        schedule.updated_at = now
        for run in firing.runs:
            await tx.add_run(run)
        await tx.update_schedule(schedule)
        return firing

    async def _fire_mowers(
        self,
        tx: Tx,
        schedule: Schedule,
        occurrence: datetime,
        closes: datetime,
        firing: Firing,
    ) -> None:
        try:
            target = await self.fleet.resolve(
                schedule.account_id, schedule.target_type, schedule.target_id
            )
        except Unavailable as exc:
            firing.runs.append(self._run(schedule, occurrence, Outcome.FAILED, exc.message))
            return
        if not target.found:
            firing.runs.append(
                self._run(schedule, occurrence, Outcome.FAILED, "the target no longer exists")
            )
            firing.disabled = self._disable(schedule, "the target no longer exists")
            return
        self._follow_timezone(schedule, target)
        owner = PrincipalRef.model_validate(schedule.owner)
        runs = await self.start_each(
            tx,
            schedule,
            target.mowers,
            owner,
            trigger="schedule",
            scheduled_for=occurrence,
            closes=closes,
            respect_rain=True,
        )
        firing.runs.extend(runs)
        if not runs:
            firing.runs.append(
                self._run(schedule, occurrence, Outcome.FAILED, "the group has no mowers")
            )
            return
        outcomes = {r.outcome for r in runs}
        if outcomes == {Outcome.SKIPPED_PERMISSION.value}:
            schedule.permission_skips += 1
            if schedule.permission_skips >= self.config.max_permission_skips:
                firing.disabled = self._disable(
                    schedule,
                    f"the owner lost can_control on the target ({schedule.permission_skips} runs "
                    "skipped in a row)",
                )
        elif Outcome.STARTED.value in outcomes:
            schedule.permission_skips = 0

    @staticmethod
    def _follow_timezone(schedule: Schedule, target: ResolveTargetReply) -> None:
        """Without an override the schedule follows its site's time zone."""
        if schedule.timezone_override is None and target.timezone:
            schedule.timezone = target.timezone

    def _disable(self, schedule: Schedule, reason: str) -> ScheduleDisabled:
        now = self.clock()
        schedule.enabled = False
        schedule.disabled_reason = reason
        _log.warning("schedule disabled", schedule_id=str(schedule.id), reason=reason)
        return ScheduleDisabled(
            schedule_id=schedule.id,
            account_id=schedule.account_id,
            name=schedule.name,
            reason=reason,
            owner=PrincipalRef.model_validate(schedule.owner),
            disabled_at=now,
        )

    # --- starting ------------------------------------------------------------------------

    async def start_each(
        self,
        tx: Tx,
        schedule: Schedule,
        mowers: list[TargetMower],
        issuer: PrincipalRef,
        *,
        trigger: str,
        scheduled_for: datetime,
        closes: datetime,
        respect_rain: bool,
    ) -> list[ScheduleRun]:
        """Check and start every mower; returns one run per mower (not stored yet)."""
        subject = subject_of_ref(issuer)
        runs: list[ScheduleRun] = []
        started = False
        for target in mowers:
            run = await self._start_one(
                tx,
                schedule,
                target,
                issuer,
                subject_allowed=await self.access.allowed(
                    subject, CAN_CONTROL, mower(target.mower_id)
                ),
                trigger=trigger,
                scheduled_for=scheduled_for,
                closes=closes,
                respect_rain=respect_rain,
            )
            started = started or run.outcome == Outcome.STARTED.value
            runs.append(run)
        if started:
            schedule.rotation_index += 1
        return runs

    async def _start_one(
        self,
        tx: Tx,
        schedule: Schedule,
        target: TargetMower,
        issuer: PrincipalRef,
        *,
        subject_allowed: bool,
        trigger: str,
        scheduled_for: datetime,
        closes: datetime,
        respect_rain: bool,
    ) -> ScheduleRun:
        mower_id = target.mower_id

        def run(outcome: Outcome, details: str | None = None) -> ScheduleRun:
            return self._run(schedule, scheduled_for, outcome, details, mower_id, trigger)

        if not subject_allowed:
            return run(Outcome.SKIPPED_PERMISSION, "the issuer has no can_control on the mower")
        status = await tx.get_status(mower_id)
        place = await tx.get_place(mower_id)
        if status is not None and status.activity in BUSY_ACTIVITIES:
            return run(Outcome.SKIPPED_MOWER_BUSY, f"the mower is {status.activity}")
        if respect_rain and schedule.rain_settings.enabled:
            location = self.weather.locate_from(target, place, status)
            answer = await self.weather.decide(schedule.rain_settings, location, self.clock())
            if answer.decision.verdict.holds:
                return run(Outcome.SKIPPED_RAIN, answer.decision.reason)
        area_ids = list(schedule.area_ids) or (list(place.area_ids) if place else [])
        if not area_ids:
            return run(Outcome.FAILED, "no areas: set area_ids or wait for the mower's map")
        defaults = (
            target.mow_pattern.model_dump(mode="json", exclude_none=True)
            if target.mow_pattern is not None
            else None
        )
        angle = (
            step_angle(schedule, defaults, await tx.get_area_angles(mower_id, area_ids), area_ids)
            if schedule.rotation_step_deg is not None
            else None
        )
        settings = run_settings(schedule, defaults, await tx.get_capabilities(mower_id), angle)
        command = StartMow(area_ids=area_ids, pattern=settings.pattern)
        result = run(Outcome.STARTED)
        result.issued_by = issuer.model_dump(mode="json")
        for setpoint in [*settings.setpoints, command]:
            try:
                reply = await self.fleet.issue(
                    IssueCommandRequest(
                        on_behalf_of=issuer,
                        account_id=schedule.account_id,
                        mower_id=mower_id,
                        command=CommandRequest(command=setpoint),
                        schedule_id=schedule.id,
                    )
                )
            except Unavailable as exc:
                return run(Outcome.FAILED, f"{exc.message} ({setpoint.kind})")
            if reply.error is not None:
                outcome = (
                    Outcome.SKIPPED_PERMISSION
                    if reply.error.code
                    in (IssueCommandErrorCode.FORBIDDEN, IssueCommandErrorCode.NOT_FOUND)
                    else Outcome.FAILED
                )
                detail = f"fleet: {reply.error.code.value}: {reply.error.detail}"
                return run(outcome, f"{detail} ({setpoint.kind})")
            assert reply.accepted is not None  # noqa: S101 - the reply has exactly one of both
            result.command_id = reply.accepted.command_id
        result.window_end_at = closes
        result.details = _started_details(area_ids, settings.pattern, settings.dropped)
        result.angle_deg = run_angle(settings.pattern)
        if result.angle_deg is not None:
            # Where the next step rotation of this mower continues, also for other schedules.
            await tx.put_area_angles(
                [
                    AreaAngle(
                        mower_id=mower_id,
                        area_id=area_id,
                        account_id=schedule.account_id,
                        angle_deg=result.angle_deg,
                        used_at=result.started_at,
                        source="schedule",
                        schedule_id=schedule.id,
                    )
                    for area_id in dict.fromkeys(area_ids)
                ]
            )
        return result

    def _run(
        self,
        schedule: Schedule,
        scheduled_for: datetime,
        outcome: Outcome,
        details: str | None = None,
        mower_id: UUID | None = None,
        trigger: str = "schedule",
    ) -> ScheduleRun:
        if mower_id is None and schedule.target_type == "mower":
            mower_id = schedule.target_id
        return ScheduleRun(
            id=uuid4(),
            schedule_id=schedule.id,
            account_id=schedule.account_id,
            trigger=trigger,
            scheduled_for=scheduled_for,
            started_at=self.clock(),
            outcome=outcome.value,
            mower_id=mower_id,
            details=details[:500] if details else None,
        )

    async def run_now(
        self, schedule_id: UUID, account_id: UUID, issuer: PrincipalRef, *, respect_rain: bool
    ) -> list[ScheduleRun]:
        """A manual run on behalf of ``issuer`` (the route checked ``can_control`` on the
        target); next occurrence and permission counter stay as they are."""
        now = self.clock()
        firing = Firing()
        async with self.store.transaction() as tx:
            schedule = await tx.get_schedule(schedule_id, account_id, for_update=True)
            if schedule is None:
                raise NotFoundError("schedule not found")
            target = await self.fleet.resolve(
                schedule.account_id, schedule.target_type, schedule.target_id
            )
            if not target.found:
                raise NotFoundError("the target of the schedule no longer exists")
            firing.runs = await self.start_each(
                tx,
                schedule,
                target.mowers,
                issuer,
                trigger="manual",
                scheduled_for=now,
                closes=window_end(now, schedule.duration_minutes),
                respect_rain=respect_rain,
            )
            for run in firing.runs:
                await tx.add_run(run)
            schedule.updated_at = now
            await tx.update_schedule(schedule)
        await self._announce(firing)
        return firing.runs

    # --- follow-up of started jobs -------------------------------------------------------

    async def follow_up(self) -> int:
        """Send started jobs back to the dock on rain or at window end; close finished ones."""
        changed = 0
        async with self.store.transaction() as tx:
            for run in await tx.claim_open_runs(self.config.batch_size):
                if await self._follow(tx, run):
                    await tx.update_run(run)
                    changed += 1
        return changed

    async def _follow(self, tx: Tx, run: ScheduleRun) -> bool:
        now = self.clock()
        schedule = await tx.get_schedule(run.schedule_id)
        status = await tx.get_status(run.mower_id) if run.mower_id else None
        if schedule is None or run.mower_id is None:
            return self._close(run, "closed", now)
        activity = status.activity if status is not None else None
        seen_after_start = (
            status is not None and status.observed_at >= run.started_at + self.config.finished_grace
        )
        if seen_after_start and activity in FINISHED_ACTIVITIES:
            return self._close(run, "finished", now)
        closes = run.window_end_at or run.started_at
        if now >= closes:
            if schedule.return_to_dock_at_window_end and activity in RUNNING_ACTIVITIES:
                await self._dock(schedule, run, "the window closed")
                return self._close(run, "window_dock", now)
            if now >= closes + self.config.followup_horizon:
                return self._close(run, "closed", now)
            return False
        rain = schedule.rain_settings
        if schedule.pause_running_jobs_on_rain and rain.enabled and activity == "mowing":
            location = self.weather.locate_from(None, await tx.get_place(run.mower_id), status)
            answer = await self.weather.decide(rain, location, now)
            if answer.decision.verdict.holds:
                await self._dock(schedule, run, f"rain pause: {answer.decision.reason}")
                return self._close(run, "rain_dock", now)
        return False

    @staticmethod
    def _close(run: ScheduleRun, followup: str, now: datetime) -> bool:
        run.followup = followup
        run.followup_at = now
        return True

    async def _dock(self, schedule: Schedule, run: ScheduleRun, why: str) -> None:
        issuer = PrincipalRef.model_validate(run.issued_by or schedule.owner)
        assert run.mower_id is not None  # noqa: S101 - checked by the caller
        try:
            reply = await self.fleet.issue(
                IssueCommandRequest(
                    on_behalf_of=issuer,
                    account_id=schedule.account_id,
                    mower_id=run.mower_id,
                    command=CommandRequest(command=ReturnToDock()),
                    schedule_id=schedule.id,
                )
            )
        except Unavailable as exc:
            run.details = f"{run.details or ''}; return to dock failed: {exc.message}"[:500]
            return
        outcome = "sent" if reply.accepted else f"refused ({reply.error and reply.error.code})"
        run.details = f"{run.details or ''}; {why}, return to dock {outcome}"[:500]
        _log.info(
            "job sent back to the dock",
            schedule_id=str(schedule.id),
            mower_id=str(run.mower_id),
            reason=why,
            outcome=outcome,
        )

    # --- events and retention ------------------------------------------------------------

    async def _announce(self, firing: Firing | None) -> None:
        if firing is None:
            return
        for run in firing.runs:
            if self.observer is not None:
                self.observer.recorded(run.trigger, run.outcome)
            _log.info(
                "schedule run recorded",
                schedule_id=str(run.schedule_id),
                mower_id=str(run.mower_id) if run.mower_id else None,
                trigger=run.trigger,
                outcome=run.outcome,
                details=run.details,
                command_id=str(run.command_id) if run.command_id else None,
            )
            await self.events.run_recorded(recorded(run))
        if firing.disabled is not None:
            await self.events.schedule_disabled(firing.disabled)

    async def purge(self) -> int:
        """Delete run records older than the retention; one replica at a time."""
        cutoff = self.clock() - self.config.run_retention
        async with self.store.transaction() as tx:
            if not await tx.try_lock(PURGE_LOCK):
                return 0
            return await tx.purge_runs(cutoff)
