"""The plug-in interface :class:`MowPattern` and the base class of the built-in patterns.

A pattern is a class with a ``key``, a ``name``, a JSON Schema of its settings and a
``compute`` method. The built-in patterns derive from :class:`BasePattern`: they describe their
settings as a Pydantic model (the JSON Schema comes from it, the same model validates the
input) and only decide which runs to cut in which order; joining the runs into segments,
estimating duration and coverage is shared.
"""

from collections.abc import Mapping
from typing import Any, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from shapely.geometry.base import BaseGeometry

from mowdeck_patterns.geometry import (
    Router,
    Run,
    Workspace,
    assemble,
    covered_geometry,
    duration_s,
)
from mowdeck_patterns.models import (
    Area,
    InvalidSettings,
    MowerSpec,
    MowPlan,
    PlanningError,
    Point,
    Segment,
)

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


@runtime_checkable
class MowPattern(Protocol):
    """What the Planner needs from a pattern plug-in."""

    @property
    def key(self) -> str:
        """Stable identifier (entry point name), lower-case with underscores."""
        ...

    @property
    def name(self) -> str:
        """Human readable name."""
        ...

    def settings_schema(self) -> dict[str, Any]:
        """JSON Schema (draft 2020-12) of the settings; the app builds its form from it."""
        ...

    def compute(self, area: Area, mower: MowerSpec, settings: Mapping[str, Any]) -> MowPlan:
        """The plan for ``area``. Raises ``InvalidSettings`` or ``PlanningError``."""
        ...


class PatternSettings(BaseModel):
    """Settings every built-in pattern accepts. Unknown keys are refused."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    speed_mps: float | None = Field(
        default=None,
        gt=0,
        le=2,
        title="Speed",
        description="Mowing speed in m/s; default: the mower's speed.",
    )


def build_plan(
    segments: list[Segment],
    area: Area | None,
    mower: MowerSpec,
    chain: tuple[str, ...],
    *,
    region: BaseGeometry | None = None,
    warnings: tuple[str, ...] = (),
) -> MowPlan:
    """Wrap segments into a plan with duration, covered area and area size."""
    if not segments:
        msg = "the pattern produced no route: the area is too small or too narrow for it"
        raise PlanningError(msg)
    target = region if region is not None else (area.region() if area is not None else None)
    covered = covered_geometry(segments, mower.cutting_width_m)
    covered_area = covered.intersection(target).area if target is not None else covered.area
    return MowPlan(
        segments=tuple(segments),
        estimated_duration_s=round(duration_s(segments, mower), 1),
        covered_area_m2=round(covered_area, 2),
        pattern_chain=chain,
        area_m2=round(target.area, 2) if target is not None else 0.0,
        warnings=warnings,
    )


class BasePattern[S: PatternSettings]:
    """Base class: settings model, schema, validation and the shared compute steps."""

    key: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str] = ""
    settings_model: ClassVar[type[PatternSettings]] = PatternSettings
    native_name: ClassVar[str | None] = None
    """Name of the equivalent native pattern (``StartMow.pattern.name``), if any."""

    def settings_schema(self) -> dict[str, Any]:
        """JSON Schema of :attr:`settings_model` with the pattern's title and description."""
        schema = self.settings_model.model_json_schema(mode="validation")
        schema["title"] = self.name
        if self.description:
            schema["description"] = self.description
        return {"$schema": JSON_SCHEMA_DIALECT, **schema}

    def parse(self, settings: Mapping[str, Any] | None) -> S:
        """Validate ``settings``; raises :class:`InvalidSettings` with one line per error."""
        try:
            parsed = self.settings_model.model_validate(dict(settings or {}))
        except ValidationError as exc:
            errors = [
                f"{'.'.join(str(p) for p in error['loc']) or '(root)'}: {error['msg']}"
                for error in exc.errors()
            ]
            msg = f"invalid settings for {self.key}: " + "; ".join(errors)
            raise InvalidSettings(msg, errors) from exc
        return parsed  # type: ignore[return-value]

    def runs(self, workspace: Workspace, settings: S, start: Point | None) -> list[Run]:
        """The runs to cut, in order. Built-in patterns override this."""
        raise NotImplementedError

    def segments(
        self,
        workspace: Workspace,
        settings: S,
        *,
        start: Point | None = None,
        router: Router | None = None,
    ) -> list[Segment]:
        """Runs joined into segments (turns and transfers)."""
        runs = self.runs(workspace, settings, start)
        return assemble(runs, workspace, speed=settings.speed_mps, router=router)

    def warnings(self, workspace: Workspace, settings: S) -> tuple[str, ...]:
        """Hints for the preview (none by default)."""
        return ()

    def compute(self, area: Area, mower: MowerSpec, settings: Mapping[str, Any]) -> MowPlan:
        """Parse the settings and plan ``area``."""
        parsed = self.parse(settings)
        region = area.region()
        workspace = Workspace.of(region, mower)
        if workspace.work.is_empty:
            msg = (
                f"the area is too narrow: nothing is left after keeping {mower.inset_m:.2f} m "
                "from every edge"
            )
            raise PlanningError(msg)
        segments = self.segments(workspace, parsed)
        return build_plan(
            segments,
            area,
            mower,
            (self.key,),
            region=region,
            warnings=self.warnings(workspace, parsed),
        )

    def native_job(
        self, settings: Mapping[str, Any], mower: MowerSpec, area: Area | None = None
    ) -> dict[str, Any] | None:
        """``StartMow.pattern`` settings when the mower could run this natively, else None."""
        return None
