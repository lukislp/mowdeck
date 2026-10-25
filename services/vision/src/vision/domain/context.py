"""The scene context the vision model gets next to the images (pure, no I/O).

Geometry is given in the **mower frame**: ``forward`` metres ahead of the mower and ``left``
metres to its left, so the model can plan maneuvers without converting map coordinates. Only
the map around the mower (``radius_m``) is included: areas, transfer paths, no-go zones,
obstacles and the dock with its distance and bearing. ``dock_route`` says whether the generic
return to dock fits (:mod:`vision.domain.dock`), ``job`` which mowing job the mower was running
and how it continues (:mod:`vision.domain.job`), ``recent_track`` where the mower was shortly
before. No account, user or mower names are sent (area names are the user's own labels).
"""

import math
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from mowdeck_contracts.v1 import (
    LocalPoint,
    MowerActivity,
    MowerMap,
    MowerState,
    MowPath,
    VisionPhotoProblem,
    VisionTask,
)
from mowdeck_control.geo import distance, from_wgs84, local_angle, point_in_polygon
from pydantic import BaseModel, Field

from vision.domain.dock import DockRoute, dock_route
from vision.domain.job import JobContext, job_context
from vision.domain.models import MAX_STEP_SECONDS, MAX_STEPS

TRACK_POINTS = 10
"""At most this many recent positions go into the context (newest last)."""


class Pose(BaseModel):
    """Where the mower is in the map's local frame (metres) and where it faces."""

    x: float
    y: float
    theta_rad: float | None = Field(
        default=None, description="Local direction, counter-clockwise from +x; None if unknown."
    )


class MowerSnapshot(BaseModel):
    """The last state of the mower."""

    activity: str
    error_code: str | None = None
    error_message: str | None = None
    drive_recoverable: bool | None = Field(
        default=None,
        description="Only with activity `error`: whether driving may free the mower.",
    )
    sensor_stop: bool = Field(
        default=False,
        description="A lift, tilt or other safety stop is active: nothing may move the mower.",
    )
    lifted: bool = False
    battery_percent: int
    rtk_fix: str
    heading_known: bool
    seconds_old: float = Field(description="Age of the state when the context was built.")


class Shape(BaseModel):
    """A map polygon in the mower frame."""

    kind: Literal["area", "no_go_zone", "obstacle", "path"]
    id: str
    name: str | None = None
    points: list[tuple[float, float]] = Field(
        description="(forward, left) in metres; a polygon, for `path` a line."
    )
    contains_mower: bool = False


class DockHint(BaseModel):
    """The charging station seen from the mower."""

    forward: float
    left: float
    distance_m: float
    bearing_deg: float = Field(description="Angle to turn to face the dock, left positive.")


class MapExcerpt(BaseModel):
    """The map around the mower."""

    radius_m: float
    shapes: list[Shape] = Field(default_factory=list)
    dock: DockHint | None = None
    inside_area: str | None = None
    inside_no_go_zone: bool = False


class TrackPoint(BaseModel):
    """A recent position of the mower (with an RTK fix), in the mower frame."""

    forward: float
    left: float
    seconds_ago: float


class RecentCommand(BaseModel):
    """A command seen for the mower shortly before."""

    kind: str
    seconds_ago: float


class PreviousRound(BaseModel):
    """An earlier round of this assist session."""

    round: int
    situation: str | None = None
    action: str = Field(default="drive", description="`drive`, `return_to_dock` or `resume`.")
    steps: list[dict[str, float]] = Field(default_factory=list)
    outcome: str
    detail: str | None = None


class Motion(BaseModel):
    """How the normalised drive values translate to motion (typical values of the mower)."""

    linear_mps_at_1: float = Field(description="Speed in m/s at linear = 1.")
    turn_deg_per_s_at_1: float = Field(description="Turn rate in degrees/s at angular = 1.")
    reverse: bool = Field(
        default=True,
        description="The drive link can drive backwards (a negative `linear`); false over the "
        "Mammotion cloud.",
    )
    max_steps: int = MAX_STEPS
    max_step_seconds: float = MAX_STEP_SECONDS


class SceneContext(BaseModel):
    """Everything the model gets besides the images."""

    task: VisionTask
    mower: MowerSnapshot | None = None
    map: MapExcerpt | None = None
    dock_route: DockRoute | None = None
    job: JobContext | None = Field(
        default=None,
        description="The mowing job the mower was running (planned areas in order, done, "
        "current and next area, the connection to the next area); null: no job is known.",
    )
    recent_track: list[TrackPoint] = Field(
        default_factory=list, description="Recent positions with an RTK fix, oldest first."
    )
    recent_commands: list[RecentCommand] = Field(default_factory=list)
    last_drive: str | None = Field(
        default=None, description="How the last drive session of the mower ended, if known."
    )
    previous_rounds: list[PreviousRound] = Field(default_factory=list)
    motion: Motion
    images: int = Field(default=0, description="Number of images attached.")
    stereo: bool = Field(
        default=False,
        description=(
            "Images 1 and 2 are the left and right front camera of the same moment (a stereo "
            "pair; the camera spacing is not calibrated)."
        ),
    )
    map_image: bool = Field(
        default=False,
        description="The last image is a top-down rendering of the map around the mower.",
    )
    photo_problem: VisionPhotoProblem | None = Field(
        default=None,
        description="The mower's camera delivered no photo, and why (`weak_wifi`, "
        "`weak_mobile`, `offline`, `no_frame`): no image shows the scene around the mower; "
        "judge from the map and the context only.",
    )
    signal_dbm: int | None = Field(
        default=None, description="With a weak signal: the mower's signal in dBm."
    )


def pose_of(state: MowerState, mower_map: MowerMap | None) -> Pose | None:
    """The state's position in the map frame (None without a position or a map)."""
    if state.position is None or mower_map is None:
        return None
    x, y = from_wgs84(mower_map, state.position.lat, state.position.lon)
    heading = state.position.heading
    theta = local_angle(mower_map, heading) if heading is not None else None
    return Pose(x=x, y=y, theta_rad=theta)


def to_mower_frame(point: LocalPoint, pose: Pose) -> tuple[float, float]:
    """A map point as (forward, left) metres from the mower."""
    theta = pose.theta_rad or 0.0
    dx, dy = point[0] - pose.x, point[1] - pose.y
    forward = dx * math.cos(theta) + dy * math.sin(theta)
    left = -dx * math.sin(theta) + dy * math.cos(theta)
    return (round(forward, 2), round(left, 2))


def _near(points: list[LocalPoint], pose: Pose, radius: float) -> bool:
    if point_in_polygon((pose.x, pose.y), points):
        return True
    return any(distance(p, (pose.x, pose.y)) <= radius for p in points)


def map_excerpt(mower_map: MowerMap, pose: Pose, radius_m: float) -> MapExcerpt:
    """The shapes near the mower and the dock, in the mower frame."""
    excerpt = MapExcerpt(radius_m=radius_m)
    here = (pose.x, pose.y)
    groups = (
        ("area", mower_map.areas),
        ("no_go_zone", mower_map.no_go_zones),
        ("obstacle", mower_map.obstacles),
    )
    for kind, shapes in groups:
        for shape in shapes:
            points = list(shape.boundary)
            inside = point_in_polygon(here, points)
            if kind == "area" and inside and excerpt.inside_area is None:
                excerpt.inside_area = shape.id
            if kind != "area" and inside:
                excerpt.inside_no_go_zone = True
            if _near(points, pose, radius_m):
                excerpt.shapes.append(
                    Shape(
                        kind=kind,  # type: ignore[arg-type]
                        id=shape.id,
                        name=shape.name,
                        points=[to_mower_frame(p, pose) for p in points],
                        contains_mower=inside,
                    )
                )
    for path in mower_map.paths:
        line = list(path.points)
        if any(distance(p, here) <= radius_m for p in line):
            excerpt.shapes.append(
                Shape(
                    kind="path",
                    id=path.id,
                    name=path.name,
                    points=[to_mower_frame(p, pose) for p in line],
                )
            )
    dock = mower_map.dock
    if dock is not None:
        forward, left = to_mower_frame((dock.x, dock.y), pose)
        excerpt.dock = DockHint(
            forward=forward,
            left=left,
            distance_m=round(math.hypot(forward, left), 2),
            bearing_deg=round(math.degrees(math.atan2(left, forward)), 1),
        )
    return excerpt


def build_context(
    task: VisionTask,
    *,
    state: MowerState | None,
    state_age_seconds: float,
    mower_map: MowerMap | None,
    commands: list[tuple[str, datetime]],
    now: datetime,
    last_drive: str | None,
    previous_rounds: list[PreviousRound],
    motion: Motion,
    images: int,
    radius_m: float = 8.0,
    stereo: bool = False,
    track: list[tuple[float, float, datetime]] | None = None,
    map_image: bool = False,
    mow_path: MowPath | None = None,
    last_start: Sequence[str] | None = None,
) -> SceneContext:
    """Assemble the context from what the service knows about the mower; ``track`` are recent
    WGS84 positions ``(lat, lon, observed_at)``, oldest first; ``mow_path`` is the route of the
    running job (the gateway's path snapshot) and ``last_start`` the areas of the newest
    ``start_mow``."""
    snapshot = None
    excerpt = None
    pose = None
    if state is not None:
        in_error = state.activity is MowerActivity.ERROR
        lifted = state.raw.get("lifted") is True
        snapshot = MowerSnapshot(
            activity=str(state.activity),
            error_code=state.error_code,
            error_message=state.error_message,
            drive_recoverable=state.drive_recoverable if in_error else None,
            sensor_stop=lifted or (in_error and not state.drive_recoverable),
            lifted=lifted,
            battery_percent=state.battery_percent,
            rtk_fix=str(state.rtk_fix),
            heading_known=state.position is not None and state.position.heading is not None,
            seconds_old=round(max(0.0, state_age_seconds), 1),
        )
        pose = pose_of(state, mower_map)
        if pose is not None and mower_map is not None:
            excerpt = map_excerpt(mower_map, pose, radius_m)
    route = None
    points: list[TrackPoint] = []
    if mower_map is not None:
        route = dock_route(mower_map, (pose.x, pose.y) if pose else None, state)
        if pose is not None:
            for lat, lon, at in (track or [])[-TRACK_POINTS:]:
                forward, left = to_mower_frame(from_wgs84(mower_map, lat, lon), pose)
                age = round(max(0.0, (now - at).total_seconds()), 1)
                points.append(TrackPoint(forward=forward, left=left, seconds_ago=age))
    job = job_context(
        state,
        mower_map=mower_map,
        here=(pose.x, pose.y) if pose else None,
        theta_rad=pose.theta_rad if pose else None,
        path=mow_path,
        last_start=last_start,
    )
    recent = [
        RecentCommand(kind=kind, seconds_ago=round(max(0.0, (now - at).total_seconds()), 1))
        for kind, at in commands[-10:]
    ]
    return SceneContext(
        task=task,
        mower=snapshot,
        map=excerpt,
        dock_route=route,
        job=job,
        recent_track=points,
        recent_commands=recent,
        last_drive=last_drive,
        previous_rounds=previous_rounds,
        motion=motion,
        images=images,
        stereo=stereo,
        map_image=map_image,
    )
