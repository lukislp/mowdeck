"""Turn shapes at lane ends (ADR 0043): arcs, bulb turns and three-point turns.

The stripe patterns join two neighbouring runs with a short straight ``turn`` segment. A mower
that follows it literally rotates on the spot twice at every lane end, which scuffs the turf of
an all-wheel-drive mower. :func:`shape_turns` replaces every such lane-end reversal with a
drivable shape:

* ``arc``: a U-turn with a radius of at least half the lane spacing (a semicircle when half the
  spacing reaches ``min_radius_m``). When the spacing is narrower than twice the radius, a *bulb
  turn*: a short swing away from the next lane, a large arc over the neighbouring lane and a
  swing back in (three arcs of ``min_radius_m``).
* ``multi_point``: a three-point turn, a forward arc, a reverse arc (blades off) and a forward
  arc, all of ``min_radius_m``; a semicircle when the spacing allows one. Needs a drive link
  that can reverse; without one it falls back to ``arc``.
* ``in_place``: nothing changes (the straight connection, the mower rotates on the spot).

Every shape must stay inside ``allowed`` (the area shrunk by the safety margin, and the spot
run's geofence when the caller knows it). When it does not fit at the lane end, it is pulled
back into the lanes (the runs are shortened, the turn's own cut covers most of the ends); when it
does not fit at all, that lane end keeps its straight connection (the mower turns in place there)
and the result carries a warning. Only reversals between two ``mow`` runs are shaped: rings and
spirals need no turns of their own.

Pure geometry, no knowledge of a mower model.
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from shapely import prepare
from shapely.geometry import LineString
from shapely.geometry.base import BaseGeometry

from mowdeck_patterns.models import PatternError, Point, Segment

TurnStyleName = Literal["in_place", "arc", "multi_point"]
"""The generic turn styles (``mowdeck_contracts.v1.TurnStyle``)."""
TURN_STYLES: Final[tuple[TurnStyleName, ...]] = ("in_place", "arc", "multi_point")

REVERSAL_DEG: Final = 150.0
"""Two runs whose directions differ by more than this are a lane end that needs a turn."""
ARC_STEP_RAD: Final = math.radians(15.0)
"""Heading change between two sampled points of an arc."""
PULL_STEP_M: Final = 0.05
"""How far a turn that does not fit is pulled back into the lanes per attempt."""
MIN_LANE_M: Final = 0.1
"""A shortened run keeps at least this length."""
MIN_OFFSET_M: Final = 0.02
"""Lanes closer than this need no turn shape (they are the same lane)."""
_FIT_SLACK_M: Final = 1e-4


@dataclass(frozen=True, slots=True)
class TurnOptions:
    """How :func:`shape_turns` shapes the lane ends."""

    style: TurnStyleName = "arc"
    min_radius_m: float = 0.3
    """Smallest arc radius the mower drives without scuffing (the bulb and three-point turns
    use exactly this radius)."""
    reverse: bool = True
    """The drive link can reverse; without it ``multi_point`` falls back to ``arc``."""
    fence: BaseGeometry | None = None
    """A region the turns must stay in on top of the area (the spot run's geofence)."""

    def __post_init__(self) -> None:
        if self.style not in TURN_STYLES:
            msg = f"unknown turn style {self.style!r}"
            raise PatternError(msg)
        if not 0.05 <= self.min_radius_m <= 5:
            msg = "min_radius_m must be between 0.05 and 5"
            raise PatternError(msg)

    @property
    def effective_style(self) -> TurnStyleName:
        """The style after the fallback: ``multi_point`` needs ``reverse``."""
        if self.style == "multi_point" and not self.reverse:
            return "arc"
        return self.style


@dataclass(frozen=True, slots=True)
class TurnResult:
    """The segments with shaped lane ends."""

    segments: list[Segment]
    style: TurnStyleName
    """The style used (``arc`` when ``multi_point`` fell back for lack of reverse)."""
    lane_ends: int = 0
    """Lane-end reversals found."""
    shaped: int = 0
    """Lane ends that got the style's shape."""
    in_place: list[Point] = field(default_factory=list)
    """Lane ends where no shape fitted: the mower turns in place there."""
    warnings: tuple[str, ...] = ()


# --- canonical shapes ---------------------------------------------------------------------------
#
# In the frame of a lane end: the mower arrives at (0, 0) heading +x, the next lane lies to the
# left at y = d and is driven in -x. A shape ends somewhere on that line, at (e, d) heading -x.

_Pose = tuple[float, float, float]
_Piece = tuple[list[Point], bool]
"""Points of one part of a turn and whether it is driven backwards."""


def _arc(pose: _Pose, *, forward: bool, left: bool, radius: float, angle: float) -> list[_Pose]:
    """Poses along an arc of ``radius`` that changes the heading by ``angle`` (left: CCW)."""
    x, y, theta = pose
    k = 1.0 if left else -1.0
    direction = 1.0 if forward else -1.0
    cx = x - direction * k * radius * math.sin(theta)
    cy = y + direction * k * radius * math.cos(theta)
    steps = max(1, math.ceil(angle / ARC_STEP_RAD - 1e-9))
    poses: list[_Pose] = []
    for i in range(1, steps + 1):
        turn = k * angle * i / steps
        c, s = math.cos(turn), math.sin(turn)
        dx, dy = x - cx, y - cy
        poses.append((cx + dx * c - dy * s, cy + dx * s + dy * c, theta + turn))
    return poses


def _chain(parts: Sequence[tuple[bool, bool, float, float]]) -> list[_Piece]:
    """Arcs ``(forward, left, radius, angle)`` from (0, 0) heading +x, as pieces (consecutive
    arcs in the same direction of travel form one piece)."""
    pose: _Pose = (0.0, 0.0, 0.0)
    pieces: list[_Piece] = []
    for forward, left, radius, angle in parts:
        if angle <= 1e-9:
            continue
        poses = _arc(pose, forward=forward, left=left, radius=radius, angle=angle)
        points = [(p[0], p[1]) for p in poses]
        if pieces and pieces[-1][1] == (not forward):
            pieces[-1][0].extend(points)
        else:
            pieces.append(([(pose[0], pose[1]), *points], not forward))
        pose = poses[-1]
    return pieces


def _semicircle(d: float) -> list[_Piece]:
    return _chain([(True, True, d / 2, math.pi)])


def arc_shape(d: float, min_radius: float) -> list[_Piece]:
    """A U-turn to the lane at ``d`` (left): a semicircle when ``d / 2 >= min_radius``, else a
    bulb turn of three arcs of ``min_radius`` (away from the lane, over it, back in)."""
    return arc_shapes(d, min_radius)[0]


def _lopsided_bulb(d: float, r: float, *, swing_first: bool) -> list[_Piece]:
    """A bulb turn of two arcs of ``r``: without the swing away from the next lane
    (``swing_first`` False: a large left arc beyond the next lane, then back right), or without
    reaching beyond the next lane (``swing_first`` True: right first, then a large left arc that
    ends on the next lane at its top)."""

    def parts(gamma: float) -> list[tuple[bool, bool, float, float]]:
        if swing_first:
            return [(True, False, r, gamma), (True, True, r, math.pi + gamma)]
        return [(True, True, r, math.pi + gamma), (True, False, r, gamma)]

    low, high = 0.0, math.pi / 2  # lateral offset 2r .. 0, falling
    for _ in range(50):
        middle = (low + high) / 2
        if _chain(parts(middle))[-1][0][-1][1] > d:
            low = middle
        else:
            high = middle
    return _chain(parts((low + high) / 2))


def arc_shapes(d: float, min_radius: float) -> list[list[_Piece]]:
    """The arc turns to the lane at ``d`` (left), preferred first: the semicircle when
    ``d / 2 >= min_radius``; else the symmetric bulb turn (away from the lane, over it, back in)
    and two lopsided bulbs for lanes at the edge of the area: one that never swings away from
    the next lane (the first lane) and one that never reaches beyond it (the last lane)."""
    if d / 2 >= min_radius:
        return [_semicircle(d)]
    r = min_radius
    alpha = math.acos((d / (2 * r) + 1) / 2)
    bulb = _chain(
        [(True, False, r, alpha), (True, True, r, math.pi + 2 * alpha), (True, False, r, alpha)]
    )
    return [
        bulb,
        _lopsided_bulb(d, r, swing_first=False),
        _lopsided_bulb(d, r, swing_first=True),
    ]


def _three_point_end(d_angle: float, r: float) -> tuple[float, float]:
    pieces = _chain(
        [
            (True, True, r, d_angle),
            (False, True, r, math.pi - 2 * d_angle),
            (True, True, r, d_angle),
        ]
    )
    return pieces[-1][0][-1]


def multi_point_shape(d: float, min_radius: float) -> list[_Piece]:
    """A three-point turn to the lane at ``d`` (left): forward arc, reverse arc, forward arc of
    ``min_radius`` (the heading turns left all the way); a semicircle when ``d / 2`` reaches
    ``min_radius`` (no reverse needed)."""
    if d / 2 >= min_radius:
        return _semicircle(d)
    r = min_radius
    low, high = 0.0, math.pi / 2  # lateral offset -2r .. +2r, rising
    for _ in range(50):
        middle = (low + high) / 2
        if _three_point_end(middle, r)[1] < d:
            low = middle
        else:
            high = middle
    angle = (low + high) / 2
    return _chain(
        [(True, True, r, angle), (False, True, r, math.pi - 2 * angle), (True, True, r, angle)]
    )


# --- shaping ------------------------------------------------------------------------------------


def _heading(a: Point, b: Point) -> float:
    return math.atan2(b[1] - a[1], b[0] - a[0])


def _difference_deg(h1: float, h2: float) -> float:
    diff = abs(h2 - h1) % (2 * math.pi)
    return math.degrees(min(diff, 2 * math.pi - diff))


def _clean(points: Sequence[Point]) -> list[Point]:
    out: list[Point] = []
    for point in points:
        if not out or math.dist(out[-1], point) > 1e-6:
            out.append(point)
    return out


@dataclass(frozen=True, slots=True)
class _Fit:
    previous: tuple[Point, ...]
    turn: list[Segment]
    following: tuple[Point, ...]


def _fit_lane_end(
    previous: Segment,
    turn: Segment,
    following: Segment,
    allowed: BaseGeometry,
    style: TurnStyleName,
    min_radius: float,
) -> _Fit | None:
    """The shaped turn between ``previous`` and ``following``; None when nothing fits."""
    p_points, n_points = list(previous.polyline), list(following.polyline)
    start, end = p_points[-1], n_points[0]
    h = _heading(p_points[-2], start)
    ux, uy = math.cos(h), math.sin(h)
    rel = (end[0] - start[0], end[1] - start[1])
    along = rel[0] * ux + rel[1] * uy
    lateral = ux * rel[1] - uy * rel[0]
    d = abs(lateral)
    if d < MIN_OFFSET_M:
        return None
    side = 1.0 if lateral > 0 else -1.0
    vx, vy = -uy * side, ux * side  # towards the next lane

    def world(x: float, y: float) -> Point:
        return (start[0] + ux * x + vx * y, start[1] + uy * x + vy * y)

    shapes = arc_shapes(d, min_radius) if style == "arc" else [multi_point_shape(d, min_radius)]
    prev_room = math.dist(p_points[-2], start) - MIN_LANE_M
    next_room = math.dist(end, n_points[1]) - MIN_LANE_M
    for shape in shapes:
        fit = _place(
            shape, p_points, n_points, along, d, world, prev_room, next_room, allowed, turn
        )
        if fit is not None:
            return fit
    return None


def _place(
    shape: list[_Piece],
    p_points: list[Point],
    n_points: list[Point],
    along: float,
    d: float,
    world: Callable[[float, float], Point],
    prev_room: float,
    next_room: float,
    allowed: BaseGeometry,
    turn: Segment,
) -> _Fit | None:
    """``shape`` at the lane end, pulled back into the lanes until it fits ``allowed``."""
    e = shape[-1][0][-1][0]
    high = max(0.0, along - e)
    low = max(-prev_room, along - e - next_room)
    x0 = high
    while x0 >= low - 1e-9:
        pieces: list[_Piece] = [([(x + x0, y) for x, y in pts], rev) for pts, rev in shape]
        if x0 > 1e-9:  # extend the arriving lane up to the turn
            pieces[0] = ([(0.0, 0.0), *pieces[0][0]], pieces[0][1])
        if x0 + e > along + 1e-9:  # drive on to the next lane's start
            pieces[-1] = ([*pieces[-1][0], (along, d)], pieces[-1][1])
        world_pieces = [([world(x, y) for x, y in pts], rev) for pts, rev in pieces]
        if all(allowed.covers(LineString(_clean(pts))) for pts, _ in world_pieces):
            new_previous = (*p_points[:-1], world(x0, 0.0)) if x0 < -1e-9 else tuple(p_points)
            new_following = (
                (world(x0 + e, d), *n_points[1:]) if x0 + e < along - 1e-9 else tuple(n_points)
            )
            segments = [
                Segment(
                    tuple(_clean(pts)),
                    turn.blades_on and not rev,
                    turn.speed,
                    "turn",
                    reverse=rev,
                )
                for pts, rev in world_pieces
                if len(_clean(pts)) >= 2
            ]
            return _Fit(new_previous, segments, new_following)
        x0 -= PULL_STEP_M
    return None


def shape_turns(
    segments: Sequence[Segment], allowed: BaseGeometry, options: TurnOptions
) -> TurnResult:
    """Replace the straight lane-end turns of ``segments`` with the shapes of ``options``
    (see the module docs); every shape stays inside ``allowed`` (and ``options.fence``)."""
    style = options.effective_style
    notes: list[str] = []
    if options.style == "multi_point" and style == "arc":
        notes.append("the drive link cannot reverse: multi-point turns fall back to arcs")
    if style == "in_place":
        return TurnResult(list(segments), style, warnings=tuple(notes))
    region = allowed if options.fence is None else allowed.intersection(options.fence)
    region = region.buffer(_FIT_SLACK_M)
    prepare(region)
    work = list(segments)
    out: list[Segment] = []
    lane_ends = shaped = 0
    in_place: list[Point] = []
    for index, segment in enumerate(work):
        previous = out[-1] if out else None
        following = work[index + 1] if index + 1 < len(work) else None
        lane_end = (
            segment.kind == "turn"
            and len(segment.polyline) == 2
            and not segment.reverse
            and previous is not None
            and following is not None
            and previous.kind == "mow"
            and following.kind == "mow"
            and len(previous.polyline) >= 2
            and len(following.polyline) >= 2
            and _difference_deg(
                _heading(previous.polyline[-2], previous.polyline[-1]),
                _heading(following.polyline[0], following.polyline[1]),
            )
            > REVERSAL_DEG
        )
        if not lane_end:
            out.append(segment)
            continue
        assert previous is not None  # noqa: S101 - lane_end
        assert following is not None  # noqa: S101 - lane_end
        lane_ends += 1
        fit = _fit_lane_end(previous, segment, following, region, style, options.min_radius_m)
        if fit is None:
            in_place.append(segment.polyline[0])
            out.append(segment)
            continue
        shaped += 1
        out[-1] = _with_points(previous, fit.previous)
        out.extend(fit.turn)
        work[index + 1] = _with_points(following, fit.following)
    if in_place:
        x, y = in_place[0]
        notes.append(
            f"{len(in_place)} of {lane_ends} lane ends turn in place: no {style.replace('_', '-')} "
            f"turn fits inside the area (first near ({x:.2f}, {y:.2f}))"
        )
    return TurnResult(out, style, lane_ends, shaped, in_place, tuple(notes))


def _with_points(segment: Segment, points: Sequence[Point]) -> Segment:
    if tuple(points) == segment.polyline:
        return segment
    return Segment(tuple(points), segment.blades_on, segment.speed, segment.kind, segment.reverse)


__all__ = [
    "TURN_STYLES",
    "TurnOptions",
    "TurnResult",
    "TurnStyleName",
    "arc_shape",
    "arc_shapes",
    "multi_point_shape",
    "shape_turns",
]
