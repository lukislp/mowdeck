"""The mowing job a stuck mower was running, as the vision model sees it (pure, no I/O).

A stuck mower in the middle of a multi-area job is often best helped by continuing that job
(``resume``) instead of sending it home. The model can only judge that when it knows the job:
whether one runs or is paused by the error, which areas it plans in which order, which are done,
which area comes next, and whether the mower stands at the entry of the transfer path (channel)
that leads there.

Sources (the authoritative one first):

* **The mower's own route** of the running job, the Device-Gateway's path snapshot
  ``device.mower.<id>.path`` (:class:`~mowdeck_contracts.v1.MowPath`): the polylines in driving
  order with their kinds, the areas in order and the progress along the route. The gateway
  publishes it while the mower works on a job (also while paused) and clears it when the job
  ended, so it says which job the mower itself will continue on ``resume_mow``.
* **The mower state** (Device-Gateway telemetry): activity and error (running, paused, paused by
  an error, returning), ``job`` (the mower's own report of the job: the area it works in and the
  progress) and ``spot_run`` for a spot run MowDeck drives (ADR 0039; ``resume_mow`` cannot
  continue those).
* **The newest ``start_mow``** seen on ``command.mower.<id>`` (Fleet publishes every command,
  whoever issued it: the web app, the Scheduler, Home Assistant): the area order when no route
  is known yet. Areas before the current one then count as done (``inferred``).

Areas are matched to the route's polylines geometrically (most points inside the area); the
current area is the one the mower reports (``MowerState.job.area_id``), else the planned area
that holds the mower, else the one of the route position. The
connection to the next area is a mapped transfer path that touches both areas (its end at the
current area is the entry), else the route's next ``transfer`` line.
"""

import math
from collections.abc import Sequence
from typing import Final, Literal

from mowdeck_contracts.v1 import (
    LocalPoint,
    MapArea,
    MowerActivity,
    MowerMap,
    MowerState,
    MowPath,
)
from mowdeck_control.geo import distance, point_in_polygon
from pydantic import BaseModel, Field

from vision.domain.dock import PATH_JOIN_M, hardware_fault, polygon_distance

ENTRY_NEAR_M: Final = 3.0
"""The mower counts as standing at the entry of a connection within this distance."""
ENTRY_AHEAD_DEG: Final = 60.0
"""An entry within this angle of the heading counts as ahead of the mower."""
_AREA_MATCH_M: Final = 0.5
"""A route point this close to an area's boundary counts for that area (stripes at the edge)."""

JobStatus = Literal["running", "paused", "paused_by_error", "returning", "spot_run"]
JobSource = Literal["mow_path", "start_command", "state", "spot_run"]
AreaStatus = Literal["done", "current", "next", "planned"]


class JobArea(BaseModel):
    """One planned area of the job, in driving order."""

    id: str
    name: str | None = None
    status: AreaStatus


class AreaConnection(BaseModel):
    """How the current area connects to the next planned area, seen from the mower."""

    source: Literal["map_path", "job_route"] = Field(
        description="`map_path`: a mapped transfer path (channel) touching both areas; "
        "`job_route`: the transfer line of the mower's own route."
    )
    path_id: str | None = None
    path_name: str | None = None
    from_area: str = Field(description="The current area (name or id).")
    to_area: str = Field(description="The next planned area (name or id).")
    entry_forward: float = Field(description="Entry of the connection, metres ahead.")
    entry_left: float = Field(description="Entry of the connection, metres to the left.")
    entry_distance_m: float
    entry_bearing_deg: float = Field(
        description="Angle to turn to face the entry, left positive (0: straight ahead)."
    )
    mower_at_entry: bool = Field(
        description=f"The mower stands within {ENTRY_NEAR_M:g} m of the entry."
    )
    entry_ahead: bool = Field(
        description=f"The entry lies within {ENTRY_AHEAD_DEG:g} degrees of the heading "
        "(false when the heading is unknown)."
    )


class JobContext(BaseModel):
    """The mowing job of the mower."""

    status: JobStatus = Field(
        description="`running`, `paused` (resume_mow continues it), `paused_by_error` (the job "
        "stopped with the current error; it continues once the error cleared), `returning`, "
        "`spot_run` (driven by MowDeck; resume_mow cannot continue it)."
    )
    source: JobSource = Field(
        description="`mow_path`: the mower's own route of the job; `start_command`: the "
        "newest start (done areas inferred); `state`: only the activity is known."
    )
    areas: list[JobArea] = Field(default_factory=list, description="Planned areas, in order.")
    current_area: str | None = Field(default=None, description="The area being mowed.")
    current_area_finished: bool = Field(
        default=False, description="The route of the current area is done."
    )
    next_area: str | None = Field(default=None, description="The next planned area.")
    progress_percent: int | None = Field(
        default=None, description="Share of the route done, when the mower reports it."
    )
    error_code: str | None = Field(default=None, description="The error that paused the job.")
    resumable: bool = Field(
        description="`resume` may continue this job (after an optional short drive)."
    )
    resume_note: str = Field(description="Why `resume` fits or not.")
    connection_to_next: AreaConnection | None = None


def _label(area: MapArea | None, area_id: str) -> str:
    return (area.name or area.id) if area is not None else area_id


def _near_area(point: LocalPoint, area: MapArea) -> bool:
    return polygon_distance(point, area.boundary) <= _AREA_MATCH_M


def _polyline_area(points: Sequence[LocalPoint], candidates: Sequence[MapArea]) -> str | None:
    """The area holding most points of the polyline (None when none holds any)."""
    best: tuple[int, str] | None = None
    for area in candidates:
        inside = sum(1 for p in points if _near_area(p, area))
        if inside and (best is None or inside > best[0]):
            best = (inside, area.id)
    return best[1] if best else None


def _status(state: MowerState) -> JobStatus | None:
    if state.spot_run is not None and not state.spot_run.final:
        return "spot_run"
    return {
        MowerActivity.MOWING: "running",
        MowerActivity.PAUSED: "paused",
        MowerActivity.ERROR: "paused_by_error",
        MowerActivity.RETURNING: "returning",
    }.get(state.activity)  # type: ignore[return-value]


def resume_problem(job: "JobContext | None", state: MowerState | None) -> str | None:
    """Why ``resume`` cannot continue the job now (None: it can, after the error cleared)."""
    if state is None:
        return "no state of the mower is known"
    if job is None:
        return "no mowing job of the mower is known"
    if job.status == "spot_run":
        return "a spot run is driven by MowDeck and cannot be resumed; start it again"
    if job.status == "running":
        return "the job is running"
    if job.status == "returning":
        return "the mower is returning to the dock"
    fault = hardware_fault(state)
    if fault is not None:
        return fault
    return None


def job_context(
    state: MowerState | None,
    *,
    mower_map: MowerMap | None,
    here: LocalPoint | None,
    theta_rad: float | None,
    path: MowPath | None,
    last_start: Sequence[str] | None,
) -> JobContext | None:
    """The job of the mower from the state, its route (``path``, the gateway's snapshot) and
    the newest start's areas; None when no job is known. ``here`` and ``theta_rad`` are the
    mower's pose in the map frame (None: unknown)."""
    if state is None:
        return None
    status = _status(state)
    if status is None:
        return None
    if status == "spot_run":
        return JobContext(
            status="spot_run",
            source="spot_run",
            resumable=False,
            resume_note="a spot run is driven by MowDeck and cannot be resumed; start it again",
            progress_percent=state.spot_run.progress_percent if state.spot_run else None,
        )
    order: list[str] = []
    source: JobSource = "state"
    if path is not None and path.area_ids:
        order, source = list(dict.fromkeys(path.area_ids)), "mow_path"
    elif last_start:
        order, source = list(dict.fromkeys(last_start)), "start_command"
    reported = state.job
    if status == "paused_by_error" and source == "state" and reported is None:
        return None  # an error without any job evidence: nothing to continue
    areas = {a.id: a for a in mower_map.areas} if mower_map is not None else {}
    planned = [areas[a] for a in order if a in areas]
    line_areas: list[str | None] = []
    done: set[str] = set()
    index: int | None = None
    if path is not None and source == "mow_path":
        kinds = path.kinds or ["other"] * len(path.polylines)
        line_areas = [
            None if kind == "transfer" else _polyline_area(line, planned)
            for line, kind in zip(path.polylines, kinds, strict=True)
        ]
        if path.progress is not None:
            index = path.progress.index
            start = 0
            ends: dict[str, int] = {}
            for line, area_id in zip(path.polylines, line_areas, strict=True):
                start += len(line)
                if area_id is not None:
                    ends[area_id] = start
            done = {a for a, end in ends.items() if end <= index}
    own = reported.area_id if reported is not None else None
    if own is not None and (own in order or (not order and own in areas)):
        current: str | None = own
    else:
        current = _current_area(here, planned, path, line_areas, index)
    if index is None and current in order:
        done = set(order[: order.index(current)])  # inferred from the order
    finished = current is not None and current in done
    following = order[order.index(current) + 1 :] if current in order else order
    upcoming = next((a for a in following if a not in done and a != current), None)
    job_areas = [
        JobArea(
            id=a,
            name=areas[a].name if a in areas else None,
            status=(
                "current"
                if a == current
                else "next"
                if a == upcoming
                else "done"
                if a in done
                else "planned"
            ),
        )
        for a in order
    ]
    progress = None
    if path is not None and path.progress is not None and path.points_total:
        progress = round(100 * min(path.progress.index, path.points_total) / path.points_total)
    elif reported is not None:
        progress = reported.progress_percent
    connection = None
    if current is not None and upcoming is not None and here is not None:
        connection = _connection(
            mower_map, here, theta_rad, current, upcoming, areas, path, line_areas
        )
    job = JobContext(
        status=status,
        source=source,
        areas=job_areas,
        current_area=_label(areas.get(current), current) if current else None,
        current_area_finished=finished,
        next_area=_label(areas.get(upcoming), upcoming) if upcoming else None,
        progress_percent=progress,
        error_code=state.error_code if status == "paused_by_error" else None,
        resumable=False,
        resume_note="",
        connection_to_next=connection,
    )
    problem = resume_problem(job, state)
    if problem is not None:
        note = problem
    elif status == "paused_by_error":
        note = (
            "the job is paused by the error; resume continues it once the error cleared (a "
            "short drive first clears a stuck error)"
        )
    else:
        note = "the job is paused; resume continues it"
    return job.model_copy(update={"resumable": problem is None, "resume_note": note})


def _current_area(
    here: LocalPoint | None,
    planned: Sequence[MapArea],
    path: MowPath | None,
    line_areas: Sequence[str | None],
    index: int | None,
) -> str | None:
    """The planned area holding the mower, else the area of the route position."""
    if here is not None:
        for area in planned:
            if point_in_polygon(here, area.boundary):
                return area.id
        near = [a for a in planned if polygon_distance(here, a.boundary) <= PATH_JOIN_M]
        if near:
            return min(near, key=lambda a: polygon_distance(here, a.boundary)).id
    if path is None or index is None:
        return None
    start = 0
    last: str | None = None
    for line, area_id in zip(path.polylines, line_areas, strict=True):
        if area_id is not None and start <= index:
            last = area_id
        start += len(line)
        if index < start:
            break
    return last


def _connection(
    mower_map: MowerMap | None,
    here: LocalPoint,
    theta_rad: float | None,
    current: str,
    upcoming: str,
    areas: dict[str, MapArea],
    path: MowPath | None,
    line_areas: Sequence[str | None],
) -> AreaConnection | None:
    """The entry of the way from ``current`` to ``upcoming``: a mapped path touching both,
    else the route's transfer line between them."""
    origin, target = areas.get(current), areas.get(upcoming)
    entry: LocalPoint | None = None
    found: tuple[str | None, str | None, Literal["map_path", "job_route"]] | None = None
    if mower_map is not None and origin is not None and target is not None:
        best: tuple[float, LocalPoint, str, str | None] | None = None
        for candidate in mower_map.paths:
            points = list(candidate.points)
            touches_origin = any(
                polygon_distance(p, origin.boundary) <= PATH_JOIN_M for p in points
            )
            touches_target = any(
                polygon_distance(p, target.boundary) <= PATH_JOIN_M for p in points
            )
            if not (touches_origin and touches_target):
                continue
            ends = sorted(
                (points[0], points[-1]),
                key=lambda p: (polygon_distance(p, origin.boundary), distance(p, here)),
            )
            point = ends[0]
            score = distance(point, here)
            if best is None or score < best[0]:
                best = (score, point, candidate.id, candidate.name)
        if best is not None:
            entry, found = best[1], (best[2], best[3], "map_path")
    if entry is None and path is not None and path.kinds is not None:
        seen_current = False
        for line, kind, area_id in zip(path.polylines, path.kinds, line_areas, strict=True):
            if area_id == current:
                seen_current = True
            elif seen_current and kind == "transfer":
                entry, found = line[0], (None, None, "job_route")
                break
            elif seen_current and area_id is not None:
                break
    if entry is None or found is None:
        return None
    dx, dy = entry[0] - here[0], entry[1] - here[1]
    theta = theta_rad or 0.0
    forward = dx * math.cos(theta) + dy * math.sin(theta)
    left = -dx * math.sin(theta) + dy * math.cos(theta)
    bearing = math.degrees(math.atan2(left, forward))
    gap = math.hypot(dx, dy)
    return AreaConnection(
        source=found[2],
        path_id=found[0],
        path_name=found[1],
        from_area=_label(origin, current),
        to_area=_label(target, upcoming),
        entry_forward=round(forward, 2),
        entry_left=round(left, 2),
        entry_distance_m=round(gap, 2),
        entry_bearing_deg=round(bearing, 1),
        mower_at_entry=gap <= ENTRY_NEAR_M,
        entry_ahead=theta_rad is not None and abs(bearing) <= ENTRY_AHEAD_DEG,
    )
