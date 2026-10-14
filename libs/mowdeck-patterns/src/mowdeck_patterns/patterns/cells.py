"""Decomposed cells: the area is cut into simpler pieces, each mowed in its own best direction.

The decomposition is a trapezoidal (slab) split: vertical cuts through every concave corner of
the outline and every corner of a hole or zone, in a frame turned by ``sweep_angle_deg``. Each
slab piece is a cell; cells are visited nearest first and striped at the angle with the fewest
runs inside the cell.
"""

import itertools
import math
from typing import Any, ClassVar

from pydantic import Field
from shapely.affinity import rotate
from shapely.geometry import Polygon, box
from shapely.geometry.polygon import orient

from mowdeck_patterns.base import BasePattern, PatternSettings
from mowdeck_patterns.geometry import Run, Workspace, ring_points, stripe_runs
from mowdeck_patterns.models import Point, polygons_of
from mowdeck_patterns.patterns.stripes import best_angle

MAX_CUTS = 60
"""At most this many cuts per polygon (more corners are thinned out)."""
MERGE_DISTANCE_M = 0.05


def _cut_positions(polygon: Polygon) -> list[float]:
    oriented = orient(polygon, 1.0)
    xs: list[float] = []
    ring = ring_points(oriented.exterior.coords)
    count = len(ring)
    for i in range(count):
        a, b, c = ring[i - 1], ring[i], ring[(i + 1) % count]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if cross < -1e-9:
            xs.append(b[0])
    for interior in oriented.interiors:
        xs.extend(p[0] for p in ring_points(interior.coords))
    xs.sort()
    merged: list[float] = []
    for x in xs:
        if not merged or x - merged[-1] > MERGE_DISTANCE_M:
            merged.append(x)
    if len(merged) > MAX_CUTS:
        step = len(merged) / MAX_CUTS
        merged = [merged[math.floor(i * step)] for i in range(MAX_CUTS)]
    return merged


def decompose(region: Any, sweep_angle_deg: float = 0.0, min_area: float = 0.0) -> list[Polygon]:
    """Cells of ``region``: slab pieces between cuts through concave corners and hole corners."""
    cells: list[Polygon] = []
    turned = rotate(region, -sweep_angle_deg, origin=(0.0, 0.0))
    for polygon in polygons_of(turned):
        minx, miny, maxx, maxy = polygon.bounds
        cuts = [x for x in _cut_positions(polygon) if minx + 1e-6 < x < maxx - 1e-6]
        edges = [minx, *cuts, maxx]
        for left, right in itertools.pairwise(edges):
            slab = box(left, miny - 1.0, right, maxy + 1.0)
            for piece in polygons_of(polygon.intersection(slab)):
                if piece.area > max(min_area, 1e-6):
                    cells.append(rotate(piece, sweep_angle_deg, origin=(0.0, 0.0)))
    return cells


class CellsSettings(PatternSettings):
    """Settings of :class:`CellsPattern`."""

    sweep_angle_deg: float = Field(
        default=0.0,
        ge=-360,
        le=360,
        title="Cut direction",
        description="Cells are cut across this direction (degrees from the map's x axis).",
    )
    step_deg: int = Field(
        default=15, ge=1, le=45, title="Angle step", description="Candidate angles per cell."
    )


class CellsPattern(BasePattern[CellsSettings]):
    """The area is split into cells, each striped in its own best direction."""

    key: ClassVar[str] = "cells"
    name: ClassVar[str] = "Cells"
    description: ClassVar[str] = (
        "Splits the area into pieces with their own direction; complex shapes with obstacles."
    )
    settings_model: ClassVar[type[PatternSettings]] = CellsSettings

    def runs(self, workspace: Workspace, settings: CellsSettings, start: Point | None) -> list[Run]:
        spacing = workspace.mower.spacing_m
        cells = decompose(workspace.work, settings.sweep_angle_deg, min_area=spacing * 0.02)
        runs: list[Run] = []
        here = start
        left = list(cells)
        while left:
            anchor = here if here is not None else (left[0].bounds[0], left[0].bounds[1])
            cell = min(left, key=lambda c: c.exterior.distance(_shapely_point(anchor)))
            left.remove(cell)
            angle = best_angle(cell, spacing, settings.step_deg)
            part = stripe_runs(cell, angle, spacing, here)
            runs.extend(part)
            if part:
                here = part[-1][-1]
        return runs


def _shapely_point(point: Point) -> Any:
    from shapely.geometry import Point as ShapelyPoint

    return ShapelyPoint(point)
