"""Routes of the virtual zones under ``/api/v1/telemetry`` (ADR 0070).

Reading the zones, the current zone and the time per zone needs ``can_view`` on the mower;
drawing, changing and deleting zones needs ``can_control`` (like renaming a map area): 404 for
foreign mowers, 403 when visible without it. Zones belong to the mower and so to its account
and site like every other mower record; no new relation exists in the authorization model.
"""

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from mowdeck_service_kit import CurrentAccountId
from mowdeck_service_kit.errors import NotFound
from pydantic import BaseModel, Field

from telemetry.adapters.api.deps import ControlMower, ServicesDep, ViewMower
from telemetry.application.zones import ZoneOverview, Zones, ZoneTotals
from telemetry.domain.zones import (
    MAX_ANCHOR_DISTANCE_M,
    MAX_AREA_M2,
    MAX_CORNERS,
    MAX_EXTENT_M,
    MAX_NAME_LENGTH,
    MAX_ZONES,
    MIN_AREA_M2,
    MIN_CORNERS,
    START_PLAN_REF_MAX,
    Presence,
    VirtualZone,
)

router = APIRouter(prefix="/telemetry", tags=["zones"])

ZONE_RESPONSES: dict[int | str, dict[str, str]] = {
    403: {"description": "The caller sees the mower but has no `can_control`."},
    404: {"description": "The mower (or zone) does not exist or is not visible to the caller."},
    409: {
        "description": "Zones are not offered for this mower (`vendor_map`, `no_position`) or it "
        "has the most zones (`limit`); the error's `code` says which."
    },
    422: {"description": "The ring or the name is not acceptable; the error points at it."},
}


def get_zones(services: ServicesDep) -> Zones:
    """The zone use cases (404 when not wired)."""
    if services.zones is None:
        raise NotFound("virtual zones are not available")
    return services.zones


ZonesDep = Annotated[Zones, Depends(get_zones)]

Ring = Annotated[
    list[Annotated[list[float], Field(min_length=2, max_length=2)]],
    Field(
        min_length=MIN_CORNERS,
        max_length=MAX_CORNERS + 1,
        description="Corners as `[lat, lon]` (WGS84 degrees), open or closed; edges must not "
        "cross.",
    ),
]


class ZoneOut(BaseModel):
    """One virtual zone."""

    id: UUID
    name: str
    ring: list[list[float]] = Field(description="Corners as `[lat, lon]`, open ring.")
    area_m2: float
    start_plan_ref: str | None = Field(
        default=None,
        description="The vendor zone start (`StartPlan` of kind `zone`) this zone is linked "
        "to; send it as `StartMow.plan_ref` to start the mower there.",
    )
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, zone: VirtualZone) -> "ZoneOut":
        """From a stored zone."""
        return cls(
            id=zone.id,
            name=zone.name,
            ring=[[lat, lon] for lat, lon in zone.ring],
            area_m2=zone.area_m2,
            start_plan_ref=zone.start_plan_ref,
            created_at=zone.created_at,
            updated_at=zone.updated_at,
        )


class PresenceOut(BaseModel):
    """Where the mower is: in a zone (``zone_id``) or in none."""

    zone_id: UUID | None = Field(description="The current zone; null outside every zone.")
    zone_name: str | None = None
    since: datetime = Field(description="When it entered this zone (or left the last one).")
    observed_at: datetime = Field(description="The report the evaluation is based on.")

    @classmethod
    def of(cls, presence: Presence, zones: list[VirtualZone]) -> "PresenceOut":
        """From the stored presence."""
        name = next((z.name for z in zones if z.id == presence.zone_id), None)
        return cls(
            zone_id=presence.zone_id,
            zone_name=name,
            since=presence.since,
            observed_at=presence.observed_at,
        )


class ZonePlanOut(BaseModel):
    """A vendor zone start a zone can be linked to."""

    ref: str
    name: str | None = None


class ZoneLimits(BaseModel):
    """The validation limits, so a client can check a drawing before it is sent."""

    max_zones: int = MAX_ZONES
    min_corners: int = MIN_CORNERS
    max_corners: int = MAX_CORNERS
    min_area_m2: float = MIN_AREA_M2
    max_area_m2: float = MAX_AREA_M2
    max_extent_m: float = MAX_EXTENT_M
    max_anchor_distance_m: float = MAX_ANCHOR_DISTANCE_M
    max_name_length: int = MAX_NAME_LENGTH


class AnchorOut(BaseModel):
    """Where to centre a map that has no vendor geometry."""

    lat: float
    lon: float


class ZonesOut(BaseModel):
    """The zones of a mower and whether the editor is offered."""

    availability: Literal["available", "vendor_map", "no_position"] = Field(
        description="`available`: the vendor shares no map areas and the mower reports "
        "positions; `vendor_map`: its vendor's areas are its zones; `no_position`: it reports "
        "no position."
    )
    zones: list[ZoneOut]
    presence: PresenceOut | None = Field(
        default=None, description="The current zone; null before the first evaluation."
    )
    zone_plans: list[ZonePlanOut] = Field(
        default_factory=list,
        description="Vendor zone starts declared by the mower's connection (ADR 0066); empty "
        "when it declares none, then zones cannot be linked to a start.",
    )
    anchor: AnchorOut | None = None
    limits: ZoneLimits = Field(default_factory=ZoneLimits)

    @classmethod
    def of(cls, overview: ZoneOverview) -> "ZonesOut":
        """From the use case's overview."""
        return cls(
            availability=overview.availability.value,
            zones=[ZoneOut.of(z) for z in overview.zones],
            presence=PresenceOut.of(overview.presence, overview.zones)
            if overview.presence is not None
            else None,
            zone_plans=[ZonePlanOut(ref=p.ref, name=p.name) for p in overview.plans],
            anchor=AnchorOut(lat=overview.anchor[0], lon=overview.anchor[1])
            if overview.anchor is not None
            else None,
        )


class ZoneIn(BaseModel):
    """A new zone."""

    name: str = Field(min_length=1, max_length=200)
    ring: Ring
    start_plan_ref: str | None = Field(default=None, min_length=1, max_length=START_PLAN_REF_MAX)


class ZonePatch(BaseModel):
    """Changes to a zone; fields left out stay as they are."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    ring: Ring | None = None
    start_plan_ref: str | None = Field(
        default=None,
        min_length=1,
        max_length=START_PLAN_REF_MAX,
        description="Link the zone to this vendor zone start.",
    )
    unlink_start_plan: bool = Field(default=False, description="Remove the link to a start.")


class ZoneDayOut(BaseModel):
    """Time in a zone on one UTC day."""

    day: date
    seconds: float
    mowing_seconds: float


class ZoneTotalsOut(BaseModel):
    """Time in one zone over the period."""

    zone_id: UUID
    name: str
    seconds: float = Field(description="Time the mower was in the zone while it reported.")
    mowing_seconds: float = Field(description="Of that, time it mowed.")
    entries: int = Field(description="How often it entered the zone.")
    days: list[ZoneDayOut]

    @classmethod
    def of(cls, totals: ZoneTotals) -> "ZoneTotalsOut":
        """From the use case's totals."""
        return cls(
            zone_id=totals.zone.id,
            name=totals.zone.name,
            seconds=totals.seconds,
            mowing_seconds=totals.mowing_seconds,
            entries=totals.entries,
            days=[ZoneDayOut(day=d, seconds=s, mowing_seconds=m) for d, s, m in totals.days],
        )


class ZoneStatsOut(BaseModel):
    """Time per zone over whole UTC days."""

    first_day: date
    last_day: date
    items: list[ZoneTotalsOut]


@router.get("/mowers/{mower_id}/zones")
async def list_zones(
    mower_id: UUID, _: ViewMower, account_id: CurrentAccountId, zones: ZonesDep
) -> ZonesOut:
    """The mower's virtual zones, the zone it is in and whether the editor is offered."""
    return ZonesOut.of(await zones.overview(account_id, mower_id))


@router.post("/mowers/{mower_id}/zones", status_code=201, responses=ZONE_RESPONSES)
async def create_zone(
    mower_id: UUID,
    body: ZoneIn,
    principal: ControlMower,
    account_id: CurrentAccountId,
    zones: ZonesDep,
) -> ZoneOut:
    """Draw a zone (``can_control``). The current zone is evaluated again at once."""
    zone = await zones.create(
        account_id, mower_id, principal.subject, body.name, body.ring, body.start_plan_ref
    )
    return ZoneOut.of(zone)


@router.patch("/mowers/{mower_id}/zones/{zone_id}", responses=ZONE_RESPONSES)
async def update_zone(
    mower_id: UUID,
    zone_id: UUID,
    body: ZonePatch,
    _: ControlMower,
    account_id: CurrentAccountId,
    zones: ZonesDep,
) -> ZoneOut:
    """Rename, reshape or (un)link a zone (``can_control``)."""
    zone = await zones.update(
        account_id,
        mower_id,
        zone_id,
        name=body.name,
        ring=body.ring,
        start_plan_ref=body.start_plan_ref,
        clear_plan=body.unlink_start_plan,
    )
    return ZoneOut.of(zone)


@router.delete(
    "/mowers/{mower_id}/zones/{zone_id}",
    status_code=204,
    response_class=Response,
    responses=ZONE_RESPONSES,
)
async def delete_zone(
    mower_id: UUID,
    zone_id: UUID,
    _: ControlMower,
    account_id: CurrentAccountId,
    zones: ZonesDep,
) -> Response:
    """Delete a zone with its recorded time (``can_control``)."""
    await zones.delete(account_id, mower_id, zone_id)
    return Response(status_code=204)


@router.get("/mowers/{mower_id}/zones/stats")
async def zone_stats(
    mower_id: UUID,
    _: ViewMower,
    account_id: CurrentAccountId,
    zones: ZonesDep,
    days: Annotated[int, Query(ge=1, le=92, description="Whole UTC days, today included.")] = 7,
) -> ZoneStatsOut:
    """Time per zone (in it, of that mowing, entries) over the last ``days`` days."""
    stats = await zones.stats(account_id, mower_id, days)
    return ZoneStatsOut(
        first_day=stats.first,
        last_day=stats.last,
        items=[ZoneTotalsOut.of(item) for item in stats.items],
    )
