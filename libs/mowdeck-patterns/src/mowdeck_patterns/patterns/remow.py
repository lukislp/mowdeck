"""Re-mow: stripes over the gaps of the last run only."""

import math
from typing import Any, ClassVar, Self

from pydantic import Field, model_validator
from shapely.geometry import Polygon
from shapely.ops import unary_union

from mowdeck_patterns.base import BasePattern, PatternSettings
from mowdeck_patterns.coverage import gaps
from mowdeck_patterns.geometry import Run, Workspace, stripe_runs
from mowdeck_patterns.models import Point, polygonal, polygons_of

PolygonPoints = list[tuple[float, float]]
MAX_GAP_POLYGONS = 500


def _polygon(points: PolygonPoints) -> Polygon | None:
    if len(points) < 3:
        return None
    polygon = Polygon(points).buffer(0)
    return polygon if not polygon.is_empty else None


class RemowSettings(PatternSettings):
    """Settings of :class:`RemowPattern`: the gaps themselves, or the covered ground."""

    gaps: list[PolygonPoints] = Field(
        default_factory=list,
        max_length=MAX_GAP_POLYGONS,
        title="Gaps",
        description="Polygons to mow, in local metres (from the Planner's coverage gaps).",
    )
    covered: list[PolygonPoints] = Field(
        default_factory=list,
        max_length=MAX_GAP_POLYGONS,
        title="Covered",
        description="Ground already mowed; the gaps are the rest of the area.",
    )
    min_gap_m2: float = Field(default=0.05, ge=0, le=100, title="Smallest gap (m²)")
    angle_deg: float = Field(default=0.0, ge=-360, le=360, title="Angle")

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if not self.gaps and not self.covered:
            msg = "give the gaps or the covered ground"
            raise ValueError(msg)
        return self


class RemowPattern(BasePattern[RemowSettings]):
    """Mows only the gaps of the last run, each with a few stripes."""

    key: ClassVar[str] = "remow"
    name: ClassVar[str] = "Re-mow gaps"
    description: ClassVar[str] = "Only the gaps the last run left; touch-ups."
    settings_model: ClassVar[type[PatternSettings]] = RemowSettings

    def targets(self, workspace: Workspace, settings: RemowSettings) -> list[Polygon]:
        """The gap polygons inside the area."""
        if settings.gaps:
            found = [p for p in (_polygon(g) for g in settings.gaps) if p is not None]
            merged = polygonal(unary_union(found)).intersection(workspace.region)
            return [p for p in polygons_of(merged) if p.area >= settings.min_gap_m2]
        covered = polygonal(
            unary_union([p for p in (_polygon(c) for c in settings.covered) if p is not None])
        )
        return gaps(workspace.region, covered, settings.min_gap_m2)

    def runs(self, workspace: Workspace, settings: RemowSettings, start: Point | None) -> list[Run]:
        spacing = workspace.mower.spacing_m
        left = self.targets(workspace, settings)
        runs: list[Run] = []
        here = start
        while left:
            anchor = here if here is not None else left[0].representative_point().coords[0]
            gap = min(left, key=lambda g: g.distance(_point(anchor)))
            left.remove(gap)
            target = gap.buffer(spacing / 2).intersection(workspace.work)
            if target.is_empty:
                continue
            part = stripe_runs(target, settings.angle_deg, spacing, here)
            runs.extend(part)
            if part:
                here = part[-1][-1]
        return runs

    def warnings(self, workspace: Workspace, settings: RemowSettings) -> tuple[str, ...]:
        unreachable = [
            g
            for g in self.targets(workspace, settings)
            if g.buffer(workspace.mower.spacing_m / 2).intersection(workspace.work).is_empty
        ]
        if not unreachable:
            return ()
        total = sum(g.area for g in unreachable)
        return (
            f"{len(unreachable)} gaps ({math.ceil(total * 100) / 100} m²) lie too close to an "
            "edge or zone to reach",
        )


def _point(point: Point | tuple[float, ...]) -> Any:
    from shapely.geometry import Point as ShapelyPoint

    return ShapelyPoint(point)
