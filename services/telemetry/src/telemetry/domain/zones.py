"""Virtual zones (ADR 0070): polygons the owner draws for a mower whose vendor shares no map.

Everything here is pure: the validation of a drawn ring, the point-in-zone test in a local
metric frame, the choice of the current zone with a hysteresis that depends on the accuracy of
the fix, and the split of the time between two reports into per zone and per UTC day amounts.

Rings are WGS84 ``(lat, lon)`` pairs, stored open (the first corner is not repeated). Planar work
happens in a local tangent plane around the ring's vertex mean (the conversion of
``mowdeck_control.geo``, accurate to millimetres over a few hundred metres).
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from mowdeck_contracts.v1 import GeoReference
from mowdeck_control.geo import boundary_distance, from_wgs84, point_in_polygon

from telemetry.domain.errors import InvalidZoneError

LatLon = tuple[float, float]
"""A WGS84 point as ``(lat, lon)`` in degrees."""

MIN_CORNERS = 3
MAX_CORNERS = 64
"""Corners of one zone (enough for a curved bed drawn by hand, little enough to check fast)."""
MAX_ZONES = 32
"""Zones of one mower."""
MIN_AREA_M2 = 1.0
MAX_AREA_M2 = 100_000.0
"""Ten hectares: larger than any garden a robot mower covers."""
MAX_EXTENT_M = 1_000.0
"""Longest side of the bounding box of one zone."""
MAX_ANCHOR_DISTANCE_M = 1_000.0
"""Every corner lies within this distance of the mower's map origin or last position."""
MAX_NAME_LENGTH = 60
START_PLAN_REF_MAX = 100
"""Longest start plan reference (the contract's pattern allows 97 characters)."""
MIN_CORNER_GAP_M = 0.05
"""Corners closer than this are one corner (a double tap)."""

MARGIN_RTK_FIXED_M = 0.3
MARGIN_RTK_FLOAT_M = 1.0
MARGIN_DOCK_M = 0.5
MARGIN_PLAIN_M = 3.0
"""Hysteresis by fix quality: how far a position must lie beyond a border before the zone
changes. Plain GNSS scatters by metres; an RTK fix by centimetres."""


@dataclass(frozen=True, slots=True)
class VirtualZone:
    """One zone as stored."""

    id: UUID
    account_id: UUID
    mower_id: UUID
    name: str
    ring: tuple[LatLon, ...]
    area_m2: float
    created_at: datetime
    updated_at: datetime
    start_plan_ref: str | None = None
    """A start plan of kind ``zone`` the mower's connection declares (ADR 0066): starting
    "this zone" sends that plan. Optional; never set for a mower without such plans."""
    created_by: UUID | None = None


@dataclass(frozen=True, slots=True)
class ZoneShape:
    """A zone prepared for the point test: its ring in a local frame and a bounding box."""

    zone_id: UUID
    area_m2: float
    frame: GeoReference
    local: tuple[tuple[float, float], ...]
    box: tuple[float, float, float, float]
    """``(min_x, min_y, max_x, max_y)`` in local metres."""

    def locate(self, lat: float, lon: float) -> tuple[bool, float]:
        """Whether the point lies inside, and its distance to the border in metres."""
        x, y = from_wgs84(self.frame, lat, lon)
        min_x, min_y, max_x, max_y = self.box
        if x < min_x or x > max_x or y < min_y or y > max_y:
            # Outside the box: the border is at least as far as the box.
            dx = max(min_x - x, 0.0, x - max_x)
            dy = max(min_y - y, 0.0, y - max_y)
            near = math.hypot(dx, dy)
            if near > MAX_EXTENT_M:  # far away: the exact distance does not matter
                return False, near
            return False, boundary_distance((x, y), self.local)
        return point_in_polygon((x, y), self.local), boundary_distance((x, y), self.local)


def distance_m(a: LatLon, b: LatLon) -> float:
    """Distance between two WGS84 points in metres (local tangent plane, short distances)."""
    x, y = from_wgs84(GeoReference(origin_lat=a[0], origin_lon=a[1]), b[0], b[1])
    return math.hypot(x, y)


def _frame(ring: Sequence[LatLon]) -> GeoReference:
    lat = sum(p[0] for p in ring) / len(ring)
    lon = sum(p[1] for p in ring) / len(ring)
    return GeoReference(origin_lat=lat, origin_lon=lon)


def _local(frame: GeoReference, ring: Sequence[LatLon]) -> list[tuple[float, float]]:
    return [from_wgs84(frame, lat, lon) for lat, lon in ring]


def shoelace(points: Sequence[tuple[float, float]]) -> float:
    """Signed area of a planar ring (positive counter-clockwise)."""
    total = 0.0
    for i, (x1, y1) in enumerate(points):
        x2, y2 = points[(i + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _orientation(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a: tuple[float, float], b: tuple[float, float], p: tuple[float, float]) -> bool:
    return min(a[0], b[0]) - 1e-9 <= p[0] <= max(a[0], b[0]) + 1e-9 and (
        min(a[1], b[1]) - 1e-9 <= p[1] <= max(a[1], b[1]) + 1e-9
    )


def segments_cross(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    """Whether the closed segments ``a-b`` and ``c-d`` share a point (touching counts)."""
    o1, o2 = _orientation(a, b, c), _orientation(a, b, d)
    o3, o4 = _orientation(c, d, a), _orientation(c, d, b)
    eps = 1e-9
    if ((o1 > eps and o2 < -eps) or (o1 < -eps and o2 > eps)) and (
        (o3 > eps and o4 < -eps) or (o3 < -eps and o4 > eps)
    ):
        return True
    return (
        (abs(o1) <= eps and _on_segment(a, b, c))
        or (abs(o2) <= eps and _on_segment(a, b, d))
        or (abs(o3) <= eps and _on_segment(c, d, a))
        or (abs(o4) <= eps and _on_segment(c, d, b))
    )


def self_intersects(points: Sequence[tuple[float, float]]) -> bool:
    """Whether two edges of the closed ring that are not neighbours touch or cross."""
    n = len(points)
    for i in range(n):
        a, b = points[i], points[(i + 1) % n]
        for j in range(i + 1, n):
            if j == i or (j + 1) % n == i or j == (i + 1) % n:
                continue  # neighbouring edges share a corner by construction
            if segments_cross(a, b, points[j], points[(j + 1) % n]):
                return True
    return False


def clean_name(name: str) -> str:
    """The trimmed name; raises :class:`InvalidZoneError` when empty or too long."""
    cleaned = " ".join(name.split())
    if not cleaned:
        raise InvalidZoneError("name", "the zone needs a name")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise InvalidZoneError("name", f"the name has more than {MAX_NAME_LENGTH} characters")
    return cleaned


def normalize_ring(points: Iterable[Sequence[float]]) -> tuple[tuple[LatLon, ...], float]:
    """A drawn ring made valid for storage: ``(ring, area_m2)``.

    Accepts the ring open or closed; drops repeated corners (closer than 5 cm). Raises
    :class:`InvalidZoneError` (field ``ring``) for fewer than 3 or more than 64 corners, a
    coordinate out of range, a zone wider than 1 km, crossing or touching edges, or an area
    outside 1 m² to 10 ha.
    """
    ring: list[LatLon] = []
    for point in points:
        if len(point) != 2:
            raise InvalidZoneError("ring", "every corner is a [lat, lon] pair")
        lat, lon = float(point[0]), float(point[1])
        if not (math.isfinite(lat) and math.isfinite(lon)):
            raise InvalidZoneError("ring", "a corner is not a number")
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise InvalidZoneError("ring", "a corner lies outside the valid latitude/longitude")
        ring.append((lat, lon))
        if len(ring) > MAX_CORNERS + 1:
            raise InvalidZoneError("ring", f"a zone has at most {MAX_CORNERS} corners")
    if len(ring) < MIN_CORNERS:
        raise InvalidZoneError("ring", f"a zone needs at least {MIN_CORNERS} corners")
    frame = _frame(ring)
    local = _local(frame, ring)
    kept: list[int] = []
    for i, p in enumerate(local):
        if kept and math.dist(local[kept[-1]], p) < MIN_CORNER_GAP_M:
            continue
        kept.append(i)
    if len(kept) > 1 and math.dist(local[kept[0]], local[kept[-1]]) < MIN_CORNER_GAP_M:
        kept.pop()  # the closing corner of a closed ring
    ring = [ring[i] for i in kept]
    if len(ring) < MIN_CORNERS:
        raise InvalidZoneError("ring", f"a zone needs at least {MIN_CORNERS} distinct corners")
    if len(ring) > MAX_CORNERS:
        raise InvalidZoneError("ring", f"a zone has at most {MAX_CORNERS} corners")
    frame = _frame(ring)
    local = _local(frame, ring)
    xs = [p[0] for p in local]
    ys = [p[1] for p in local]
    if max(max(xs) - min(xs), max(ys) - min(ys)) > MAX_EXTENT_M:
        raise InvalidZoneError("ring", f"a zone is at most {MAX_EXTENT_M:.0f} m wide")
    if self_intersects(local):
        raise InvalidZoneError("ring", "the edges of the zone cross or touch each other")
    area = abs(shoelace(local))
    if area < MIN_AREA_M2:
        raise InvalidZoneError("ring", f"a zone covers at least {MIN_AREA_M2:.0f} m²")
    if area > MAX_AREA_M2:
        raise InvalidZoneError("ring", f"a zone covers at most {MAX_AREA_M2:.0f} m²")
    return tuple(ring), round(area, 2)


def shape_of(zone: VirtualZone) -> ZoneShape:
    """Prepare a stored zone for the point test."""
    frame = _frame(zone.ring)
    local = tuple(_local(frame, zone.ring))
    xs = [p[0] for p in local]
    ys = [p[1] for p in local]
    return ZoneShape(
        zone_id=zone.id,
        area_m2=zone.area_m2,
        frame=frame,
        local=local,
        box=(min(xs), min(ys), max(xs), max(ys)),
    )


def margin_for(rtk_fix: str, position_source: str) -> float:
    """The hysteresis band (metres) for a fix of this quality."""
    if position_source == "dock":
        return MARGIN_DOCK_M
    if rtk_fix == "fixed":
        return MARGIN_RTK_FIXED_M
    if rtk_fix == "float":
        return MARGIN_RTK_FLOAT_M
    return MARGIN_PLAIN_M


def choose_zone(
    current: UUID | None, shapes: Sequence[ZoneShape], lat: float, lon: float, margin_m: float
) -> UUID | None:
    """The zone the mower is in after a fix at ``(lat, lon)``, given the zone it was in.

    * A zone is entered only when the fix lies inside it by at least ``margin_m`` ("deep"), or
      inside it while the mower is in no zone that still holds it.
    * The current zone holds the mower while the fix is inside it or less than ``margin_m``
      outside, unless a smaller zone (nested inside, e.g. a bed within the lawn) has the fix
      deep inside.
    * Of several candidates the smallest wins (the most specific zone).

    So a mower on a border never flaps: to change, the fix has to move ``margin_m`` beyond the
    border, and back again ``margin_m`` beyond it the other way.
    """
    located = [(shape, *shape.locate(lat, lon)) for shape in shapes]
    deep = [s for s, inside, edge in located if inside and edge >= margin_m]
    best_deep = min(deep, key=lambda s: s.area_m2) if deep else None
    held = next(
        (s for s, inside, edge in located if s.zone_id == current and (inside or edge < margin_m)),
        None,
    )
    if held is not None:
        if best_deep is not None and best_deep.area_m2 < held.area_m2:
            return best_deep.zone_id
        return held.zone_id
    if best_deep is not None:
        return best_deep.zone_id
    inside_any = [s for s, inside, _ in located if inside]
    return min(inside_any, key=lambda s: s.area_m2).zone_id if inside_any else None


@dataclass(frozen=True, slots=True)
class Presence:
    """Where a mower is, as the zone evaluation left it (one row per mower)."""

    mower_id: UUID
    account_id: UUID
    zone_id: UUID | None
    since: datetime
    """When the mower entered ``zone_id`` (or left the last zone)."""
    observed_at: datetime
    """The newest report evaluated (``observed_at`` of the state)."""
    seen_at: datetime | None
    """Until when time was counted; None after a link loss (nothing counts across it)."""
    mowing: bool = False
    """The mower mowed at the last report (its time until the next counts as mowing time)."""
    lat: float | None = None
    lon: float | None = None
    rtk_fix: str = "unknown"


@dataclass(frozen=True, slots=True)
class ZoneTime:
    """Time to add to one zone on one UTC day."""

    zone_id: UUID
    day: date
    seconds: float
    mowing_seconds: float
    entries: int = 0


@dataclass(frozen=True, slots=True)
class ZoneDay:
    """The totals of one zone on one UTC day."""

    mower_id: UUID
    account_id: UUID
    zone_id: UUID
    day: date
    seconds: float = 0.0
    mowing_seconds: float = 0.0
    entries: int = 0

    def plus(self, add: ZoneTime) -> "ZoneDay":
        """These totals with ``add`` counted."""
        return ZoneDay(
            mower_id=self.mower_id,
            account_id=self.account_id,
            zone_id=self.zone_id,
            day=self.day,
            seconds=self.seconds + add.seconds,
            mowing_seconds=self.mowing_seconds + add.mowing_seconds,
            entries=self.entries + add.entries,
        )


def split_by_day(start: datetime, end: datetime) -> list[tuple[date, float]]:
    """``[start, end)`` as seconds per UTC day."""
    out: list[tuple[date, float]] = []
    cursor = start.astimezone(UTC)
    stop = end.astimezone(UTC)
    while cursor < stop:
        midnight = datetime.combine(cursor.date() + timedelta(days=1), datetime.min.time(), UTC)
        piece_end = min(midnight, stop)
        out.append((cursor.date(), (piece_end - cursor).total_seconds()))
        cursor = piece_end
    return out


@dataclass(frozen=True, slots=True)
class Observation:
    """What one state tells the zone evaluation."""

    observed_at: datetime
    seen_at: datetime
    """The moment the link was known alive (``heartbeat_at``, else ``observed_at``)."""
    connected: bool
    mowing: bool
    lat: float | None = None
    lon: float | None = None
    rtk_fix: str = "unknown"
    position_source: str = "unknown"


@dataclass(frozen=True, slots=True)
class Step:
    """The outcome of one observation."""

    presence: Presence
    times: list[ZoneTime] = field(default_factory=list)
    changed: bool = False
    """The current zone changed (entered, left, or moved to another one)."""


def advance(
    previous: Presence | None,
    seen: Observation,
    shapes: Sequence[ZoneShape],
    *,
    mower_id: UUID,
    account_id: UUID,
    max_gap: timedelta,
) -> Step | None:
    """Apply one observation; None when it is older than what was already evaluated.

    Time between the previous ``seen_at`` and this one counts for the zone the mower was in (as
    mowing time when it mowed at the previous report), split by UTC day, unless the gap exceeds
    ``max_gap`` or the link was lost in between. A new position (newer ``observed_at``) is then
    evaluated with :func:`choose_zone`; a heartbeat (same report again) keeps the zone.
    """
    if previous is not None and (
        seen.observed_at < previous.observed_at
        or (previous.seen_at is not None and seen.seen_at < previous.seen_at)
    ):
        return None
    times: list[ZoneTime] = []
    known = {shape.zone_id for shape in shapes}
    current = previous.zone_id if previous is not None and previous.zone_id in known else None
    if (
        previous is not None
        and previous.seen_at is not None
        and current is not None
        and seen.connected
        and timedelta(0) < seen.seen_at - previous.seen_at <= max_gap
    ):
        for day, secs in split_by_day(previous.seen_at, seen.seen_at):
            times.append(ZoneTime(current, day, secs, secs if previous.mowing else 0.0))

    new_position = (
        seen.lat is not None
        and seen.lon is not None
        and (previous is None or seen.observed_at > previous.observed_at or previous.lat is None)
    )
    zone = current
    lat, lon, fix = (
        (previous.lat, previous.lon, previous.rtk_fix) if previous is not None else (None, None, "")
    )
    if new_position and seen.lat is not None and seen.lon is not None:
        margin = margin_for(seen.rtk_fix, seen.position_source)
        zone = choose_zone(current, shapes, seen.lat, seen.lon, margin)
        lat, lon, fix = seen.lat, seen.lon, seen.rtk_fix
    changed = previous is None or zone != (previous.zone_id if previous else None)
    entered_at = seen.observed_at if new_position else seen.seen_at
    if zone is not None and changed:
        times.append(ZoneTime(zone, entered_at.astimezone(UTC).date(), 0.0, 0.0, entries=1))
    since = entered_at if changed or previous is None else previous.since
    presence = Presence(
        mower_id=mower_id,
        account_id=account_id,
        zone_id=zone,
        since=since,
        observed_at=max(seen.observed_at, previous.observed_at) if previous else seen.observed_at,
        seen_at=seen.seen_at if seen.connected else None,
        mowing=seen.mowing,
        lat=lat,
        lon=lon,
        rtk_fix=fix or "unknown",
    )
    return Step(presence=presence, times=times, changed=changed)


def reevaluate(
    previous: Presence | None, shapes: Sequence[ZoneShape], *, now: datetime
) -> Presence | None:
    """The presence after the zones changed (drawn, moved, deleted), from the last position.

    No time is counted; a changed zone starts at ``now``. None without a known position.
    """
    if previous is None or previous.lat is None or previous.lon is None:
        return None
    known = {shape.zone_id for shape in shapes}
    current = previous.zone_id if previous.zone_id in known else None
    zone = choose_zone(
        current, shapes, previous.lat, previous.lon, margin_for(previous.rtk_fix, "unknown")
    )
    if zone == previous.zone_id:
        return previous
    return Presence(
        mower_id=previous.mower_id,
        account_id=previous.account_id,
        zone_id=zone,
        since=now,
        observed_at=previous.observed_at,
        seen_at=previous.seen_at,
        mowing=previous.mowing,
        lat=previous.lat,
        lon=previous.lon,
        rtk_fix=previous.rtk_fix,
    )
