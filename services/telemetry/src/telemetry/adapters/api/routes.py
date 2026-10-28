"""Routes of telemetry under ``/api/v1/telemetry``.

Every mower route needs ``can_view`` on the mower (404 otherwise); the blade reset
needs ``can_manage_mowers`` (403 when the mower is visible without it). The account-wide routes
(states, report summary) answer only for the mowers the caller may view (``list_objects``).
Time parameters are ISO 8601 with a time zone; ``from`` defaults to one day before ``to``,
``to`` to now.
"""

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Path, Query, Request, Response
from mowdeck_service_kit import CurrentAccountId, CurrentPrincipal

from telemetry.adapters.api.deps import (
    ManageMower,
    ServicesDep,
    ViewMower,
    visible_mower_ids,
)
from telemetry.adapters.api.schemas import (
    AerialOut,
    BatteryPointOut,
    BatterySeries,
    BladeChangeList,
    BladeChangeOut,
    BladeResetIn,
    BladeThresholdIn,
    CommandEventOut,
    CommandTimeline,
    HealthDaily,
    HealthDayOut,
    HistoryPointOut,
    MowerCapabilitiesList,
    MowerCapabilitiesOut,
    MowerHealthOut,
    MowerHeatmap,
    MowerMapOut,
    MowerStateList,
    MowerStateSnapshot,
    MowPathOut,
    PositionOut,
    PositionTrack,
    ReportSummary,
    StateHistory,
)
from telemetry.application.heatmap import DEFAULT_CELL_M, Layer

router = APIRouter(prefix="/telemetry", tags=["telemetry"])

FromQuery = Annotated[
    datetime | None, Query(alias="from", description="Start of the range (inclusive).")
]
ToQuery = Annotated[datetime | None, Query(alias="to", description="End of the range (exclusive).")]
StepQuery = Annotated[
    str | None,
    Query(description="Bin width such as 5s, 1m, 1h or 1d (date_bin); raw rows without."),
]
FromDay = Annotated[
    date | None, Query(alias="from", description="First UTC day (inclusive, YYYY-MM-DD).")
]
ToDay = Annotated[date | None, Query(alias="to", description="Last UTC day (inclusive).")]


@router.get("/mowers/{mower_id}/state")
async def get_state(
    mower_id: UUID, _: ViewMower, account_id: CurrentAccountId, services: ServicesDep
) -> MowerStateSnapshot:
    """Latest state of a mower; 404 "no telemetry yet" before the first state arrived."""
    row = await services.queries.state(account_id, mower_id)
    return MowerStateSnapshot.of(row, stale=services.queries.is_stale(row))


@router.get("/mowers/{mower_id}/history")
async def get_history(
    mower_id: UUID,
    _: ViewMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
    start: FromQuery = None,
    end: ToQuery = None,
    step: StepQuery = None,
) -> StateHistory:
    """State history; with ``step`` one point per bin (last activity and RTK fix, battery mean
    and range, charging if any, last position)."""
    result = await services.queries.history(account_id, mower_id, start, end, step)
    return StateHistory(
        mower_id=mower_id,
        start=result.start,
        end=result.end,
        step_seconds=result.step.total_seconds() if result.step else None,
        points=[HistoryPointOut.of(p) for p in result.points],
        truncated=result.truncated,
    )


@router.get("/mowers/{mower_id}/positions")
async def get_positions(
    mower_id: UUID,
    _: ViewMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
    start: FromQuery = None,
    end: ToQuery = None,
    limit: Annotated[int, Query(ge=1, le=100_000)] = 1000,
) -> PositionTrack:
    """The newest ``limit`` positions of the range, oldest first."""
    result = await services.queries.positions(account_id, mower_id, start, end, limit)
    return PositionTrack(
        mower_id=mower_id,
        start=result.start,
        end=result.end,
        positions=[PositionOut.of(p) for p in result.positions],
        truncated=result.truncated,
    )


@router.get("/mowers/{mower_id}/commands")
async def get_commands(
    mower_id: UUID,
    _: ViewMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
) -> CommandTimeline:
    """Status transitions of the mower's commands (accepted, executed, ...), newest first."""
    events = await services.queries.commands(account_id, mower_id, limit)
    return CommandTimeline(mower_id=mower_id, items=[CommandEventOut.of(e) for e in events])


@router.get(
    "/mowers/{mower_id}/map",
    response_model=MowerMapOut,
    responses={304: {"description": "Not modified (``If-None-Match`` equals the ETag)"}},
)
async def get_map(
    mower_id: UUID,
    _: ViewMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
    response: Response,
    if_none_match: Annotated[str | None, Header()] = None,
) -> MowerMapOut | Response:
    """The mower's map as the Device-Gateway last read it; 404 ``no-map-yet`` before that.

    The ETag is the map revision, so a client can revalidate with ``If-None-Match``.
    """
    row = await services.queries.snapshot(account_id, mower_id, "map")
    etag = f'"{row.revision}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if if_none_match is not None and etag in [t.strip() for t in if_none_match.split(",")]:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return MowerMapOut.of(row)


@router.get("/mowers/{mower_id}/capabilities")
async def get_capabilities(
    mower_id: UUID, _: ViewMower, account_id: CurrentAccountId, services: ServicesDep
) -> MowerCapabilitiesOut:
    """What the mower can do over its connection; 404 ``no-capabilities-yet`` before the
    gateway reported it."""
    row = await services.queries.snapshot(account_id, mower_id, "capabilities")
    return MowerCapabilitiesOut.of(row)


@router.get(
    "/mowers/{mower_id}/path",
    responses={404: {"description": "`no-path`: no running job whose route is known."}},
)
async def get_mow_path(
    mower_id: UUID, _: ViewMower, account_id: CurrentAccountId, services: ServicesDep
) -> MowPathOut:
    """The route the mower planned for its running job (local metres of its map) with the
    progress along it, as the Device-Gateway last published it; 404 ``no-path`` without a job
    or before the route arrived. Clients poll it while the mower works."""
    row = await services.queries.mow_path(account_id, mower_id)
    return MowPathOut.of(row)


@router.get("/mowers/{mower_id}/heatmap")
async def get_heatmap(
    mower_id: UUID,
    _: ViewMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
    layer: Annotated[Layer, Query(description="coverage, rtk, wifi or mnet.")],
    start: FromQuery = None,
    end: ToQuery = None,
    cell: Annotated[
        float,
        Query(
            description="Cell size in metres: 0.5, 1 or 2.",
            json_schema_extra={"enum": [0.5, 1, 2]},
        ),
    ] = DEFAULT_CELL_M,
) -> MowerHeatmap:
    """A raster layer over the mower's map frame for a time range (default: the last 7 days):
    cut share and missed areas (``coverage``), share of fixed RTK samples (``rtk``) or median
    signal in dBm (``wifi``, ``mnet``). 404 ``no-map-yet`` before the gateway published a map.
    """
    result = await services.queries.heatmap(account_id, mower_id, layer, start, end, cell)
    return MowerHeatmap.of(mower_id, result)


@router.get("/mowers/{mower_id}/health")
async def get_health(
    mower_id: UUID, _: ViewMower, account_id: CurrentAccountId, services: ServicesDep
) -> MowerHealthOut:
    """Health totals: blade and mowing hours, distance, battery cycles, charge sessions and
    errors since the first state; 404 ``no-telemetry-yet`` when nothing was recorded."""
    row = await services.health.totals(account_id, mower_id)
    return MowerHealthOut.of(row, threshold_hours=services.health.threshold_hours)


@router.get("/mowers/{mower_id}/health/daily")
async def get_health_daily(
    mower_id: UUID,
    _: ViewMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
    first: FromDay = None,
    last: ToDay = None,
) -> HealthDaily:
    """Health totals per UTC day (default: the last 30 days, at most 366)."""
    result = await services.health.daily(account_id, mower_id, first, last)
    return HealthDaily(
        mower_id=mower_id,
        start=result.first,
        end=result.last,
        days=[HealthDayOut.of(d) for d in result.days],
    )


@router.get("/mowers/{mower_id}/health/blade-changes")
async def get_blade_changes(
    mower_id: UUID, _: ViewMower, account_id: CurrentAccountId, services: ServicesDep
) -> BladeChangeList:
    """Recorded blade changes, newest first (at most 100)."""
    changes = await services.health.blade_changes(account_id, mower_id)
    return BladeChangeList(mower_id=mower_id, items=[BladeChangeOut.of(c) for c in changes])


@router.post("/mowers/{mower_id}/health/blade-reset")
async def reset_blade(
    mower_id: UUID,
    principal: ManageMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
    body: BladeResetIn,
) -> MowerHealthOut:
    """Record a blade change: the blade hours since the change restart at zero and the
    threshold reminder is armed again. Works before the first telemetry, too."""
    row = await services.health.reset_blade(account_id, mower_id, principal.subject, body.note)
    return MowerHealthOut.of(row, threshold_hours=services.health.threshold_hours)


@router.put("/mowers/{mower_id}/health/blade-threshold")
async def set_blade_threshold(
    mower_id: UUID,
    _: ManageMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
    body: BladeThresholdIn,
) -> MowerHealthOut:
    """Set the mower's blade reminder threshold in hours (``can_manage_mowers``, like the blade
    reset); ``null`` drops it, so the mower follows its own warning again
    (``device_blade_warn_hours``) when it reports one, else the default
    (``MOWDECK_TELEMETRY_BLADE_HOURS_THRESHOLD``); ``blade_hours_threshold_effective`` names the
    threshold in effect. The reminder is armed again when the blade hours shown lie below the
    new effective threshold. Works before the first telemetry, too. The warning stored on the
    mower itself is not changed."""
    row = await services.health.set_blade_threshold(account_id, mower_id, body.hours)
    return MowerHealthOut.of(row, threshold_hours=services.health.threshold_hours)


@router.get("/mowers/{mower_id}/battery")
async def get_battery(
    mower_id: UUID,
    _: ViewMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
    start: FromQuery = None,
    end: ToQuery = None,
    step: StepQuery = None,
) -> BatterySeries:
    """Battery percentage over time; with ``step`` one point per bin (mean, min, max, charging
    if any). Same ranges and limits as the history."""
    result = await services.health.battery(account_id, mower_id, start, end, step)
    return BatterySeries(
        mower_id=mower_id,
        start=result.start,
        end=result.end,
        step_seconds=result.step.total_seconds() if result.step else None,
        points=[BatteryPointOut.of(p) for p in result.points],
        truncated=result.truncated,
    )


@router.get("/accounts/me/mowers/states")
async def list_states(
    request: Request,
    principal: CurrentPrincipal,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> MowerStateList:
    """Latest state of every mower of the caller's account that the caller may view."""
    visible = await visible_mower_ids(request, principal, account_id)
    rows, truncated = await services.queries.states(account_id, visible)
    return MowerStateList(
        items=[MowerStateSnapshot.of(r, stale=services.queries.is_stale(r)) for r in rows],
        truncated=truncated,
    )


@router.get("/accounts/me/mowers/capabilities")
async def list_capabilities(
    request: Request,
    principal: CurrentPrincipal,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> MowerCapabilitiesList:
    """Capabilities of every mower of the caller's account that the caller may view and whose
    gateway has reported them (same bound as the states list)."""
    visible = await visible_mower_ids(request, principal, account_id)
    rows, truncated = await services.queries.capabilities(account_id, visible)
    return MowerCapabilitiesList(
        items=[MowerCapabilitiesOut.of(r) for r in rows], truncated=truncated
    )


@router.get("/reports/summary")
async def get_report_summary(
    request: Request,
    principal: CurrentPrincipal,
    account_id: CurrentAccountId,
    services: ServicesDep,
    first: FromDay = None,
    last: ToDay = None,
    mower_id: Annotated[
        list[UUID] | None,
        Query(description="Only these mowers (repeatable); invisible ones are left out silently."),
    ] = None,
) -> ReportSummary:
    """Day totals (UTC days, default the last 30, at most 366) of every mower of the caller's
    account that the caller may view, in one call: blade and mowing hours, distance, jobs,
    online hours, battery cycles, charge sessions and errors by code. Mowers without data in
    the range are absent; Telemetry does not know sites, so clients filter by Fleet's
    ``site_id`` (or pass ``mower_id``)."""
    visible = await visible_mower_ids(request, principal, account_id)
    wanted: set[UUID] | None = visible
    if mower_id:
        wanted = set(mower_id) if visible is None else visible & set(mower_id)
    result = await services.health.report(account_id, wanted, first, last)
    return ReportSummary.of(result.first, result.last, result.rows, result.truncated)


@router.get("/mowers/{mower_id}/aerial")
async def get_aerial(
    mower_id: UUID, _: ViewMower, account_id: CurrentAccountId, services: ServicesDep
) -> AerialOut:
    """Whether an aerial photo can be laid under the mower's map, with its credit line, zoom
    range and the box where tiles are served; `enabled: false` without a configured source.
    404 `no-map-yet` before the gateway published a map."""
    info = await services.aerial.info(account_id, mower_id)
    return AerialOut.of(
        mower_id, info, f"/api/v1/telemetry/mowers/{mower_id}/aerial/{{z}}/{{x}}/{{y}}"
    )


@router.get(
    "/mowers/{mower_id}/aerial/{z}/{x}/{y}",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}, "image/png": {}}}},
)
async def get_aerial_tile(
    mower_id: UUID,
    z: Annotated[int, Path(ge=0, le=24)],
    x: Annotated[int, Path(ge=0)],
    y: Annotated[int, Path(ge=0)],
    _: ViewMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> Response:
    """One aerial photo tile (Web Mercator, XYZ) around the mower's map. Tiles away from the
    map or outside the zoom range are 404 `tile-outside-map`, so this is no open proxy; new
    tiles are rate limited per account (429)."""
    tile = await services.aerial.tile(account_id, mower_id, z, x, y)
    return Response(
        tile.body,
        media_type=tile.media_type,
        headers={
            "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )
