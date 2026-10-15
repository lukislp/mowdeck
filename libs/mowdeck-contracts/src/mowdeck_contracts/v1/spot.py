"""Spot mowing: a computed route that travels inline in ``StartMow.spot_plan``.

The Planner (``mowdeck-patterns``) turns an area and a pattern chain into a neutral mowing plan
of ordered segments: drive this polyline with the blades on or off at this speed. The plan is
sent to Fleet inside the start command (no stored plan reference: the Planner keeps no plans),
Fleet records its hash, and the Device-Gateway passes it to the mower connection:

* a mower whose capabilities say ``custom_paths`` follows the segments itself;
* any other mower may only run it when the Planner attached a ``native`` job (whole map areas,
  one pattern the mower knows), otherwise the command is rejected.

Segments are compact: each polyline is an encoded string (the polyline algorithm known from map
services, here on local metres with centimetre precision, see :func:`encode_polyline`), so a
plan of a few thousand points stays well below the bus message size. ``plan_hash`` is the
SHA-256 of the canonical JSON of the chain and the segments; a plan whose hash does not match
its content is invalid, so a stored hash always identifies exactly one route.
"""

import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from typing import Any, Final, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from mowdeck_contracts.v1._base import ContractModel
from mowdeck_contracts.v1.maps import LocalPoint

SPOT_PRECISION: Final = 2
"""Decimal places of spot plan coordinates in metres (centimetres)."""
MAX_SPOT_SEGMENTS: Final = 10_000
"""Most segments one plan may have."""
MAX_SEGMENT_PATH_CHARS: Final = 200_000
"""Longest encoded polyline of one segment."""
MAX_SPOT_SPEED_MPS: Final = 2.0
"""Highest segment speed (the same limit as ``SetSpeed``)."""

SegmentKind = Literal["mow", "turn", "transfer"]
"""``mow``: a cutting run; ``turn``: the short connection between two runs (blades may stay on);
``transfer``: driving to the next part of the plan, always with the blades off."""


# --- Polyline encoding ------------------------------------------------------------------------


def _encode_value(value: int) -> str:
    shifted = ~(value << 1) if value < 0 else value << 1
    chunks: list[str] = []
    while shifted >= 0x20:
        chunks.append(chr((0x20 | (shifted & 0x1F)) + 63))
        shifted >>= 5
    chunks.append(chr(shifted + 63))
    return "".join(chunks)


def encode_polyline(points: Iterable[tuple[float, float]], precision: int = SPOT_PRECISION) -> str:
    """Encode ``points`` as a polyline string with ``precision`` decimal places.

    Each coordinate is rounded to ``10**-precision``, stored as the difference to the previous
    point, zig-zag encoded and written in 5-bit groups as ASCII characters 63 to 126.
    """
    factor = 10**precision
    last_a = last_b = 0
    out: list[str] = []
    for a, b in points:
        if not (math.isfinite(a) and math.isfinite(b)):
            msg = "polyline coordinates must be finite"
            raise ValueError(msg)
        ia, ib = round(a * factor), round(b * factor)
        out.append(_encode_value(ia - last_a))
        out.append(_encode_value(ib - last_b))
        last_a, last_b = ia, ib
    return "".join(out)


def decode_polyline(encoded: str, precision: int = SPOT_PRECISION) -> list[tuple[float, float]]:
    """Decode a string of :func:`encode_polyline`. Raises ``ValueError`` when malformed."""
    factor = 10**precision
    values: list[int] = []
    current = shift = 0
    for char in encoded:
        code = ord(char) - 63
        if not 0 <= code < 64:
            msg = f"invalid polyline character {char!r}"
            raise ValueError(msg)
        current |= (code & 0x1F) << shift
        if code & 0x20:
            shift += 5
            if shift > 60:
                msg = "polyline value too long"
                raise ValueError(msg)
            continue
        values.append(~(current >> 1) if current & 1 else current >> 1)
        current = shift = 0
    if shift or len(values) % 2:
        msg = "truncated polyline"
        raise ValueError(msg)
    points: list[tuple[float, float]] = []
    a = b = 0
    for index in range(0, len(values), 2):
        a += values[index]
        b += values[index + 1]
        points.append((a / factor, b / factor))
    return points


# --- Models -----------------------------------------------------------------------------------


class SpotSegment(ContractModel):
    """One part of a spot plan: drive along ``path`` with the blades on or off."""

    kind: SegmentKind = "mow"
    blades_on: bool
    speed_mps: float = Field(gt=0, le=MAX_SPOT_SPEED_MPS)
    path: str = Field(
        min_length=4,
        max_length=MAX_SEGMENT_PATH_CHARS,
        description="Encoded polyline of local metres (x, y), centimetre precision "
        "(`encode_polyline`), at least two points.",
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.kind == "transfer" and self.blades_on:
            msg = "transfer segments must have the blades off"
            raise ValueError(msg)
        if len(decode_polyline(self.path)) < 2:
            msg = "a segment needs at least two points"
            raise ValueError(msg)
        return self

    @classmethod
    def from_points(
        cls,
        points: Sequence[LocalPoint],
        *,
        blades_on: bool,
        speed_mps: float,
        kind: SegmentKind = "mow",
    ) -> "SpotSegment":
        """Build a segment from local points (rounded to centimetres)."""
        return cls(
            kind=kind,
            blades_on=blades_on,
            speed_mps=round(speed_mps, 3),
            path=encode_polyline(points),
        )

    def points(self) -> list[LocalPoint]:
        """The decoded polyline in local metres."""
        return decode_polyline(self.path)


class SpotNativeJob(ContractModel):
    """The plan expressed as a native job of the mower: whole map areas and one pattern.

    Present only when the chain is exactly one pattern the mower may run by itself (``stripes``,
    ``diagonal``, ``checkerboard``, ``edge_only``, or ``border_fill`` with one of them) on map
    areas, so a mower without ``custom_paths`` can still execute it as an ordinary
    ``StartMow(area_ids, pattern)``.
    """

    area_ids: list[str] = Field(min_length=1)
    pattern: dict[str, Any] = Field(
        description="Settings in the form of `StartMow.pattern` (`name`, `angle_deg`, "
        "`spacing_m`, `border_laps`, `order`)."
    )


def spot_plan_hash(pattern_chain: Sequence[str], segments: Sequence[SpotSegment]) -> str:
    """SHA-256 (hex) of the canonical JSON of the chain and the segments."""
    canonical = json.dumps(
        {
            "pattern_chain": list(pattern_chain),
            "segments": [[s.kind, s.blades_on, round(s.speed_mps, 3), s.path] for s in segments],
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SpotMowPlan(ContractModel):
    """A computed mowing route for ``StartMow.spot_plan``."""

    plan_hash: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 of the chain and the segments (`spot_plan_hash`); must match.",
    )
    pattern_chain: list[str] = Field(min_length=1, max_length=16)
    segments: list[SpotSegment] = Field(min_length=1, max_length=MAX_SPOT_SEGMENTS)
    estimated_duration_s: float = Field(default=0.0, ge=0)
    covered_area_m2: float = Field(default=0.0, ge=0)
    native: SpotNativeJob | None = None

    @model_validator(mode="after")
    def _check_hash(self) -> Self:
        if spot_plan_hash(self.pattern_chain, self.segments) != self.plan_hash:
            msg = "plan_hash does not match the pattern chain and segments"
            raise ValueError(msg)
        return self

    @classmethod
    def build(
        cls,
        segments: Sequence[SpotSegment],
        pattern_chain: Sequence[str],
        *,
        estimated_duration_s: float = 0.0,
        covered_area_m2: float = 0.0,
        native: SpotNativeJob | None = None,
    ) -> "SpotMowPlan":
        """Build a plan and compute its hash."""
        return cls(
            plan_hash=spot_plan_hash(pattern_chain, segments),
            pattern_chain=list(pattern_chain),
            segments=list(segments),
            estimated_duration_s=round(estimated_duration_s, 1),
            covered_area_m2=round(covered_area_m2, 2),
            native=native,
        )

    @property
    def point_count(self) -> int:
        """Number of points of all segments."""
        return sum(len(segment.points()) for segment in self.segments)


# --- Spot runs driven by MowDeck (ADR 0039) ---------------------------------------------------


MAX_SPOT_MOW_SPEED_MPS: Final = 0.3
"""Highest speed of a ``SpotMow`` run MowDeck drives itself over the Bluetooth bridge."""
MAX_SPOT_POLYGON_POINTS: Final = 64
"""Most corners of a ``SpotPolygon``."""

LatLon = tuple[float, float]
"""A WGS84 point as ``[lat, lon]`` (the Planner's ``polygon_wgs84`` convention)."""


class SpotCircle(ContractModel):
    """A round spot: ``radius_m`` around ``center_wgs84`` (``[lat, lon]``)."""

    center_wgs84: LatLon
    radius_m: float = Field(gt=0, le=10)


class SpotPolygon(ContractModel):
    """A drawn spot: a closed ring of WGS84 ``[lat, lon]`` corners (first point not repeated)."""

    polygon_wgs84: list[LatLon] = Field(min_length=3, max_length=MAX_SPOT_POLYGON_POINTS)


SpotTarget = SpotCircle | SpotPolygon
"""Where a spot run was requested, kept for the audit record (the route is in ``plan``)."""

SpotRunState = Literal[
    "preparing",
    "undocking",
    "waiting_rtk",
    "transit",
    "spot_mowing",
    "paused_rtk",
    "returning",
    "stopping",
    "done",
    "aborted",
    "failed",
]
"""Life cycle of a spot run (ADR 0039, ADR 0041): ``preparing`` (checks), ``undocking`` (the
mower leaves its charging station on its own), ``waiting_rtk`` (out of the dock, waiting for a
fixed RTK position), ``transit`` (driving to the spot with the blades off), ``spot_mowing``
(driving the plan), ``paused_rtk`` (waiting for a fixed RTK position), ``returning`` (the mower
was sent home), ``stopping`` (a stop was requested), then one of ``done``, ``aborted``
(``reason`` says why, e.g. ``rtk_lost``, ``geofence``, ``link_lost``, ``stopped_by_user``,
``emergency_stop``, ``sensor_stop``, ``timeout``, ``heading_mismatch``, ``undock_failed``,
``no_route``) or ``failed``."""

SPOT_RUN_FINAL_STATES: Final[frozenset[str]] = frozenset({"done", "aborted", "failed"})
"""States after which a spot run does not change any more."""


class SpotRunStatus(ContractModel):
    """Progress of the spot run MowDeck drives on this mower (``MowerState.spot_run``)."""

    run_id: UUID
    state: SpotRunState
    progress_percent: int = Field(default=0, ge=0, le=100)
    segment_index: int = Field(default=0, ge=0, description="The segment being driven (0-based).")
    segments_total: int = Field(default=0, ge=0)
    planned_points: int = Field(default=0, ge=0, description="Points of the plan in total.")
    driven_points: int = Field(default=0, ge=0, description="Points reached so far.")
    dry_run: bool = Field(
        default=True, description="The blades stayed off for the whole run (the default)."
    )
    reason: str | None = Field(
        default=None, max_length=200, description="Why the run ended early or paused, if so."
    )

    @property
    def final(self) -> bool:
        """True once the run has ended (``done``, ``aborted`` or ``failed``)."""
        return self.state in SPOT_RUN_FINAL_STATES
