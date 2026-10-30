"""Virtual zones (ADR 0070): the owner's own zones for mowers whose vendor shares no map.

Telemetry owns them because it is the one service that reads positions: the zones are only
ever used against the position stream (which zone the mower is in, how long it stayed), next to
the map snapshot, the capabilities and the aerial photo that decide where they are offered.

* **Offered** (``Availability``) when the mower's map snapshot has no areas and the mower
  reports positions (a current one, or one in the last 30 days). A vendor map with areas wins:
  its areas are what the mower can actually be sent to, two zone systems would contradict each
  other. Zones stay listed if that changes later, but cannot be added.
* **Evaluated** after every stored state (:meth:`Zones.observe`, called by ingestion after its
  commit; a failure here never loses a state): :func:`~telemetry.domain.zones.advance` with the
  hysteresis of the fix quality, serialised per mower by the store.
* **Started** only through the vendor: a zone may name a start plan of kind ``zone`` that the
  mower's connection declares (``MowerCapabilities.start_plans``, ADR 0066); MowDeck never
  claims it can send the mower into a zone otherwise.
"""

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import MowerState
from mowdeck_service_kit import get_logger

from telemetry.application.ports import Clock, Store
from telemetry.application.zone_ports import ZoneStore
from telemetry.domain.errors import InvalidZoneError, NoZoneError, ZonesUnavailableError
from telemetry.domain.rules import blades_on
from telemetry.domain.zones import (
    MAX_ANCHOR_DISTANCE_M,
    MAX_ZONES,
    LatLon,
    Observation,
    Presence,
    Step,
    VirtualZone,
    ZoneShape,
    ZoneTime,
    advance,
    clean_name,
    distance_m,
    normalize_ring,
    reevaluate,
    shape_of,
)

_log = get_logger("telemetry.zones")

POSITION_LOOKBACK = timedelta(days=30)
"""A mower that reported a position this recently counts as one that reports positions."""
MAX_STATS_DAYS = 92


class Availability(StrEnum):
    """Whether the zone editor is offered for a mower, and why not."""

    AVAILABLE = "available"
    VENDOR_MAP = "vendor_map"
    """The vendor shares map areas; those are the mower's zones."""
    NO_POSITION = "no_position"
    """The mower has not reported a position (no GPS, or not connected yet)."""


@dataclass(frozen=True, slots=True)
class ZonePlan:
    """A vendor zone start the mower's connection declares (``StartPlan`` of kind ``zone``)."""

    ref: str
    name: str | None


@dataclass(frozen=True, slots=True)
class ZoneOverview:
    """Everything the zone view needs."""

    availability: Availability
    zones: list[VirtualZone]
    presence: Presence | None
    plans: list[ZonePlan]
    anchor: LatLon | None
    """Where to centre a map without vendor geometry: the map's origin, else the last position,
    else the first zone."""


@dataclass(frozen=True, slots=True)
class ZoneTotals:
    """Time of one zone in a period."""

    zone: VirtualZone
    seconds: float
    mowing_seconds: float
    entries: int
    days: list[tuple[date, float, float]] = field(default_factory=list)
    """``(day, seconds, mowing_seconds)`` for every day with time, oldest first."""


@dataclass(frozen=True, slots=True)
class ZoneStats:
    """Time per zone in ``[first, last]`` (UTC days)."""

    first: date
    last: date
    items: list[ZoneTotals]


def observation_of(state: MowerState) -> Observation:
    """What a state tells the zone evaluation."""
    seen = state.observed_at
    if state.heartbeat_at is not None and state.heartbeat_at > seen:
        seen = state.heartbeat_at
    position = state.position
    return Observation(
        observed_at=state.observed_at,
        seen_at=seen,
        connected=state.connected,
        mowing=blades_on(state),
        lat=position.lat if position is not None else None,
        lon=position.lon if position is not None else None,
        rtk_fix=state.rtk_fix.value,
        position_source=state.position_source.value,
    )


class Zones:
    """The zone use cases."""

    def __init__(
        self,
        zones: ZoneStore,
        store: Store,
        clock: Clock,
        *,
        max_gap: timedelta = timedelta(minutes=10),
    ) -> None:
        self._zones = zones
        self._store = store
        self._clock = clock
        self._max_gap = max_gap
        self._shapes: dict[tuple[UUID, datetime], ZoneShape] = {}

    # --- reading ------------------------------------------------------------------------------

    async def availability(self, account_id: UUID, mower_id: UUID) -> Availability:
        """Whether the editor is offered for the mower (see the module documentation)."""
        snapshot = await self._store.snapshot_of(account_id, mower_id, "map")
        if snapshot is not None and snapshot.body.get("areas"):
            return Availability.VENDOR_MAP
        latest = await self._store.latest_of(account_id, mower_id)
        if latest is not None and latest.state.get("position"):
            return Availability.AVAILABLE
        now = self._clock()
        recent = await self._store.positions(
            account_id, mower_id, now - POSITION_LOOKBACK, now + timedelta(minutes=1), 1
        )
        return Availability.AVAILABLE if recent else Availability.NO_POSITION

    async def overview(self, account_id: UUID, mower_id: UUID) -> ZoneOverview:
        """Availability, zones, the current zone, the vendor zone plans and the map anchor."""
        zones = await self._zones.zones_of(account_id, mower_id)
        presence = await self._zones.presence_of(account_id, mower_id)
        known = {z.id for z in zones}
        if presence is not None and presence.zone_id is not None and presence.zone_id not in known:
            presence = replace(presence, zone_id=None)
        return ZoneOverview(
            availability=await self.availability(account_id, mower_id),
            zones=zones,
            presence=presence,
            plans=await self._plans(account_id, mower_id),
            anchor=await self._anchor(account_id, mower_id, zones),
        )

    async def zone_points(self, account_id: UUID, mower_id: UUID) -> list[LatLon]:
        """Every corner of the mower's zones (the aerial photo covers them too)."""
        return [p for zone in await self._zones.zones_of(account_id, mower_id) for p in zone.ring]

    async def _plans(self, account_id: UUID, mower_id: UUID) -> list[ZonePlan]:
        row = await self._store.snapshot_of(account_id, mower_id, "capabilities")
        plans: list[Any] = row.body.get("start_plans") or [] if row is not None else []
        return [
            ZonePlan(ref=str(p["ref"]), name=p.get("name"))
            for p in plans
            if isinstance(p, dict) and p.get("kind") == "zone" and isinstance(p.get("ref"), str)
        ]

    async def _anchor(
        self, account_id: UUID, mower_id: UUID, zones: list[VirtualZone]
    ) -> LatLon | None:
        snapshot = await self._store.snapshot_of(account_id, mower_id, "map")
        reference = snapshot.body.get("reference") if snapshot is not None else None
        if isinstance(reference, dict):
            lat, lon = reference.get("origin_lat"), reference.get("origin_lon")
            if isinstance(lat, int | float) and isinstance(lon, int | float) and (lat or lon):
                return float(lat), float(lon)
        latest = await self._store.latest_of(account_id, mower_id)
        position = latest.state.get("position") if latest is not None else None
        if isinstance(position, dict):
            return float(position["lat"]), float(position["lon"])
        if zones:
            return zones[0].ring[0]
        return None

    async def stats(self, account_id: UUID, mower_id: UUID, days: int) -> ZoneStats:
        """Time per zone over the last ``days`` UTC days (today included)."""
        days = max(1, min(days, MAX_STATS_DAYS))
        last = self._clock().astimezone(UTC).date()
        first = last - timedelta(days=days - 1)
        zones = await self._zones.zones_of(account_id, mower_id)
        rows = await self._zones.days_of(account_id, mower_id, first, last)
        items: list[ZoneTotals] = []
        for zone in zones:
            mine = [r for r in rows if r.zone_id == zone.id]
            items.append(
                ZoneTotals(
                    zone=zone,
                    seconds=round(sum(r.seconds for r in mine), 1),
                    mowing_seconds=round(sum(r.mowing_seconds for r in mine), 1),
                    entries=sum(r.entries for r in mine),
                    days=[
                        (r.day, round(r.seconds, 1), round(r.mowing_seconds, 1))
                        for r in sorted(mine, key=lambda r: r.day)
                    ],
                )
            )
        return ZoneStats(first=first, last=last, items=items)

    # --- editing ------------------------------------------------------------------------------

    async def create(
        self,
        account_id: UUID,
        mower_id: UUID,
        actor: UUID | None,
        name: str,
        ring: list[list[float]],
        start_plan_ref: str | None = None,
    ) -> VirtualZone:
        """Add a zone; raises :class:`InvalidZoneError` or :class:`ZonesUnavailableError`."""
        available = await self.availability(account_id, mower_id)
        if available is not Availability.AVAILABLE:
            msg = (
                "the mower's vendor shares map areas; zones are drawn only for mowers without"
                if available is Availability.VENDOR_MAP
                else "the mower has not reported a position yet"
            )
            raise ZonesUnavailableError(msg, available.value)
        cleaned = clean_name(name)
        points, area = normalize_ring(ring)
        await self._check_near(account_id, mower_id, points)
        await self._check_plan(account_id, mower_id, start_plan_ref)
        now = self._clock()
        zone = VirtualZone(
            id=uuid.uuid4(),
            account_id=account_id,
            mower_id=mower_id,
            name=cleaned,
            ring=points,
            area_m2=area,
            created_at=now,
            updated_at=now,
            start_plan_ref=start_plan_ref,
            created_by=actor,
        )
        if not await self._zones.add_zone(zone, MAX_ZONES):
            msg = f"a mower has at most {MAX_ZONES} zones"
            raise ZonesUnavailableError(msg, "limit")
        await self.refresh(account_id, mower_id)
        return zone

    async def update(
        self,
        account_id: UUID,
        mower_id: UUID,
        zone_id: UUID,
        *,
        name: str | None = None,
        ring: list[list[float]] | None = None,
        start_plan_ref: str | None = None,
        clear_plan: bool = False,
    ) -> VirtualZone:
        """Rename, reshape or (un)link a zone; raises :class:`NoZoneError` when it is unknown."""
        current = await self._zone(account_id, mower_id, zone_id)
        changes: dict[str, Any] = {"updated_at": self._clock()}
        if name is not None:
            changes["name"] = clean_name(name)
        if ring is not None:
            changes["ring"], changes["area_m2"] = normalize_ring(ring)
            await self._check_near(account_id, mower_id, changes["ring"])
        if clear_plan:
            changes["start_plan_ref"] = None
        elif start_plan_ref is not None:
            await self._check_plan(account_id, mower_id, start_plan_ref)
            changes["start_plan_ref"] = start_plan_ref
        zone = replace(current, **changes)
        if not await self._zones.put_zone(zone):
            raise NoZoneError("the mower has no such zone")
        if ring is not None:
            await self.refresh(account_id, mower_id)
        return zone

    async def delete(self, account_id: UUID, mower_id: UUID, zone_id: UUID) -> None:
        """Delete a zone with its time; raises :class:`NoZoneError` when it is unknown."""
        if not await self._zones.delete_zone(account_id, mower_id, zone_id):
            raise NoZoneError("the mower has no such zone")
        await self.refresh(account_id, mower_id)

    async def _zone(self, account_id: UUID, mower_id: UUID, zone_id: UUID) -> VirtualZone:
        for zone in await self._zones.zones_of(account_id, mower_id):
            if zone.id == zone_id:
                return zone
        raise NoZoneError("the mower has no such zone")

    async def _check_near(self, account_id: UUID, mower_id: UUID, ring: tuple[LatLon, ...]) -> None:
        """Every corner within :data:`MAX_ANCHOR_DISTANCE_M` of the mower's map origin or last
        position: zones belong to the garden (and the aerial photo follows them)."""
        anchor = await self._anchor(account_id, mower_id, [])
        if anchor is None:
            return  # unreachable while the editor is offered (it needs a position)
        if any(distance_m(anchor, corner) > MAX_ANCHOR_DISTANCE_M for corner in ring):
            raise InvalidZoneError(
                "ring",
                f"a zone lies within {MAX_ANCHOR_DISTANCE_M:.0f} m of the mower's garden",
            )

    async def _check_plan(self, account_id: UUID, mower_id: UUID, ref: str | None) -> None:
        if ref is None:
            return
        if ref not in {p.ref for p in await self._plans(account_id, mower_id)}:
            raise InvalidZoneError(
                "start_plan_ref", "the mower's connection declares no zone start with this ref"
            )

    # --- evaluation ---------------------------------------------------------------------------

    def _shapes_of(self, zones: list[VirtualZone]) -> list[ZoneShape]:
        if len(self._shapes) > 4096:
            self._shapes.clear()
        shapes: list[ZoneShape] = []
        for zone in zones:
            key = (zone.id, zone.updated_at)
            shape = self._shapes.get(key)
            if shape is None:
                shape = self._shapes[key] = shape_of(zone)
            shapes.append(shape)
        return shapes

    async def observe(self, state: MowerState) -> Step | None:
        """Evaluate a stored state; None when the mower has no zones or the state is old."""
        zones = [
            z
            for z in await self._zones.zones_for(state.mower_id)
            if z.account_id == state.account_id
        ]
        if not zones:
            return None
        shapes = self._shapes_of(zones)
        async with self._zones.presence_tx(state.mower_id) as tx:
            previous = await tx.presence()
            if previous is not None and previous.account_id != state.account_id:
                previous = None  # the mower moved to another account: start over
            step = advance(
                previous,
                observation_of(state),
                shapes,
                mower_id=state.mower_id,
                account_id=state.account_id,
                max_gap=self._max_gap,
            )
            if step is None:
                return None
            await tx.put_presence(step.presence)
            for time in step.times:
                await tx.add_time(state.mower_id, state.account_id, time)
        if step.changed:
            _log.debug(
                "zone presence changed",
                mower_id=str(state.mower_id),
                zone_id=str(step.presence.zone_id) if step.presence.zone_id else None,
            )
        return step

    async def refresh(self, account_id: UUID, mower_id: UUID) -> Presence | None:
        """Re-evaluate the last position after the zones changed (no time is counted)."""
        zones = await self._zones.zones_of(account_id, mower_id)
        shapes = self._shapes_of(zones)
        now = self._clock()
        async with self._zones.presence_tx(mower_id) as tx:
            stored = await tx.presence()
            previous = stored if stored is not None and stored.account_id == account_id else None
            if previous is None or previous.lat is None:
                previous = await self._from_latest(account_id, mower_id, now)
                if previous is None:
                    return None
            updated = reevaluate(previous, shapes, now=now)
            if updated is None:
                return None
            if updated != stored:
                await tx.put_presence(updated)
                if updated.zone_id is not None and updated.zone_id != (
                    stored.zone_id if stored is not None else None
                ):
                    await tx.add_time(
                        mower_id,
                        account_id,
                        ZoneTime(updated.zone_id, now.astimezone(UTC).date(), 0.0, 0.0, 1),
                    )
            return updated

    async def _from_latest(
        self, account_id: UUID, mower_id: UUID, now: datetime
    ) -> Presence | None:
        latest = await self._store.latest_of(account_id, mower_id)
        position = latest.state.get("position") if latest is not None else None
        if latest is None or not isinstance(position, dict):
            return None
        return Presence(
            mower_id=mower_id,
            account_id=account_id,
            zone_id=None,
            since=now,
            observed_at=latest.observed_at,
            seen_at=None,
            mowing=False,
            lat=float(position["lat"]),
            lon=float(position["lon"]),
            rtk_fix=str(latest.state.get("rtk_fix") or "unknown"),
        )
