"""Routes of the Scheduler under ``/api/v1/scheduler``."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from mowdeck_authz import Relation
from mowdeck_authz.fastapi import path_object, require_or_not_found
from mowdeck_contracts.v1 import Principal
from mowdeck_service_kit import CurrentAccountId, CurrentPrincipal
from mowdeck_service_kit.errors import NotFound

from scheduler.adapters.api.deps import ServicesDep
from scheduler.adapters.api.schemas import (
    DevWeatherIn,
    PreviewOut,
    RainDecisionOut,
    RunList,
    RunOut,
    ScheduleIn,
    ScheduleList,
    ScheduleOut,
    SchedulePatch,
    WeatherOut,
)
from scheduler.adapters.weather.static import StaticWeatherProvider
from scheduler.application.schedules import ScheduleInput
from scheduler.domain.rain import RainSettings
from scheduler.domain.rules import WEEKDAYS

router = APIRouter(prefix="/scheduler", tags=["schedules"])

ViewMower = Annotated[
    Principal, Depends(require_or_not_found(Relation.CAN_VIEW, path_object("mower", "mower_id")))
]


def _location(response: Response, schedule_id: UUID) -> None:
    response.headers["Location"] = f"/api/v1/scheduler/schedules/{schedule_id}"


@router.get("/schedules")
async def list_schedules(
    principal: CurrentPrincipal, account_id: CurrentAccountId, services: ServicesDep
) -> ScheduleList:
    """Schedules of the caller's account whose target (mower or group) the caller may view."""
    found = await services.schedules.visible(principal, account_id)
    return ScheduleList(items=[ScheduleOut.of(s) for s in found])


@router.post("/schedules", status_code=201)
async def create_schedule(
    body: ScheduleIn,
    principal: CurrentPrincipal,
    account_id: CurrentAccountId,
    services: ServicesDep,
    response: Response,
) -> ScheduleOut:
    """A new schedule: ``can_edit_schedule`` and ``can_control`` on the target (403 when the
    target is visible, 404 otherwise, also for a target of another account). The caller becomes
    the owner; every firing re-checks the owner's ``can_control``."""
    created = await services.schedules.create(
        principal,
        account_id,
        ScheduleInput(
            name=body.name,
            target_type=body.target_type,
            target_id=body.target_id,
            kind=body.kind,
            start_time=body.start_time,
            duration_minutes=body.duration_minutes,
            days_of_week=[WEEKDAYS.index(d) for d in body.days_of_week],
            anchor_date=body.anchor_date,
            interval_days=body.interval_days,
            timezone_override=body.timezone,
            area_ids=list(body.area_ids),
            pattern=body.pattern,
            rotation_angles=list(body.rotation_angles),
            rain=body.rain_pause.settings().to_json(),
            pause_running_jobs_on_rain=body.pause_running_jobs_on_rain,
            return_to_dock_at_window_end=body.return_to_dock_at_window_end,
            enabled=body.enabled,
        ),
    )
    _location(response, created.id)
    return ScheduleOut.of(created)


@router.get("/schedules/{schedule_id}")
async def get_schedule(
    schedule_id: UUID,
    principal: CurrentPrincipal,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> ScheduleOut:
    """One schedule (``can_view`` on its target, 404 otherwise)."""
    return ScheduleOut.of(await services.schedules.get(principal, account_id, schedule_id))


@router.patch("/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: UUID,
    body: SchedulePatch,
    principal: CurrentPrincipal,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> ScheduleOut:
    """Change a schedule (``can_edit_schedule`` and ``can_control`` on the old and a new
    target); the caller becomes the owner and the next run is recomputed."""
    changed = await services.schedules.update(principal, account_id, schedule_id, body.changes())
    return ScheduleOut.of(changed)


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(
    schedule_id: UUID,
    principal: CurrentPrincipal,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> None:
    """Delete a schedule and its runs (``can_edit_schedule`` on the target)."""
    await services.schedules.delete(principal, account_id, schedule_id)


@router.post("/schedules/{schedule_id}/skip-next")
async def skip_next(
    schedule_id: UUID,
    principal: CurrentPrincipal,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> ScheduleOut:
    """Skip the next firing (recorded as ``skipped_manual``); 409 without a next run."""
    return ScheduleOut.of(await services.schedules.skip_next(principal, account_id, schedule_id))


@router.post("/schedules/{schedule_id}/run-now", status_code=202)
async def run_now(
    schedule_id: UUID,
    principal: CurrentPrincipal,
    account_id: CurrentAccountId,
    services: ServicesDep,
    respect_rain: Annotated[bool, Query()] = False,
) -> RunList:
    """Start the schedule now on behalf of the caller (``can_control`` on the target, checked
    again per mower by Fleet). ``respect_rain=true`` applies the rain pause. The next firing
    stays as it is."""
    await services.schedules.manual_target(principal, account_id, schedule_id)
    runs = await services.runner.run_now(
        schedule_id, account_id, principal.ref(), respect_rain=respect_rain
    )
    return RunList(items=[RunOut.of(r) for r in runs])


@router.get("/schedules/{schedule_id}/runs")
async def list_runs(
    schedule_id: UUID,
    principal: CurrentPrincipal,
    account_id: CurrentAccountId,
    services: ServicesDep,
    before: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> RunList:
    """Run records of the schedule, newest first (``can_view`` on the target); pass
    ``next_before`` as ``before`` for the next page."""
    runs = await services.schedules.runs(
        principal, account_id, schedule_id, limit=limit + 1, before=before
    )
    more = len(runs) > limit
    runs = runs[:limit]
    return RunList(
        items=[RunOut.of(r) for r in runs], next_before=runs[-1].started_at if more else None
    )


@router.get("/schedules/{schedule_id}/preview")
async def preview(
    schedule_id: UUID,
    principal: CurrentPrincipal,
    account_id: CurrentAccountId,
    services: ServicesDep,
    count: Annotated[int, Query(ge=1, le=20)] = 5,
) -> PreviewOut:
    """The next firings and the rain decision for the first one (``can_view``)."""
    found = await services.schedules.preview(principal, account_id, schedule_id, count)
    return PreviewOut.of(found)


@router.get("/weather/{mower_id}")
async def weather(
    mower_id: UUID,
    _: ViewMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
    probability_threshold: Annotated[int, Query(ge=0, le=100)] = 60,
    precipitation_mm_threshold: Annotated[float, Query(ge=0, le=100)] = 0.5,
    lookahead_hours: Annotated[int, Query(ge=0, le=24)] = 3,
    cooldown_hours: Annotated[int, Query(ge=0, le=24)] = 2,
) -> WeatherOut:
    """The rain decision for the mower now and the forecast hours it used (``can_view``)."""
    settings = RainSettings(
        probability_threshold=probability_threshold,
        precipitation_mm_threshold=precipitation_mm_threshold,
        lookahead_hours=lookahead_hours,
        cooldown_hours=cooldown_hours,
    )
    answer = await services.schedules.weather_of_mower(account_id, mower_id, settings)
    return WeatherOut(
        mower_id=mower_id, at=services.schedules.clock(), decision=RainDecisionOut.of(answer)
    )


@router.put("/dev/weather", include_in_schema=False)
async def set_dev_weather(
    body: DevWeatherIn, request: Request, principal: CurrentPrincipal
) -> DevWeatherIn:
    """Development only: switch the static weather provider of this replica (404 unless
    ``MOWDECK_SCHEDULER_DEV_ENDPOINTS`` is on and the provider is ``static``)."""
    del principal  # any authenticated caller; the endpoint is off in production
    provider = getattr(request.app.state, "weather_provider", None)
    if not request.app.state.settings.scheduler_dev_endpoints or not isinstance(
        provider, StaticWeatherProvider
    ):
        raise NotFound("not found")
    provider.mode = body.mode
    return body
