"""Stripe patterns: stripes, diagonal, checkerboard, auto-aligned and alternating direction."""

import random
from collections.abc import Mapping
from typing import Any, ClassVar, Literal

from pydantic import Field, field_validator

from mowdeck_patterns.base import BasePattern, PatternSettings
from mowdeck_patterns.geometry import (
    Run,
    Workspace,
    count_stripe_runs,
    edge_angles,
    inset,
    stripe_runs,
)
from mowdeck_patterns.models import Area, MowerSpec, Point

AngleDeg = float


def normalise_angle(angle: float) -> float:
    """``angle`` folded into ``[0, 180)`` degrees (a stripe and its reverse are the same)."""
    return round(angle % 180.0, 6)


class StripesSettings(PatternSettings):
    """Settings of :class:`StripesPattern`."""

    angle_deg: AngleDeg = Field(
        default=0.0,
        ge=-360,
        le=360,
        title="Angle",
        description="Direction of the stripes in degrees, counter-clockwise from the map's x axis.",
    )


class StripesPattern(BasePattern[StripesSettings]):
    """Parallel back-and-forth runs at a free angle."""

    key: ClassVar[str] = "stripes"
    name: ClassVar[str] = "Stripes"
    description: ClassVar[str] = "Parallel runs at any angle; the fast standard."
    settings_model: ClassVar[type[PatternSettings]] = StripesSettings
    native_name: ClassVar[str | None] = "stripes"

    def angles(self, workspace: Workspace, settings: StripesSettings) -> list[float]:
        """Stripe directions, one per pass."""
        return [normalise_angle(settings.angle_deg)]

    def runs(
        self, workspace: Workspace, settings: StripesSettings, start: Point | None
    ) -> list[Run]:
        runs: list[Run] = []
        here = start
        for angle in self.angles(workspace, settings):
            part = stripe_runs(workspace.work, angle, workspace.mower.spacing_m, here)
            runs.extend(part)
            if part:
                here = part[-1][-1]
        return runs

    def native_job(
        self, settings: Mapping[str, Any], mower: MowerSpec, area: Area | None = None
    ) -> dict[str, Any] | None:
        parsed = self.parse(settings)
        assert isinstance(parsed, StripesSettings)  # noqa: S101 - settings_model
        return {
            "name": self.native_name,
            "angle_deg": normalise_angle(parsed.angle_deg),
            "spacing_m": round(mower.spacing_m, 3),
        }


class DiagonalPattern(StripesPattern):
    """Stripes at 45 degrees to a base direction."""

    key: ClassVar[str] = "diagonal"
    name: ClassVar[str] = "Diagonal"
    description: ClassVar[str] = "Stripes at 45 degrees to the base angle; fewer visible tracks."
    native_name: ClassVar[str | None] = "diagonal"

    def angles(self, workspace: Workspace, settings: StripesSettings) -> list[float]:
        return [normalise_angle(settings.angle_deg + 45.0)]


class CheckerboardPattern(StripesPattern):
    """Two stripe passes crossing at a right angle."""

    key: ClassVar[str] = "checkerboard"
    name: ClassVar[str] = "Checkerboard"
    description: ClassVar[str] = "Two passes crossing at 90 degrees; an even cut."
    native_name: ClassVar[str | None] = "checkerboard"

    def angles(self, workspace: Workspace, settings: StripesSettings) -> list[float]:
        angle = normalise_angle(settings.angle_deg)
        return [angle, normalise_angle(angle + 90.0)]


class AutoAlignedSettings(PatternSettings):
    """Settings of :class:`AutoAlignedPattern`."""

    step_deg: int = Field(
        default=5,
        ge=1,
        le=45,
        title="Angle step",
        description="Candidate angles every this many degrees, plus the directions of the "
        "longest edges.",
    )


def best_angle(region: Any, spacing: float, step_deg: int = 5) -> float:
    """The stripe angle with the fewest runs over ``region``.

    Ties go to the direction of the longest edge, then to the smaller angle.
    """
    edges = [normalise_angle(a) for a in edge_angles(region)]
    candidates = sorted({normalise_angle(a) for a in range(0, 180, step_deg)} | set(edges))

    def edge_rank(angle: float) -> int:
        return next((i for i, edge in enumerate(edges) if abs(edge - angle) < 1e-6), len(edges))

    scored = [
        (count_stripe_runs(region, angle, spacing), edge_rank(angle), angle) for angle in candidates
    ]
    return min(scored)[2]


class AutoAlignedPattern(BasePattern[AutoAlignedSettings]):
    """Stripes at the angle that needs the fewest runs (and so the fewest turns)."""

    key: ClassVar[str] = "auto_aligned"
    name: ClassVar[str] = "Auto-aligned"
    description: ClassVar[str] = (
        "Stripes at the angle with the fewest turns; long, narrow areas, saves battery."
    )
    settings_model: ClassVar[type[PatternSettings]] = AutoAlignedSettings

    def runs(
        self, workspace: Workspace, settings: AutoAlignedSettings, start: Point | None
    ) -> list[Run]:
        angle = best_angle(workspace.work, workspace.mower.spacing_m, settings.step_deg)
        return stripe_runs(workspace.work, angle, workspace.mower.spacing_m, start)

    def native_job(
        self, settings: Mapping[str, Any], mower: MowerSpec, area: Area | None = None
    ) -> dict[str, Any] | None:
        if area is None:
            return None
        parsed = self.parse(settings)
        work = inset(area.region(), mower.inset_m)
        if work.is_empty:
            return None
        angle = best_angle(work, mower.spacing_m, parsed.step_deg)
        return {"name": "stripes", "angle_deg": angle, "spacing_m": round(mower.spacing_m, 3)}


class AlternatingSettings(PatternSettings):
    """Settings of :class:`AlternatingPattern`."""

    mode: Literal["rotate", "random"] = Field(
        default="rotate",
        title="Mode",
        description="rotate: the next angle of the list on every run; random: a random angle "
        "that is the same for the same seed and run.",
    )
    angles_deg: list[float] = Field(
        default_factory=lambda: [0.0, 45.0, 90.0, 135.0],
        min_length=1,
        max_length=36,
        title="Angles",
        description="Rotation list of stripe angles in degrees.",
    )
    run_index: int = Field(
        default=0,
        ge=0,
        le=1_000_000,
        title="Run number",
        description="Which run this is (schedules count it up); picks the angle.",
    )
    seed: int = Field(default=0, ge=0, le=2**31 - 1, title="Seed", description="Random seed.")

    @field_validator("angles_deg")
    @classmethod
    def _finite(cls, value: list[float]) -> list[float]:
        if any(not -360 <= a <= 360 for a in value):
            msg = "angles must be between -360 and 360 degrees"
            raise ValueError(msg)
        return value


def alternating_angle(settings: AlternatingSettings) -> float:
    """The angle of this run: deterministic for the same settings."""
    if settings.mode == "random":
        rng = random.Random(f"mowdeck:{settings.seed}:{settings.run_index}")  # noqa: S311
        return float(rng.randrange(0, 180, 5))
    return normalise_angle(settings.angles_deg[settings.run_index % len(settings.angles_deg)])


class AlternatingPattern(BasePattern[AlternatingSettings]):
    """Stripes whose angle changes with every run, so no tracks form in the lawn."""

    key: ClassVar[str] = "alternating"
    name: ClassVar[str] = "Alternating direction"
    description: ClassVar[str] = "The stripe angle changes with every run: no tracks in the lawn."
    settings_model: ClassVar[type[PatternSettings]] = AlternatingSettings
    native_name: ClassVar[str | None] = "stripes"

    def runs(
        self, workspace: Workspace, settings: AlternatingSettings, start: Point | None
    ) -> list[Run]:
        angle = alternating_angle(settings)
        return stripe_runs(workspace.work, angle, workspace.mower.spacing_m, start)

    def native_job(
        self, settings: Mapping[str, Any], mower: MowerSpec, area: Area | None = None
    ) -> dict[str, Any] | None:
        parsed = self.parse(settings)
        return {
            "name": "stripes",
            "angle_deg": alternating_angle(parsed),
            "spacing_m": round(mower.spacing_m, 3),
        }
