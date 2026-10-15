"""Input and output of every pattern: :class:`Area`, :class:`MowerSpec`, :class:`MowPlan`.

All geometry is ``(x, y)`` in metres in the mower's local map frame (``mowdeck_contracts.v1``
``MowerMap``). A plan is neutral: an ordered list of :class:`Segment` objects (drive this
polyline with the blades on or off at this speed), independent of the mower model.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from mowdeck_contracts.v1 import MowerMap, SpotMowPlan, SpotNativeJob, SpotSegment
from shapely import make_valid
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

Point = tuple[float, float]
"""A point ``(x, y)`` in metres."""
Ring = tuple[Point, ...]
"""A closed ring of at least three points; the closing edge is implicit."""
SegmentKind = Literal["mow", "turn", "transfer"]
"""``mow``: a cutting run; ``turn``: the connection between two runs inside the area;
``transfer``: driving to the next part of the plan, always with the blades off."""

MAX_VERTICES = 5_000
"""Most vertices an area (boundary, holes and zones together) may have."""
MAX_AREA_M2 = 50_000.0
"""Largest area one plan may cover (5 ha)."""
MAX_EXTENT_M = 1_000.0
"""Largest width or height of an area."""


class PatternError(ValueError):
    """Base class of the errors of this package."""


class InvalidArea(PatternError):  # noqa: N818 - reads as the finding
    """The area is not a usable polygon (too small, too large, self-intersecting, ...)."""


class InvalidSettings(PatternError):  # noqa: N818 - reads as the finding
    """Settings do not match the pattern's JSON Schema."""

    def __init__(self, message: str, errors: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.errors = tuple(errors)


class PlanningError(PatternError):
    """The pattern cannot produce a route for this area (for example a split area)."""


class PlanRejected(PatternError):  # noqa: N818 - reads as the finding
    """The validation chain refused a plan; ``reasons`` lists every finding."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__("plan rejected: " + "; ".join(self.reasons))


def _ring(points: Iterable[Sequence[float]], what: str) -> Ring:
    ring = tuple((float(p[0]), float(p[1])) for p in points)
    if len(ring) >= 2 and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 3:
        msg = f"{what} needs at least three points"
        raise InvalidArea(msg)
    if not all(math.isfinite(c) for p in ring for c in p):
        msg = f"{what} has coordinates that are not finite"
        raise InvalidArea(msg)
    return ring


def polygons_of(geometry: BaseGeometry) -> list[Polygon]:
    """The non-empty polygons of ``geometry`` (a polygon, a multipolygon or a collection)."""
    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return [p for p in geometry.geoms if not p.is_empty]
    found: list[Polygon] = []
    for part in getattr(geometry, "geoms", ()):
        found.extend(polygons_of(part))
    return found


def polygonal(geometry: BaseGeometry) -> Polygon | MultiPolygon:
    """``geometry`` reduced to its polygons (empty polygon if there are none)."""
    parts = polygons_of(geometry)
    if not parts:
        return Polygon()
    if len(parts) == 1:
        return parts[0]
    return MultiPolygon(parts)


@dataclass(frozen=True, slots=True)
class Area:
    """What to mow: a boundary with holes (beds), no-go zones and obstacles, in local metres.

    Holes, no-go zones and obstacles are all left out; they differ only in where they come from
    (a cut-out drawn with the spot polygon, or zones of the mower's map).
    """

    boundary: Ring
    holes: tuple[Ring, ...] = ()
    no_go_zones: tuple[Ring, ...] = ()
    obstacles: tuple[Ring, ...] = ()

    def __post_init__(self) -> None:
        vertices = len(self.boundary) + sum(
            len(r) for group in (self.holes, self.no_go_zones, self.obstacles) for r in group
        )
        if vertices > MAX_VERTICES:
            msg = f"the area has {vertices} vertices, at most {MAX_VERTICES} are allowed"
            raise InvalidArea(msg)
        outer = Polygon(self.boundary)
        minx, miny, maxx, maxy = outer.bounds
        if max(maxx - minx, maxy - miny) > MAX_EXTENT_M:
            msg = f"the area is wider than {MAX_EXTENT_M:.0f} m"
            raise InvalidArea(msg)
        region = self.region()
        if region.area > MAX_AREA_M2:
            msg = f"the area covers {region.area:.0f} m², at most {MAX_AREA_M2:.0f} m² are allowed"
            raise InvalidArea(msg)
        if region.area < 0.01:
            msg = "the area is empty after removing holes and zones"
            raise InvalidArea(msg)

    @classmethod
    def of(
        cls,
        boundary: Iterable[Sequence[float]],
        *,
        holes: Iterable[Iterable[Sequence[float]]] = (),
        no_go_zones: Iterable[Iterable[Sequence[float]]] = (),
        obstacles: Iterable[Iterable[Sequence[float]]] = (),
    ) -> "Area":
        """Build an area from plain coordinate lists (a closing point is dropped)."""
        return cls(
            boundary=_ring(boundary, "the boundary"),
            holes=tuple(_ring(h, "a hole") for h in holes),
            no_go_zones=tuple(_ring(z, "a no-go zone") for z in no_go_zones),
            obstacles=tuple(_ring(o, "an obstacle") for o in obstacles),
        )

    @classmethod
    def from_map(
        cls,
        mower_map: MowerMap,
        area_id: str,
        *,
        boundary: Iterable[Sequence[float]] | None = None,
        holes: Iterable[Iterable[Sequence[float]]] = (),
    ) -> "Area":
        """The area ``area_id`` of a mower map with the map's no-go zones and obstacles.

        ``boundary`` replaces the area's own boundary (a spot drawn inside it); zones that do
        not touch the boundary are left out.
        """
        map_area = mower_map.area(area_id)
        if map_area is None:
            msg = f"unknown area id {area_id!r}"
            raise InvalidArea(msg)
        outline = _ring(boundary if boundary is not None else map_area.boundary, "the boundary")
        outer = Polygon(outline).buffer(0)

        def touching(zones: Iterable[Sequence[Sequence[float]]]) -> tuple[Ring, ...]:
            rings = (_ring(z, "a zone") for z in zones)
            return tuple(r for r in rings if Polygon(r).buffer(0).intersects(outer))

        return cls(
            boundary=outline,
            holes=tuple(_ring(h, "a hole") for h in holes),
            no_go_zones=touching(z.boundary for z in mower_map.no_go_zones),
            obstacles=touching(o.boundary for o in mower_map.obstacles),
        )

    def outline(self) -> Polygon | MultiPolygon:
        """The boundary minus the holes (valid geometry)."""
        shell = polygonal(make_valid(Polygon(self.boundary)))
        cut = [polygonal(make_valid(Polygon(h))) for h in self.holes]
        if cut:
            shell = polygonal(shell.difference(unary_union(cut)))
        return shell

    def forbidden(self) -> Polygon | MultiPolygon:
        """No-go zones and obstacles together."""
        zones = [polygonal(make_valid(Polygon(z))) for z in (*self.no_go_zones, *self.obstacles)]
        return polygonal(unary_union(zones)) if zones else Polygon()

    def region(self) -> Polygon | MultiPolygon:
        """What may be mowed: the outline minus every zone."""
        forbidden = self.forbidden()
        outline = self.outline()
        return polygonal(outline.difference(forbidden)) if not forbidden.is_empty else outline


@dataclass(frozen=True, slots=True)
class MowerSpec:
    """The mower data a pattern needs. Lengths in metres, speeds in metres per second.

    ``turning_radius_m`` is 0 for mowers that turn on the spot (differential drive such as the
    LUBA); the validation chain then accepts any heading change.
    """

    cutting_width_m: float = 0.4
    overlap_m: float = 0.05
    turning_radius_m: float = 0.0
    max_speed_mps: float = 0.4
    transfer_speed_mps: float = 0.6
    safety_margin_m: float = 0.15
    turn_time_s: float = 2.0
    """Time added per sharp turn (heading change above 60 degrees) in the duration estimate."""

    def __post_init__(self) -> None:
        checks = {
            "cutting_width_m must be between 0.05 and 2": 0.05 <= self.cutting_width_m <= 2,
            "overlap_m must be at least 0 and below the cutting width": 0
            <= self.overlap_m
            < self.cutting_width_m,
            "turning_radius_m must be between 0 and 10": 0 <= self.turning_radius_m <= 10,
            "max_speed_mps must be above 0 and at most 2": 0 < self.max_speed_mps <= 2,
            "transfer_speed_mps must be above 0 and at most 2": 0 < self.transfer_speed_mps <= 2,
            "safety_margin_m must be between 0 and 2": 0 <= self.safety_margin_m <= 2,
            "turn_time_s must be between 0 and 60": 0 <= self.turn_time_s <= 60,
        }
        failed = [message for message, ok in checks.items() if not ok]
        if failed:
            raise PatternError("; ".join(failed))

    @property
    def spacing_m(self) -> float:
        """Distance between neighbouring runs: cutting width minus overlap."""
        return self.cutting_width_m - self.overlap_m

    @property
    def inset_m(self) -> float:
        """How far the mower's centre stays from the edge while cutting: half the cutting width
        (the blade reaches the edge), at least the safety margin."""
        return max(self.safety_margin_m, self.cutting_width_m / 2)


@dataclass(frozen=True, slots=True)
class Segment:
    """Drive along ``polyline`` with the blades on or off at ``speed`` m/s (backwards with
    ``reverse``: part of a three-point turn, always with the blades off)."""

    polyline: tuple[Point, ...]
    blades_on: bool
    speed: float
    kind: SegmentKind = "mow"
    reverse: bool = False

    def __post_init__(self) -> None:
        if len(self.polyline) < 2:
            msg = "a segment needs at least two points"
            raise PatternError(msg)
        if not self.speed > 0:
            msg = "a segment needs a positive speed"
            raise PatternError(msg)

    @property
    def length(self) -> float:
        """Length in metres."""
        return sum(math.dist(a, b) for a, b in zip(self.polyline, self.polyline[1:], strict=False))

    @property
    def start(self) -> Point:
        """First point."""
        return self.polyline[0]

    @property
    def end(self) -> Point:
        """Last point."""
        return self.polyline[-1]


@dataclass(frozen=True, slots=True)
class MowPlan:
    """A neutral mowing plan: ordered segments plus estimates."""

    segments: tuple[Segment, ...]
    estimated_duration_s: float
    covered_area_m2: float
    pattern_chain: tuple[str, ...]
    area_m2: float = 0.0
    warnings: tuple[str, ...] = field(default=())
    turn_style: str | None = None
    """The turn style the lane ends were shaped with (ADR 0043); None: straight connections."""

    @property
    def mow_length_m(self) -> float:
        """Length driven with the blades on."""
        return sum(s.length for s in self.segments if s.blades_on)

    @property
    def transfer_length_m(self) -> float:
        """Length driven with the blades off."""
        return sum(s.length for s in self.segments if not s.blades_on)

    @property
    def coverage_ratio(self) -> float:
        """Covered share of the mowable area (0 when the area is unknown)."""
        return self.covered_area_m2 / self.area_m2 if self.area_m2 > 0 else 0.0

    def to_spot_plan(self, native: SpotNativeJob | None = None) -> SpotMowPlan:
        """The contract form for ``StartMow.spot_plan`` (centimetre precision)."""
        segments: list[SpotSegment] = []
        for segment in self.segments:
            points = _dedupe([(round(x, 2), round(y, 2)) for x, y in segment.polyline])
            if len(points) < 2:
                continue
            segments.append(
                SpotSegment.from_points(
                    points,
                    blades_on=segment.blades_on,
                    speed_mps=min(segment.speed, 2.0),
                    kind=segment.kind,
                    reverse=segment.reverse,
                )
            )
        return SpotMowPlan.build(
            segments,
            self.pattern_chain,
            estimated_duration_s=self.estimated_duration_s,
            covered_area_m2=self.covered_area_m2,
            native=native,
        )

    @classmethod
    def from_spot_plan(cls, plan: SpotMowPlan) -> "MowPlan":
        """Rebuild a plan from its contract form (estimates are taken over)."""
        return cls(
            segments=tuple(
                Segment(
                    polyline=tuple(s.points()),
                    blades_on=s.blades_on,
                    speed=s.speed_mps,
                    kind=s.kind,
                    reverse=s.reverse,
                )
                for s in plan.segments
            ),
            estimated_duration_s=plan.estimated_duration_s,
            covered_area_m2=plan.covered_area_m2,
            pattern_chain=tuple(plan.pattern_chain),
        )


def _dedupe(points: Sequence[Point]) -> list[Point]:
    out: list[Point] = []
    for point in points:
        if not out or out[-1] != point:
            out.append(point)
    return out
