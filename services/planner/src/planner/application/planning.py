"""Plans and coverage: the use cases around ``mowdeck-patterns``.

* :meth:`Planning.compute`: area (a map area, a spot polygon in local metres or WGS84, or both)
  plus a pattern chain gives a validated plan, its preview, the ``SpotMowPlan`` for Fleet and
  how the mower can run it (``custom_paths``, natively, or not at all).
* :meth:`Planning.gaps`: the mower's position track in a time range gives the covered ground
  and the gaps left in the area, ready for a ``remow`` step.

The geometry is CPU-bound: it runs in worker threads, at most ``max_parallel`` at a time, so the
event loop keeps answering health checks and other requests.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from mowdeck_authz import mower
from mowdeck_contracts.v1 import (
    MowerCapabilities,
    MowerMap,
    PositionTrackRequest,
    Principal,
    SpotMowPlan,
    SpotNativeJob,
)
from mowdeck_control.geo import from_wgs84, to_wgs84
from mowdeck_patterns import (
    Area,
    InvalidArea,
    InvalidSettings,
    MowerSpec,
    MowPlan,
    PatternError,
    PatternRegistry,
    PlanningError,
    PlanRejected,
    UnknownPattern,
    check,
    coverage,
    covered_ratio,
    gaps,
    plan,
    preview,
)
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from planner.application.access import CAN_CONTROL, CAN_VIEW, Access
from planner.application.context import PlannerConfig
from planner.application.ports import Store, TrackSource
from planner.domain.errors import ConflictError, InvalidInput, NotFoundError

LocalPoints = list[tuple[float, float]]
ExecutionMode = Literal["custom_paths", "native", "unsupported", "unknown"]

NATIVE_NAMES = frozenset({"stripes", "diagonal", "checkerboard", "edge_only"})


@dataclass(frozen=True, slots=True)
class AreaSpec:
    """Where to mow: a map area, a spot polygon (local metres or WGS84), or both."""

    area_id: str | None = None
    polygon: LocalPoints | None = None
    polygon_wgs84: LocalPoints | None = None
    """``(lat, lon)`` pairs, converted with the map's geo reference."""
    holes: list[LocalPoints] = field(default_factory=list)

    @property
    def is_whole_area(self) -> bool:
        """True when it is exactly one map area without a drawn outline or holes."""
        return (
            self.area_id is not None
            and self.polygon is None
            and self.polygon_wgs84 is None
            and not self.holes
        )


@dataclass(frozen=True, slots=True)
class PlanInput:
    """One planning request."""

    chain: list[tuple[str, dict[str, Any]]]
    area: AreaSpec
    mower_id: UUID | None = None
    raw_map: MowerMap | None = None
    mower_overrides: dict[str, float] = field(default_factory=dict)
    wgs84_preview: bool = False


@dataclass(frozen=True, slots=True)
class Execution:
    """How the mower can run a plan."""

    mode: ExecutionMode
    reason: str
    native_job: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PlanResult:
    """A validated plan with everything the API shows."""

    plan: MowPlan
    spot: SpotMowPlan
    preview: dict[str, Any]
    execution: Execution
    mower: MowerSpec
    plan_bytes: int


@dataclass(frozen=True, slots=True)
class GapsInput:
    """A coverage question: which parts of an area did the mower miss in a time range."""

    mower_id: UUID
    start: datetime
    end: datetime
    area: AreaSpec = field(default_factory=AreaSpec)
    min_gap_m2: float = 0.05
    cutting_width_m: float | None = None


@dataclass(frozen=True, slots=True)
class GapsResult:
    """Covered ground and gaps (local metres and WGS84)."""

    area_id: str | None
    area_m2: float
    covered_area_m2: float
    coverage_ratio: float
    track_points: int
    truncated: bool
    gaps: list[LocalPoints]
    gaps_wgs84: list[LocalPoints]
    gap_area_m2: float


class Planning:
    """Use cases of plans and coverage."""

    def __init__(
        self,
        store: Store,
        access: Access,
        registry: PatternRegistry,
        config: PlannerConfig,
        tracks: TrackSource | None = None,
    ) -> None:
        self.store = store
        self.access = access
        self.registry = registry
        self.config = config
        self.tracks = tracks
        self._slots = asyncio.Semaphore(config.max_parallel)

    async def _run[T](self, work: Callable[[], T]) -> T:
        async with self._slots:
            return await asyncio.to_thread(work)

    # --- inputs --------------------------------------------------------------------------

    async def _mower_facts(
        self, principal: Principal, account_id: UUID, mower_id: UUID, relation: str
    ) -> tuple[MowerMap, MowerCapabilities | None]:
        await self.access.require(principal, relation, mower(mower_id))
        async with self.store.transaction() as tx:
            facts = await tx.get_facts(mower_id)
        if facts is None or facts.account_id != account_id:
            raise ConflictError("no map received from the mower yet")
        if facts.map is None:
            raise ConflictError("no map received from the mower yet")
        caps = (
            MowerCapabilities.model_validate(facts.capabilities)
            if facts.capabilities is not None
            else None
        )
        return MowerMap.model_validate(facts.map), caps

    def mower_spec(self, overrides: dict[str, float], caps: MowerCapabilities | None) -> MowerSpec:
        """The configured mower data, the request's overrides and the mower's speed limit."""
        values: dict[str, float] = {
            "cutting_width_m": self.config.cutting_width_m,
            "overlap_m": self.config.overlap_m,
            "turning_radius_m": self.config.turning_radius_m,
            "max_speed_mps": self.config.mowing_speed_mps,
            "transfer_speed_mps": self.config.transfer_speed_mps,
            "safety_margin_m": self.config.safety_margin_m,
            **overrides,
        }
        if caps is not None and caps.max_speed_mps is not None:
            values["max_speed_mps"] = min(values["max_speed_mps"], caps.max_speed_mps)
            values["transfer_speed_mps"] = min(values["transfer_speed_mps"], caps.max_speed_mps)
        try:
            return MowerSpec(**values)
        except PatternError as exc:
            raise InvalidInput(str(exc)) from exc

    def area_of(self, spec: AreaSpec, mower_map: MowerMap | None) -> Area:
        """Build the :class:`Area`; raises :class:`InvalidInput`."""
        outline = spec.polygon
        if spec.polygon_wgs84 is not None:
            if mower_map is None:
                raise InvalidInput("a WGS84 polygon needs a map with a geo reference")
            outline = [from_wgs84(mower_map, lat, lon) for lat, lon in spec.polygon_wgs84]
        vertices = len(outline or []) + sum(len(h) for h in spec.holes)
        if vertices > self.config.max_vertices:
            msg = f"the spot has {vertices} vertices, at most {self.config.max_vertices}"
            raise InvalidInput(msg)
        try:
            if spec.area_id is not None:
                if mower_map is None:
                    raise InvalidInput("area_id needs a mower or a map")
                area = Area.from_map(mower_map, spec.area_id, boundary=outline, holes=spec.holes)
            elif outline is not None:
                zones = mower_map.no_go_zones if mower_map is not None else []
                obstacles = mower_map.obstacles if mower_map is not None else []
                area = Area.of(
                    outline,
                    holes=spec.holes,
                    no_go_zones=[z.boundary for z in zones],
                    obstacles=[o.boundary for o in obstacles],
                )
            else:
                raise InvalidInput("give area_id, polygon or polygon_wgs84")
        except InvalidArea as exc:
            raise InvalidInput(str(exc)) from exc
        region = area.region()
        if region.area > self.config.max_area_m2:
            msg = f"the area covers {region.area:.0f} m², at most {self.config.max_area_m2:.0f} m²"
            raise InvalidInput(msg)
        if outline is not None and mower_map is not None:
            self._inside_map(Polygon(outline), mower_map, spec.area_id)
        return area

    def _inside_map(self, outline: Polygon, mower_map: MowerMap, area_id: str | None) -> None:
        """A drawn spot must stay inside the mapped area(s): the mower knows nothing else."""
        areas = [a for a in mower_map.areas if area_id is None or a.id == area_id]
        mapped = unary_union([Polygon(a.boundary).buffer(0) for a in areas])
        if not mapped.buffer(self.config.spot_tolerance_m).covers(outline.buffer(0)):
            where = f"area {area_id}" if area_id else "the mower's mapped areas"
            raise InvalidInput(f"the spot must lie inside {where}")

    # --- plans ---------------------------------------------------------------------------

    def execution(
        self,
        spec: AreaSpec,
        chain: Sequence[tuple[str, dict[str, Any]]],
        mower_spec: MowerSpec,
        area: Area,
        caps: MowerCapabilities | None,
    ) -> Execution:
        """How the mower can run the plan, and the native job if there is one."""
        native = self.native_job(spec, chain, mower_spec, area)
        if native is not None and caps is not None and not caps.supports_pattern(native["name"]):
            native = None
        if caps is None:
            return Execution("unknown", "the mower's capabilities are not known", native)
        if caps.custom_paths:
            return Execution("custom_paths", "the mower follows the route itself", native)
        if native is not None:
            return Execution(
                "native", f"runs as the mower's own {native['name']} job on {spec.area_id}", native
            )
        return Execution(
            "unsupported",
            "the mower cannot follow custom paths and the plan has no native form (one "
            "native pattern on a whole map area)",
        )

    def native_job(
        self,
        spec: AreaSpec,
        chain: Sequence[tuple[str, dict[str, Any]]],
        mower_spec: MowerSpec,
        area: Area,
    ) -> dict[str, Any] | None:
        """``StartMow.pattern`` settings when the chain is one native pattern on a whole area."""
        if not spec.is_whole_area or len(chain) != 1:
            return None
        key, settings = chain[0]
        pattern = self.registry.get(key)
        build = getattr(pattern, "native_job", None)
        if build is None:
            return None
        job: dict[str, Any] | None = build(settings, mower_spec, area)
        if job is None or job.get("name") not in NATIVE_NAMES:
            return None
        spacing = job.get("spacing_m")
        if isinstance(spacing, float) and not 0.05 <= spacing <= 2.0:
            return None
        return job

    async def compute(
        self, principal: Principal, account_id: UUID, request: PlanInput, *, for_start: bool
    ) -> PlanResult:
        """Compute, validate and describe a plan (``for_start`` asks ``can_control``)."""
        caps: MowerCapabilities | None = None
        mower_map = request.raw_map
        if request.mower_id is not None:
            relation = CAN_CONTROL if for_start else CAN_VIEW
            mower_map, caps = await self._mower_facts(
                principal, account_id, request.mower_id, relation
            )
        if not request.chain:
            raise InvalidInput("the chain needs at least one pattern")
        for key, _ in request.chain:
            try:
                self.registry.get(key)
            except UnknownPattern as exc:
                raise InvalidInput(str(exc)) from exc
        mower_spec = self.mower_spec(request.mower_overrides, caps)
        area = self.area_of(request.area, mower_map)
        reference = mower_map.reference if mower_map is not None else None

        def work() -> tuple[MowPlan, dict[str, Any], Execution]:
            try:
                result = plan(area, mower_spec, list(request.chain), self.registry)
            except InvalidSettings as exc:
                raise InvalidInput(str(exc), exc.errors) from exc
            except PlanRejected as exc:
                raise InvalidInput("the plan failed the validation chain", exc.reasons) from exc
            except (PlanningError, PatternError) as exc:
                raise InvalidInput(str(exc)) from exc
            convert = None
            if request.wgs84_preview and reference is not None:

                def convert(x: float, y: float) -> tuple[float, float]:
                    return to_wgs84(reference, x, y)

            execution = self.execution(request.area, request.chain, mower_spec, area, caps)
            return result, preview(result, convert), execution

        result, document, execution = await self._run(work)
        native = (
            SpotNativeJob(area_ids=[request.area.area_id or ""], pattern=execution.native_job)
            if execution.native_job is not None and request.area.area_id
            else None
        )
        spot = result.to_spot_plan(native)
        rounded = check(MowPlan.from_spot_plan(spot), area, mower_spec)
        if rounded:
            raise InvalidInput("the plan failed the validation chain after rounding", rounded)
        size = len(spot.model_dump_json())
        if size > self.config.max_plan_bytes:
            msg = (
                f"the plan has {size} bytes, at most {self.config.max_plan_bytes} can be sent; "
                "mow a smaller spot or split the area"
            )
            raise InvalidInput(msg)
        document["properties"]["plan_hash"] = spot.plan_hash
        return PlanResult(result, spot, document, execution, mower_spec, size)

    # --- coverage ------------------------------------------------------------------------

    async def gaps(self, principal: Principal, account_id: UUID, request: GapsInput) -> GapsResult:
        """Covered ground and gaps of an area from the position track of a time range."""
        if request.start >= request.end:
            raise InvalidInput("start must be before end")
        if request.end - request.start > self.config.coverage_max_range:
            hours = self.config.coverage_max_range.total_seconds() / 3600
            raise InvalidInput(f"the time range may span at most {hours:.0f} hours")
        mower_map, _ = await self._mower_facts(principal, account_id, request.mower_id, CAN_VIEW)
        if self.tracks is None:
            raise InvalidInput("coverage needs the event bus")
        reply = await self.tracks.track(
            PositionTrackRequest(
                account_id=account_id,
                mower_id=request.mower_id,
                start=request.start,
                end=request.end,
                limit=self.config.track_limit,
            )
        )
        if reply.error is not None:
            raise InvalidInput(f"telemetry refused the track: {reply.error}")
        track = [from_wgs84(mower_map, lat, lon) for lat, lon in reply.points()]
        width = request.cutting_width_m or self.config.cutting_width_m
        area_spec = request.area
        if (
            area_spec.area_id is None
            and area_spec.polygon is None
            and area_spec.polygon_wgs84 is None
        ):
            area_spec = replace(area_spec, area_id=_busiest_area(mower_map, track))
        area = self.area_of(area_spec, mower_map)
        limit = self.config.max_gaps

        def work() -> tuple[float, float, list[Polygon]]:
            try:
                covered = coverage(track, width)
            except PatternError as exc:
                raise InvalidInput(str(exc)) from exc
            left = gaps(area, covered, request.min_gap_m2)
            region = area.region()
            covered_area = region.intersection(covered).area if not covered.is_empty else 0.0
            ratio = covered_ratio(area, covered) if not covered.is_empty else 0.0
            return covered_area, ratio, left[:limit]

        covered_area, ratio, left = await self._run(work)
        local = [_ring(g) for g in left]
        reference = mower_map.reference
        return GapsResult(
            area_id=area_spec.area_id,
            area_m2=round(area.region().area, 2),
            covered_area_m2=round(covered_area, 2),
            coverage_ratio=round(ratio, 4),
            track_points=reply.count,
            truncated=reply.truncated,
            gaps=local,
            gaps_wgs84=[[_round_geo(to_wgs84(reference, x, y)) for x, y in ring] for ring in local],
            gap_area_m2=round(sum(g.area for g in left), 2),
        )


def _ring(polygon: Polygon) -> LocalPoints:
    simple = polygon.simplify(0.02, preserve_topology=True)
    target = simple if isinstance(simple, Polygon) and not simple.is_empty else polygon
    coords = [(round(x, 3), round(y, 3)) for x, y in target.exterior.coords]
    return coords[:-1] if len(coords) > 3 and coords[0] == coords[-1] else coords


def _round_geo(point: tuple[float, float]) -> tuple[float, float]:
    return (round(point[0], 7), round(point[1], 7))


def _busiest_area(mower_map: MowerMap, track: LocalPoints) -> str:
    """The map area with the most track points (the first area without a track)."""
    if not mower_map.areas:
        raise NotFoundError("the mower's map has no areas")

    best = max(
        mower_map.areas,
        key=lambda a: sum(1 for p in track if Polygon(a.boundary).covers(Point(p))),
    )
    return best.id
