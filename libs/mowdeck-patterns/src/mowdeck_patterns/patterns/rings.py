"""Ring patterns: spiral, contour-parallel, edge only and border first plus fill."""

import dataclasses
from collections.abc import Mapping
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field
from shapely.geometry import LineString, Polygon

from mowdeck_patterns.base import BasePattern, PatternSettings
from mowdeck_patterns.geometry import (
    Run,
    Workspace,
    closed_lap,
    inset,
    nearest_first,
    offset_levels,
    ring_points,
)
from mowdeck_patterns.models import Area, InvalidSettings, MowerSpec, Point, polygons_of

MAX_NATIVE_BORDER_LAPS = 4
Direction = Literal["inward", "outward"]


def _reverse(runs: list[Run]) -> list[Run]:
    return [tuple(reversed(run)) for run in reversed(runs)]


def contour_runs(
    work: Any,
    spacing: float,
    start: Point | None,
    *,
    max_levels: int | None = None,
    direction: Direction = "inward",
) -> list[Run]:
    """Closed laps parallel to every edge (outside and around holes), level by level."""
    here = start
    runs: list[Run] = []
    for level in offset_levels(work, spacing, max_levels):
        rings: list[list[Point]] = []
        for polygon in level:
            rings.append(ring_points(polygon.exterior.coords))
            rings.extend(ring_points(interior.coords) for interior in polygon.interiors)
        rings = [r for r in rings if len(r) >= 3]
        if not rings:
            continue
        anchor = here if here is not None else rings[0][0]
        ends = {
            i: (_nearest_vertex(r, anchor), _nearest_vertex(r, anchor)) for i, r in enumerate(rings)
        }
        for ring in nearest_first(rings, ends, anchor):
            lap = closed_lap(ring, here)
            runs.append(lap)
            here = lap[-1]
    return _reverse(runs) if direction == "outward" else runs


def _nearest_vertex(ring: list[Point], point: Point) -> Point:
    return min(ring, key=lambda p: (p[0] - point[0]) ** 2 + (p[1] - point[1]) ** 2)


def spiral_runs(
    work: Any,
    spacing: float,
    start: Point | None,
    *,
    max_levels: int | None = None,
    direction: Direction = "inward",
) -> list[Run]:
    """Concentric laps around the outer shape only; where a lap meets a hole it is cut."""
    shells = [Polygon(p.exterior) for p in polygons_of(work)]
    here = start
    runs: list[Run] = []
    cutting = work.buffer(1e-6)
    for shell in shells:
        for level in offset_levels(shell, spacing, max_levels):
            pieces: list[Run] = []
            for polygon in level:
                ring = ring_points(polygon.exterior.coords)
                if len(ring) < 3:
                    continue
                lap = closed_lap(ring, here)
                line = LineString(lap)
                if cutting.covers(line):
                    pieces.append(lap)
                    continue
                clipped = line.intersection(work)
                for part in getattr(clipped, "geoms", [clipped]):
                    if isinstance(part, LineString) and part.length > 0.02:
                        pieces.append(tuple((c[0], c[1]) for c in part.coords))
            if not pieces:
                continue
            anchor = here if here is not None else pieces[0][0]
            ends = {i: (p[0], p[-1]) for i, p in enumerate(pieces)}
            for piece in nearest_first(pieces, ends, anchor):
                runs.append(piece)
                here = piece[-1]
    return _reverse(runs) if direction == "outward" else runs


class SpiralSettings(PatternSettings):
    """Settings of :class:`SpiralPattern`."""

    direction: Direction = Field(
        default="inward", title="Direction", description="From the edge inwards or outwards."
    )


class SpiralPattern(BasePattern[SpiralSettings]):
    """Concentric laps from the outside in (or out), around the outer shape."""

    key: ClassVar[str] = "spiral"
    name: ClassVar[str] = "Spiral"
    description: ClassVar[str] = "From the outside in or the inside out; round and compact areas."
    settings_model: ClassVar[type[PatternSettings]] = SpiralSettings

    def runs(
        self, workspace: Workspace, settings: SpiralSettings, start: Point | None
    ) -> list[Run]:
        return spiral_runs(
            workspace.work, workspace.mower.spacing_m, start, direction=settings.direction
        )


class ContourSettings(PatternSettings):
    """Settings of :class:`ContourPattern`."""

    direction: Direction = Field(default="inward", title="Direction")
    rings: int | None = Field(
        default=None,
        ge=1,
        le=500,
        title="Rings",
        description="Stop after this many rings; empty: until the area is filled.",
    )


class ContourPattern(BasePattern[ContourSettings]):
    """Laps parallel to every edge, also around beds and zones."""

    key: ClassVar[str] = "contour"
    name: ClassVar[str] = "Contour-parallel"
    description: ClassVar[str] = "Laps follow the shape inwards; angled beds and curves."
    settings_model: ClassVar[type[PatternSettings]] = ContourSettings

    def runs(
        self, workspace: Workspace, settings: ContourSettings, start: Point | None
    ) -> list[Run]:
        return contour_runs(
            workspace.work,
            workspace.mower.spacing_m,
            start,
            max_levels=settings.rings,
            direction=settings.direction,
        )


class EdgeOnlySettings(PatternSettings):
    """Settings of :class:`EdgeOnlyPattern`."""

    laps: int = Field(default=1, ge=1, le=10, title="Laps", description="Number of edge laps.")


class EdgeOnlyPattern(BasePattern[EdgeOnlySettings]):
    """One or more laps along the edges only."""

    key: ClassVar[str] = "edge_only"
    name: ClassVar[str] = "Edge only"
    description: ClassVar[str] = "One or more laps along the edges; between two full runs."
    settings_model: ClassVar[type[PatternSettings]] = EdgeOnlySettings
    native_name: ClassVar[str | None] = "edge_only"

    def runs(
        self, workspace: Workspace, settings: EdgeOnlySettings, start: Point | None
    ) -> list[Run]:
        return contour_runs(
            workspace.work, workspace.mower.spacing_m, start, max_levels=settings.laps
        )

    def native_job(
        self, settings: Mapping[str, Any], mower: MowerSpec, area: Area | None = None
    ) -> dict[str, Any] | None:
        parsed = self.parse(settings)
        if parsed.laps > MAX_NATIVE_BORDER_LAPS:
            return None
        return {
            "name": "edge_only",
            "border_laps": parsed.laps,
            "spacing_m": round(mower.spacing_m, 3),
        }


FillKey = Literal[
    "stripes",
    "diagonal",
    "checkerboard",
    "auto_aligned",
    "alternating",
    "spiral",
    "contour",
    "cells",
]


class FillSpec(BaseModel):
    """The pattern that fills the area inside the border laps."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pattern: FillKey = Field(default="stripes", title="Fill pattern")
    settings: dict[str, Any] = Field(
        default_factory=dict, title="Fill settings", description="Settings of the fill pattern."
    )


class BorderFillSettings(PatternSettings):
    """Settings of :class:`BorderFillPattern`."""

    laps: int = Field(default=2, ge=1, le=10, title="Border laps")
    fill: FillSpec = Field(default_factory=FillSpec, title="Fill")


def _fill_pattern(key: str) -> BasePattern[Any]:
    from mowdeck_patterns.patterns import BUILTIN_PATTERNS

    return BUILTIN_PATTERNS[key]()


class BorderFillPattern(BasePattern[BorderFillSettings]):
    """Border laps first, then any fill pattern inside them."""

    key: ClassVar[str] = "border_fill"
    name: ClassVar[str] = "Border first + fill"
    description: ClassVar[str] = "Border laps, then a fill pattern inside; clean edges."
    settings_model: ClassVar[type[PatternSettings]] = BorderFillSettings

    def runs(
        self, workspace: Workspace, settings: BorderFillSettings, start: Point | None
    ) -> list[Run]:
        spacing = workspace.mower.spacing_m
        border = contour_runs(workspace.work, spacing, start, max_levels=settings.laps)
        here = border[-1][-1] if border else start
        fill = _fill_pattern(settings.fill.pattern)
        fill_settings = fill.parse(settings.fill.settings)
        inner = inset(workspace.work, settings.laps * spacing)
        if inner.is_empty:
            return border
        return [
            *border,
            *fill.runs(dataclasses.replace(workspace, work=inner), fill_settings, here),
        ]

    def parse(self, settings: Mapping[str, Any] | None) -> BorderFillSettings:
        parsed: BorderFillSettings = super().parse(settings)
        try:
            _fill_pattern(parsed.fill.pattern).parse(parsed.fill.settings)
        except InvalidSettings as exc:
            errors = [f"fill.settings.{e}" for e in exc.errors]
            msg = f"invalid settings for {self.key}: " + "; ".join(errors)
            raise InvalidSettings(msg, errors) from exc
        return parsed

    def native_job(
        self, settings: Mapping[str, Any], mower: MowerSpec, area: Area | None = None
    ) -> dict[str, Any] | None:
        parsed = self.parse(settings)
        if parsed.laps > MAX_NATIVE_BORDER_LAPS:
            return None
        fill = _fill_pattern(parsed.fill.pattern)
        if fill.native_name not in {"stripes", "diagonal", "checkerboard"}:
            return None
        job = fill.native_job(parsed.fill.settings, mower, area)
        if job is None:
            return None
        return {**job, "border_laps": parsed.laps, "order": "border_first"}
