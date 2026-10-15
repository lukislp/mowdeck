"""Request and response bodies of the Planner API (``/api/v1/planner``)."""

from datetime import datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from mowdeck_contracts.v1 import MowerMap, SpotMowPlan, StartMow
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from planner.application.planning import (
    AreaSpec,
    Execution,
    GapsResult,
    PlanResult,
    TransitResult,
    TransitStart,
)
from planner.domain.models import Template

LocalPoint = tuple[float, float]
Ring = Annotated[list[LocalPoint], Field(min_length=3, max_length=5000)]
PatternKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
TemplateName = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[^/\x00-\x1f]+$")]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PatternOut(BaseModel):
    """A registered pattern: the app builds the settings form from ``settings_schema``."""

    key: str
    name: str
    description: str | None = None
    settings_schema: dict[str, Any] = Field(description="JSON Schema (draft 2020-12).")
    native: str | None = Field(
        default=None,
        description="Name of the native pattern a mower may run instead (on a whole map area); "
        "whether a mower supports it is in its capabilities (`native_patterns`).",
    )


class PatternList(BaseModel):
    """Every registered pattern, by key."""

    items: list[PatternOut]


class ChainStepIn(_Body):
    """One pattern of a chain with its settings (validated by the pattern's schema)."""

    pattern: PatternKey
    settings: dict[str, Any] = Field(default_factory=dict)


class MowerSpecIn(_Body):
    """Overrides of the mower data (defaults: the Planner's settings and the mower's speed)."""

    cutting_width_m: float | None = Field(default=None, ge=0.05, le=2)
    overlap_m: float | None = Field(default=None, ge=0, le=1)
    turning_radius_m: float | None = Field(default=None, ge=0, le=10)
    max_speed_mps: float | None = Field(default=None, gt=0, le=2)
    transfer_speed_mps: float | None = Field(default=None, gt=0, le=2)
    safety_margin_m: float | None = Field(default=None, ge=0, le=2)

    def overrides(self) -> dict[str, float]:
        """The given values only."""
        return {k: v for k, v in self.model_dump().items() if v is not None}


class MowerSpecOut(BaseModel):
    """The mower data a plan was computed with."""

    cutting_width_m: float
    overlap_m: float
    spacing_m: float
    turning_radius_m: float
    max_speed_mps: float
    transfer_speed_mps: float
    safety_margin_m: float


class AreaIn(_Body):
    """Where to mow. ``area_id`` alone is a whole map area; a polygon (local metres or WGS84
    ``[lat, lon]``) is a spot, inside ``area_id`` when given, else inside the mapped areas."""

    area_id: str | None = Field(default=None, min_length=1, max_length=128)
    polygon: Ring | None = None
    polygon_wgs84: Ring | None = Field(default=None, description="`[lat, lon]` pairs.")
    holes: list[Ring] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _one_outline(self) -> Self:
        if self.polygon is not None and self.polygon_wgs84 is not None:
            msg = "give polygon or polygon_wgs84, not both"
            raise ValueError(msg)
        if self.area_id is None and self.polygon is None and self.polygon_wgs84 is None:
            msg = "give area_id, polygon or polygon_wgs84"
            raise ValueError(msg)
        return self

    def spec(self) -> AreaSpec:
        """The use case form."""
        return AreaSpec(
            area_id=self.area_id,
            polygon=self.polygon,
            polygon_wgs84=self.polygon_wgs84,
            holes=list(self.holes),
        )


class TransitIn(_Body):
    """Where a spot run's transit would start (ADR 0041): ``from_dock`` for a docked mower (the
    map's dock, as the Device-Gateway plans it), else the mower's ``position`` in local
    metres."""

    from_dock: bool = False
    position: LocalPoint | None = None

    @model_validator(mode="after")
    def _one_start(self) -> Self:
        if self.from_dock == (self.position is not None):
            msg = "give from_dock or position"
            raise ValueError(msg)
        return self

    def start(self) -> TransitStart:
        """As the use case input."""
        return TransitStart(from_dock=self.from_dock, position=self.position)


class TransitOut(BaseModel):
    """The transit a spot run would drive from the start to the plan's first point (blades
    off), planned by the same algorithm as the Device-Gateway's run."""

    found: bool = Field(description="A route exists (the web app's route condition).")
    from_dock: bool
    length_m: float
    route: list[list[float]] = Field(
        description="The route, local metres or `[lon, lat]` with `wgs84`; empty without one."
    )
    reason: str | None = Field(default=None, description="`no_route: ...` without a route.")

    @classmethod
    def of(cls, result: TransitResult) -> "TransitOut":
        """From the use case result."""
        return cls(
            found=result.found,
            from_dock=result.from_dock,
            length_m=round(result.length_m, 2),
            route=[[round(x, 7), round(y, 7)] for x, y in result.route],
            reason=result.reason,
        )


class PlanRequestIn(_Body):
    """A plan for a mower (``mower_id``: its map and capabilities) or for a raw ``map``."""

    mower_id: UUID | None = None
    map: MowerMap | None = Field(default=None, description="A map instead of a mower's.")
    area: AreaIn
    chain: list[ChainStepIn] = Field(default_factory=list, max_length=8)
    template: TemplateName | None = Field(
        default=None, description="Use the chain of a saved template instead of `chain`."
    )
    mower: MowerSpecIn | None = None
    wgs84: bool = Field(default=False, description="Preview coordinates as `[lon, lat]`.")
    transit: TransitIn | None = Field(
        default=None,
        description="Also plan the transit of a spot run from the dock or a position to the "
        "plan's first point (ADR 0041).",
    )

    @model_validator(mode="after")
    def _sources(self) -> Self:
        if self.mower_id is not None and self.map is not None:
            msg = "give mower_id or map, not both"
            raise ValueError(msg)
        if bool(self.chain) == (self.template is not None):
            msg = "give chain (one to eight patterns) or template"
            raise ValueError(msg)
        return self


class ExecutionOut(BaseModel):
    """How the mower can run the plan."""

    mode: Literal["custom_paths", "native", "unsupported", "unknown"] = Field(
        description="`custom_paths`: the mower follows the route; `native`: it runs "
        "`native_job` on the map area; `unsupported`: it cannot run this plan; `unknown`: no "
        "capabilities (a raw map)."
    )
    reason: str
    native_job: dict[str, Any] | None = Field(
        default=None, description="`StartMow.pattern` settings of the native form, if any."
    )

    @classmethod
    def of(cls, execution: Execution) -> "ExecutionOut":
        """From the use case result."""
        return cls(mode=execution.mode, reason=execution.reason, native_job=execution.native_job)


class PreviewOut(BaseModel):
    """A validated plan: totals, how it runs, and the segments as a GeoJSON-like collection."""

    pattern_chain: list[str]
    plan_hash: str
    estimated_duration_s: float
    covered_area_m2: float
    area_m2: float
    coverage_ratio: float
    mow_length_m: float
    transfer_length_m: float
    segment_count: int
    plan_bytes: int
    warnings: list[str]
    execution: ExecutionOut
    mower: MowerSpecOut
    preview: dict[str, Any] = Field(
        description="FeatureCollection: one LineString per segment with `kind`, `blades_on`, "
        "`speed_mps`; coordinates local metres, or `[lon, lat]` with `wgs84`."
    )
    transit: TransitOut | None = Field(
        default=None, description="The transit of a spot run, when the request asked for it."
    )

    @classmethod
    def fields_of(cls, result: PlanResult) -> dict[str, Any]:
        """The shared fields of a result."""
        plan = result.plan
        spec = result.mower
        return {
            "pattern_chain": list(plan.pattern_chain),
            "plan_hash": result.spot.plan_hash,
            "estimated_duration_s": plan.estimated_duration_s,
            "covered_area_m2": plan.covered_area_m2,
            "area_m2": plan.area_m2,
            "coverage_ratio": round(plan.coverage_ratio, 4),
            "mow_length_m": round(plan.mow_length_m, 2),
            "transfer_length_m": round(plan.transfer_length_m, 2),
            "segment_count": len(plan.segments),
            "plan_bytes": result.plan_bytes,
            "warnings": list(plan.warnings),
            "execution": ExecutionOut.of(result.execution),
            "mower": MowerSpecOut(
                cutting_width_m=spec.cutting_width_m,
                overlap_m=spec.overlap_m,
                spacing_m=round(spec.spacing_m, 4),
                turning_radius_m=spec.turning_radius_m,
                max_speed_mps=spec.max_speed_mps,
                transfer_speed_mps=spec.transfer_speed_mps,
                safety_margin_m=spec.safety_margin_m,
            ),
            "preview": result.preview,
            "transit": TransitOut.of(result.transit) if result.transit is not None else None,
        }

    @classmethod
    def of(cls, result: PlanResult) -> "PreviewOut":
        """From the use case result."""
        return cls(**cls.fields_of(result))


class PlanOut(PreviewOut):
    """A plan ready to start: send ``command`` to Fleet
    (``POST /api/v1/fleet/mowers/{mower_id}/commands`` with ``{"command": ...}``)."""

    spot_plan: SpotMowPlan
    command: StartMow

    @classmethod
    def of(cls, result: PlanResult) -> "PlanOut":
        """From the use case result."""
        return cls(
            **PreviewOut.fields_of(result),
            spot_plan=result.spot,
            command=StartMow(spot_plan=result.spot),
        )


class GapsRequestIn(_Body):
    """Which parts of an area did the mower miss between ``start`` and ``end``?"""

    mower_id: UUID
    start: AwareDatetime
    end: AwareDatetime
    area: AreaIn | None = Field(
        default=None, description="Default: the map area with the most track points."
    )
    min_gap_m2: float = Field(default=0.05, ge=0, le=100)
    cutting_width_m: float | None = Field(default=None, ge=0.05, le=2)


class GapsOut(BaseModel):
    """Covered ground and gaps; ``remow`` is a ready chain step for the gaps."""

    area_id: str | None
    area_m2: float
    covered_area_m2: float
    coverage_ratio: float
    track_points: int
    truncated: bool
    gap_area_m2: float
    gaps: list[list[LocalPoint]] = Field(description="Gap polygons in local metres.")
    gaps_wgs84: list[list[LocalPoint]] = Field(description="The same as `[lat, lon]`.")
    remow: ChainStepIn | None = Field(
        default=None, description="A `remow` step over the gaps (None without gaps)."
    )

    @classmethod
    def of(cls, result: GapsResult) -> "GapsOut":
        """From the use case result."""
        remow = (
            ChainStepIn(pattern="remow", settings={"gaps": [list(g) for g in result.gaps]})
            if result.gaps
            else None
        )
        return cls(
            area_id=result.area_id,
            area_m2=result.area_m2,
            covered_area_m2=result.covered_area_m2,
            coverage_ratio=result.coverage_ratio,
            track_points=result.track_points,
            truncated=result.truncated,
            gap_area_m2=result.gap_area_m2,
            gaps=result.gaps,
            gaps_wgs84=result.gaps_wgs84,
            remow=remow,
        )


class TemplateIn(_Body):
    """A saved chain."""

    description: str | None = Field(default=None, max_length=500)
    chain: list[ChainStepIn] = Field(min_length=1, max_length=8)


class TemplateOut(BaseModel):
    """A saved chain of the account."""

    name: str
    description: str | None
    chain: list[ChainStepIn]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, template: Template) -> "TemplateOut":
        """From the entity."""
        return cls(
            name=template.name,
            description=template.description,
            chain=[ChainStepIn.model_validate(step) for step in template.chain],
            created_at=template.created_at,
            updated_at=template.updated_at,
        )


class TemplateList(BaseModel):
    """Templates of the account, by name."""

    items: list[TemplateOut]
