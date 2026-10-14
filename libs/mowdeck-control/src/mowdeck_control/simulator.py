"""A deterministic simulated mower, so Phase 1 needs no hardware.

:class:`SimulatedMower` implements :class:`~mowdeck_control.connection.MowerConnection`. It is a
state machine advanced by :meth:`SimulatedMower.tick`; nothing happens between ticks, so tests
are exact and never sleep. :meth:`SimulatedMower.run` ticks in real time for the Device-Gateway
and the demo.

What it simulates:

* activity: docked, mowing, paused, returning, docked, charging, plus idle and error;
* a default map with two areas, a no-go zone, a transfer path and a dock;
* stripe paths inside the selected areas (blades off on every transfer), at the set speed;
* spot plans (``StartMow.spot_plan``): it follows the segments of a Planner route with the
  blades and speed of each segment (``custom_paths`` is True for the simulator);
* battery drain while mowing and driving, charging at the dock, return on low battery;
* RTK fix and correction source (test hook :meth:`SimulatedMower.set_rtk`);
* error injection and a lift sensor that stops everything and cannot be cleared by a command;
* getting stuck (:meth:`SimulatedMower.inject_stuck`, or after ``stuck_after_seconds`` of
  mowing): ``error`` with code ``stuck`` and ``drive_recoverable``; the wheels only move
  backwards until a backward move of at least ``unstuck_backward_m`` frees the mower;
* manual driving through a :class:`SimulatedDriveSession`;
* a camera: :meth:`SimulatedMower.get_camera_session` returns a session with ``provider``
  ``fake`` (no video; the web app draws a test pattern) and two front cameras (``front_left``
  uid 1, ``front_right`` uid 2, a stereo pair like a LUBA 2 AWD X), so the camera path runs end
  to end;
* still images of its surroundings drawn from the map (:meth:`SimulatedMower.capture_photo`);
* a slow job start (``SimulationConfig.start_delay_s``): ``StartMow`` and ``ResumeMow`` are sent
  at once and the mower reports ``mowing`` only after the delay, confirmed through the state like
  a real mower (:class:`~mowdeck_control.connection.TwoPhaseExecution`), so the controller's
  interrupted-confirmation path runs without hardware;
* a light (``SetLight``, capability ``light``) that keeps its state in ``light_on``; switching
  it works in every state, also while stuck or lifted;
* its own planned route (:class:`~mowdeck_control.connection.MowPathSource`): every leg of the
  job is one point of the path, split into polylines at the transfers, and the progress counts
  the legs done; :meth:`SimulatedMower.preview_path` plans the same route without starting
  (:class:`~mowdeck_control.connection.PathPreviewCapable`).

``SimulationConfig.time_scale`` makes simulated time run faster than real time.
"""

import asyncio
import itertools
import logging
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from mowdeck_contracts.v1 import (
    CameraAccess,
    CameraInfo,
    CameraPlacement,
    CameraProvider,
    CameraSession,
    ConnectionKind,
    DockPose,
    EmergencyStop,
    GeoReference,
    LocalPoint,
    MapArea,
    MapPath,
    MapZone,
    MowerActivity,
    MowerCapabilities,
    MowerMap,
    MowerState,
    MowPath,
    MowPathKind,
    MowPathProgress,
    PauseMow,
    PositionSource,
    ResumeMow,
    ReturnToDock,
    RtkFixStatus,
    RtkSource,
    SetBladeHeight,
    SetLight,
    SetSpeed,
    SpotMowPlan,
    StartMow,
    utc_now,
)

from mowdeck_control.broadcast import StateBroadcaster, StateSubscription
from mowdeck_control.connection import ConnectionConfig, Dispatch, JobCommand, Outcome, Photo
from mowdeck_control.errors import CommandRejected, NotConnected, SensorStop, UnsupportedPattern
from mowdeck_control.geo import distance, point_in_polygon, to_geo_position
from mowdeck_control.paths import stripe_path
from mowdeck_control.photo import render_scene
from mowdeck_control.translation import MowRequest, NativeJob, Pattern, plan_to_native_job

_log = logging.getLogger("mowdeck_control.simulator")

DEFAULT_MOWER_ID: Final = UUID("00000000-0000-4000-8000-00000000000a")
DEFAULT_ACCOUNT_ID: Final = UUID("00000000-0000-4000-8000-00000000000b")
LIFT_ERROR_CODE: Final = "lifted"
SPOT_AREA_ID: Final = "spot"
"""``raw.area_id`` and ``raw.job_areas`` while the simulator follows a spot plan."""
STUCK_ERROR_CODE: Final = "stuck"
NIGHT_BRIGHTNESS: Final = 0.12
"""Photo brightness at night (``SimulationConfig.night``) with the light off."""
NIGHT_LIT_BRIGHTNESS: Final = 0.8
"""Photo brightness at night with the light on."""
_ARRIVAL_EPS = 1e-6
_POSITION_SOURCE: Final = {
    RtkFixStatus.FIXED: PositionSource.RTK,
    RtkFixStatus.FLOAT: PositionSource.RTK,
    RtkFixStatus.NONE: PositionSource.GNSS,
}
"""The simulated position is the RTK solution; without a fix it counts as plain GNSS."""
_DRIVE_STEP_S = 0.1
_PATH_ACTIVITIES: Final = frozenset(
    {MowerActivity.MOWING, MowerActivity.PAUSED, MowerActivity.RETURNING}
)
"""Activities in which the route of the job stays shown."""


DEFAULT_ORIGIN: Final = (48.137154, 11.576124)
"""WGS84 origin of the demo garden (latitude, longitude)."""


def default_map(origin: tuple[float, float] = DEFAULT_ORIGIN) -> MowerMap:
    """The simulator's garden: ``front`` (12 x 8 m) with a flower bed no-go zone in its corner,
    ``back`` (10 x 6 m) behind it, a transfer path between both and the dock left of ``front``.
    ``origin`` places the garden on the earth (several simulated sites on one fleet map).
    """
    return MowerMap(
        reference=GeoReference(origin_lat=origin[0], origin_lon=origin[1], rotation_deg=12.5),
        areas=[
            MapArea(id="front", name="Front lawn", boundary=[(0, 0), (12, 0), (12, 8), (0, 8)]),
            MapArea(id="back", name="Back lawn", boundary=[(0, 11), (10, 11), (10, 17), (0, 17)]),
        ],
        no_go_zones=[
            MapZone(
                id="flowerbed", name="Flower bed", boundary=[(9, 5.5), (12, 5.5), (12, 8), (9, 8)]
            )
        ],
        paths=[MapPath(id="side-path", name="Side path", points=[(1, 8), (1, 11)])],
        dock=DockPose(x=-1.0, y=1.0, heading_deg=0.0),
        revision="sim-1",
    )


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    """Tunable behaviour of the simulator. Rates are per simulated hour."""

    time_scale: float = 1.0
    """Simulated seconds per real second passed to :meth:`SimulatedMower.tick`."""
    mowing_speed_mps: float = 0.4
    transfer_speed_mps: float = 0.6
    drive_max_speed_mps: float = 0.5
    drive_max_turn_rad_s: float = 1.0
    margin_m: float = 0.2
    battery_percent: float = 100.0
    drain_mowing_per_h: float = 30.0
    drain_moving_per_h: float = 15.0
    drain_idle_per_h: float = 0.5
    charge_per_h: float = 60.0
    low_battery_percent: float = 15.0
    min_start_battery_percent: float = 20.0
    blade_height_mm: int = 40
    manual_drive_blades: bool = True
    custom_paths: bool = True
    """Follow spot plans (``StartMow.spot_plan``); False behaves like a mower without own
    routes (for example the LUBA), which only runs plans with a native job."""
    firmware: str = "sim-1.0.0"
    model: str = "MowDeck Simulator"
    origin_lat: float | None = None
    """Latitude of the demo garden's origin (with ``origin_lon``); default: Munich."""
    origin_lon: float | None = None
    """Longitude of the demo garden's origin (with ``origin_lat``)."""
    camera: bool = True
    """Offer a fake camera (capability ``cloud``, sessions with provider ``fake``)."""
    camera_session_seconds: float = 300.0
    """Lifetime of a fake camera session (short values exercise the client's refresh)."""
    stuck_after_seconds: float | None = None
    """Development only: get stuck after this many simulated seconds of mowing in a job (once
    per job), so AI assist can be demonstrated. None: never."""
    unstuck_backward_m: float = 0.3
    """Backward distance that frees a stuck mower."""
    start_delay_s: float | None = None
    """Simulated seconds between a ``StartMow`` or ``ResumeMow`` and the mower reporting
    ``mowing``. None: at once (the command returns ``executed``). With a delay the command is
    sent and confirmed through the state within ``confirm_timeout_s``, otherwise it is
    ``accepted``; ``math.inf`` never starts (a confirmation that never arrives). A pause, return
    or emergency stop before the start cancels it."""
    confirm_timeout_s: float = 30.0
    """Real seconds a delayed start waits for its confirmation (see ``start_delay_s``)."""
    light: bool = True
    """Offer a switchable light (capability ``light``)."""
    night: bool = False
    """Take dark photos (brightness ``NIGHT_BRIGHTNESS``) unless the light is on
    (``NIGHT_LIT_BRIGHTNESS``), so AI assist's light handling can be tried without hardware."""

    @property
    def origin(self) -> tuple[float, float]:
        """The garden's WGS84 origin."""
        if self.origin_lat is None or self.origin_lon is None:
            return DEFAULT_ORIGIN
        return (self.origin_lat, self.origin_lon)

    def __post_init__(self) -> None:
        if self.time_scale <= 0:
            msg = "time_scale must be positive"
            raise ValueError(msg)
        if not 0 <= self.battery_percent <= 100:
            msg = "battery_percent must be within 0..100"
            raise ValueError(msg)
        if not 10 <= self.camera_session_seconds <= 86_400:
            msg = "camera_session_seconds must be within 10..86400"
            raise ValueError(msg)
        if (self.origin_lat is None) != (self.origin_lon is None):
            msg = "origin_lat and origin_lon go together"
            raise ValueError(msg)
        if self.origin_lat is not None and not -90 <= self.origin_lat <= 90:
            msg = "origin_lat must be within -90..90"
            raise ValueError(msg)
        if self.origin_lon is not None and not -180 <= self.origin_lon <= 180:
            msg = "origin_lon must be within -180..180"
            raise ValueError(msg)
        if self.stuck_after_seconds is not None and self.stuck_after_seconds <= 0:
            msg = "stuck_after_seconds must be positive"
            raise ValueError(msg)
        if self.unstuck_backward_m <= 0:
            msg = "unstuck_backward_m must be positive"
            raise ValueError(msg)
        if self.start_delay_s is not None and self.start_delay_s < 0:
            msg = "start_delay_s must not be negative"
            raise ValueError(msg)
        if self.confirm_timeout_s <= 0:
            msg = "confirm_timeout_s must be positive"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class _Leg:
    target: LocalPoint
    blade: bool
    area_id: str | None = None
    speed: float | None = None
    """Speed of this leg (spot plans); None: the mowing or transfer speed."""


@dataclass(slots=True)
class _Job:
    area_ids: tuple[str, ...]
    pattern: str
    mow_length_m: float
    mowed_m: float = 0.0
    plan_hash: str | None = None
    mowing_s: float = 0.0
    stuck_done: bool = False


@dataclass(slots=True)
class _PendingStart:
    """A start or resume the simulated mower received but has not begun yet."""

    remaining_s: float
    begin: Callable[[], None]


@dataclass(slots=True)
class _DriveCommand:
    linear: float = 0.0
    angular: float = 0.0
    blade_on: bool = False


@dataclass(slots=True)
class _Status:
    activity: MowerActivity = MowerActivity.DOCKED
    resume_activity: MowerActivity | None = None
    error_code: str | None = None
    error_message: str | None = None
    emergency_stopped: bool = False
    legs: list[_Leg] = field(default_factory=list)


class SimulatedDriveSession:
    """Manual drive session of a :class:`SimulatedMower`."""

    def __init__(self, mower: "SimulatedMower") -> None:
        self._mower = mower
        self._open = True

    @property
    def is_open(self) -> bool:
        return self._open

    async def update(self, linear: float, angular: float, blade_on: bool) -> None:
        if not self._open:
            raise CommandRejected("drive session is closed")
        self._mower._drive_update(linear, angular, blade_on)

    async def stop(self) -> None:
        self._mower._drive_stop()

    async def close(self) -> None:
        if self._open:
            self._mower._drive_stop()
            self._open = False
            self._mower._drive_closed(self)

    def _end(self) -> None:
        self._open = False


class SimulatedMower:
    """A simulated mower behind the :class:`~mowdeck_control.connection.MowerConnection`
    interface."""

    def __init__(
        self,
        *,
        mower_id: UUID = DEFAULT_MOWER_ID,
        account_id: UUID = DEFAULT_ACCOUNT_ID,
        config: SimulationConfig | None = None,
        mower_map: MowerMap | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.mower_id = mower_id
        self.account_id = account_id
        self.config = config or SimulationConfig()
        self._map = mower_map or default_map(self.config.origin)
        if self._map.dock is None:
            msg = "the simulated map needs a dock"
            raise ValueError(msg)
        self._clock = clock
        self._broadcaster = StateBroadcaster()
        self._connected = False
        self._status = _Status()
        self._job: _Job | None = None
        dock = self._map.dock
        self._x, self._y = dock.x, dock.y
        self._theta = math.radians(dock.heading_deg)
        self._battery = self.config.battery_percent
        self._blade_on = False
        self._moving_speed = 0.0
        self._blade_height = self.config.blade_height_mm
        self._mowing_speed = self.config.mowing_speed_mps
        self._rtk_fix = RtkFixStatus.FIXED
        self._rtk_source = RtkSource.BASE_STATION
        self._elapsed_s = 0.0
        self._drive: SimulatedDriveSession | None = None
        self._drive_cmd = _DriveCommand()
        self._backed_off_m = 0.0
        self._pending_start: _PendingStart | None = None
        self._path: MowPath | None = None
        self._path_done = 0
        self._changed = asyncio.Event()
        self._light_on = False
        self._capabilities = MowerCapabilities(
            model=self.config.model,
            native_patterns=frozenset({Pattern.STRIPES, Pattern.DIAGONAL, Pattern.CHECKERBOARD}),
            manual_drive=True,
            manual_drive_blades=self.config.manual_drive_blades,
            custom_paths=self.config.custom_paths,
            camera=CameraAccess.CLOUD if self.config.camera else CameraAccess.NONE,
            light=self.config.light,
            rtk_sources=frozenset({RtkSource.BASE_STATION, RtkSource.NETWORK}),
            blade_height_range_mm=(20, 100),
            cutting_width_mm=300,
            min_speed_mps=0.1,
            max_speed_mps=1.0,
            connection_kinds=frozenset({ConnectionKind.SIMULATED}),
        )

    # --- MowerConnection ----------------------------------------------------------------------

    @property
    def kind(self) -> ConnectionKind:
        return ConnectionKind.SIMULATED

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        if not self._connected:
            self._connected = True
            self._publish()

    async def disconnect(self) -> None:
        if self._connected:
            self._end_drive_session()
            self._connected = False
            self._broadcaster.close()

    async def capabilities(self) -> MowerCapabilities:
        return self._capabilities

    async def get_state(self) -> MowerState:
        self._require_connected()
        return self.snapshot()

    def watch_state(self) -> StateSubscription:
        self._require_connected()
        return self._broadcaster.subscribe(initial=self.snapshot())

    async def get_map(self) -> MowerMap:
        self._require_connected()
        return self._map

    async def get_camera_session(self) -> CameraSession | None:
        """A fake camera session (``provider`` ``fake``); None when the camera is off."""
        self._require_connected()
        if not self.config.camera:
            return None
        return CameraSession(
            provider=CameraProvider.FAKE,
            app_id="",
            channel=f"sim-{self.mower_id}",
            token="",
            uid=0,
            # Like a LUBA 2 AWD X: two front cameras, a stereo pair (the web app draws each with
            # its own label, the right one mirrored, so the two are told apart).
            cameras=[
                CameraInfo(
                    id="front_left", name="Front left", uid=1, placement=CameraPlacement.FRONT_LEFT
                ),
                CameraInfo(
                    id="front_right",
                    name="Front right",
                    uid=2,
                    placement=CameraPlacement.FRONT_RIGHT,
                ),
            ],
            encryption=None,
            expires_at=self._clock() + timedelta(seconds=self.config.camera_session_seconds),
            quota=None,
        )

    async def execute(self, command: JobCommand) -> Outcome:
        return await (await self.dispatch(command)).settle()

    async def dispatch(self, command: JobCommand) -> Dispatch:
        """:class:`~mowdeck_control.connection.TwoPhaseExecution`: everything is final at once
        except a delayed start (``SimulationConfig.start_delay_s``), which waits for ``mowing``."""
        self._require_connected()
        if isinstance(command, EmergencyStop):
            self._pending_start = None
            return Dispatch.done(self._emergency_stop())
        if isinstance(command, SetLight):
            # The light works in every state: AI assist lights up a stuck mower at night.
            return Dispatch.done(self._set_light(on=command.on))
        if self.stuck:
            return Dispatch.done(Outcome.rejected("the mower is stuck: drive it free first"))
        self._require_no_sensor_stop()
        delayed = self.config.start_delay_s is not None
        match command:
            case StartMow():
                outcome = self._start_mow(command)
            case PauseMow():
                outcome = self._pause()
            case ResumeMow():
                outcome = self._resume()
            case ReturnToDock():
                self._pending_start = None
                outcome = self._return_to_dock(end_job=command.end_job)
            case SetBladeHeight():
                outcome = self._set_blade_height(command.mm)
            case SetSpeed():
                outcome = self._set_speed(command.mps)
            case _:
                return Dispatch.done(Outcome.rejected(f"{command.kind} is not supported here"))
        self._publish()
        if delayed and isinstance(command, StartMow | ResumeMow) and self._pending_start:
            return Dispatch.awaiting(f"{command.kind} sent", self._confirm_mowing)
        return Dispatch.done(outcome)

    async def _confirm_mowing(self) -> Outcome:
        """Wait (real time) until the simulated mower reports ``mowing``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.confirm_timeout_s
        while self._status.activity is not MowerActivity.MOWING:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return Outcome.accepted(
                    "command sent; the mower did not report mowing within "
                    f"{self.config.confirm_timeout_s:g} s"
                )
            self._changed.clear()
            try:
                async with asyncio.timeout(remaining):
                    await self._changed.wait()
            except TimeoutError:
                continue
        return Outcome.executed("mower is mowing")

    async def start_drive_session(self) -> SimulatedDriveSession:
        self._require_connected()
        self._require_no_sensor_stop()
        status = self._status
        if status.activity in (MowerActivity.MOWING, MowerActivity.RETURNING):
            raise CommandRejected("pause or end the job before driving manually")
        self._end_drive_session()
        if status.activity is MowerActivity.PAUSED:
            _log.info("manual drive cancels the paused job")
        self._job = None
        status.legs = []
        status.resume_activity = None
        status.emergency_stopped = False
        if not self.stuck:
            status.activity = MowerActivity.IDLE
        self._drive = SimulatedDriveSession(self)
        self._drive_cmd = _DriveCommand()
        self._publish()
        return self._drive

    # --- Simulation ---------------------------------------------------------------------------

    @property
    def elapsed_s(self) -> float:
        """Simulated seconds since construction."""
        return self._elapsed_s

    @property
    def local_position(self) -> LocalPoint:
        """Current position in the map's local frame (metres)."""
        return (self._x, self._y)

    @property
    def blade_on(self) -> bool:
        """Whether the blades are spinning."""
        return self._blade_on

    @property
    def stuck(self) -> bool:
        """True while the mower is stuck (an error manual driving may clear)."""
        status = self._status
        return status.activity is MowerActivity.ERROR and status.error_code == STUCK_ERROR_CODE

    @property
    def theta(self) -> float:
        """Current direction in the local frame (radians, counter-clockwise from +x)."""
        return self._theta

    def current_path(self) -> MowPath | None:
        """:class:`~mowdeck_control.connection.MowPathSource`: the route of the job with the legs
        done as progress; None without a job (it ends at the dock)."""
        path = self._path
        if path is None:
            return None
        if self._status.activity not in _PATH_ACTIVITIES:
            self._path = None
            return None
        index = min(self._path_done, path.points_total)
        return path.model_copy(update={"progress": MowPathProgress(index=index)})

    async def preview_path(
        self, area_ids: Sequence[str], pattern: Mapping[str, Any] | None
    ) -> MowPath:
        """:class:`~mowdeck_control.connection.PathPreviewCapable`: the route a start of
        ``area_ids`` with ``pattern`` would drive, without starting."""
        self._require_connected()
        if self._status.activity in _PATH_ACTIVITIES:
            msg = f"the mower is {self._status.activity.value}; pause or end the job first"
            raise CommandRejected(msg)
        try:
            command = StartMow(area_ids=list(area_ids), pattern=dict(pattern) if pattern else None)
        except ValueError as exc:
            raise CommandRejected("choose at least one area") from exc
        planned = self._plan_areas(command)
        if isinstance(planned, str):
            raise CommandRejected(planned)
        job, legs, _ = planned
        return self._route_path(legs, job.area_ids)

    async def capture_photo(self) -> Photo:
        """A still image of the surroundings drawn from the map (:class:`PhotoCapture`)."""
        self._require_connected()
        return render_scene(
            self._map,
            (self._x, self._y),
            self._theta,
            stuck=self.stuck,
            brightness=self._photo_brightness(),
        )

    def _photo_brightness(self) -> float:
        if not self.config.night:
            return 1.0
        return NIGHT_LIT_BRIGHTNESS if self._light_on else NIGHT_BRIGHTNESS

    def tick(self, dt: float) -> MowerState:
        """Advance the simulation by ``dt`` real seconds (times ``time_scale``) and publish."""
        if dt < 0:
            msg = "dt must not be negative"
            raise ValueError(msg)
        remaining = dt * self.config.time_scale
        self._elapsed_s += remaining
        pending = self._pending_start
        if pending is not None:
            pending.remaining_s -= remaining
            if pending.remaining_s <= 0:
                self._pending_start = None
                pending.begin()
        self._moving_speed = 0.0
        guard = 0
        while remaining > 1e-9:
            guard += 1
            if guard > 1_000_000:  # pragma: no cover - defensive, never reached
                break
            remaining -= self._advance(remaining)
        state = self.snapshot()
        self._changed.set()
        if self._connected:
            self._broadcaster.publish(state)
        return state

    async def run(
        self,
        interval: float = 0.5,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Tick every ``interval`` real seconds until cancelled."""
        if interval <= 0:
            msg = "interval must be positive"
            raise ValueError(msg)
        while True:
            await sleep(interval)
            self.tick(interval)

    def snapshot(self) -> MowerState:
        """The current state as a contract model."""
        status = self._status
        leg = status.legs[0] if status.legs else None
        raw: dict[str, Any] = {
            "x": round(self._x, 3),
            "y": round(self._y, 3),
            "blade_on": self._blade_on,
            "area_id": leg.area_id if leg is not None and leg.blade else None,
            "phase": self._phase(),
            "elapsed_s": round(self._elapsed_s, 3),
        }
        if self._job is not None:
            raw["job_areas"] = list(self._job.area_ids)
            raw["job_pattern"] = self._job.pattern
            raw["progress_percent"] = self._progress()
            if self._job.plan_hash is not None:
                raw["job_plan_hash"] = self._job.plan_hash
        if status.emergency_stopped:
            raw["emergency_stopped"] = True
        if status.activity is MowerActivity.ERROR and status.error_code == LIFT_ERROR_CODE:
            raw["lifted"] = True  # the generic lift flag consumers check (theft alarm)
        if self.stuck:
            raw["stuck"] = True
            raw["backed_off_m"] = round(self._backed_off_m, 3)
        return MowerState(
            mower_id=self.mower_id,
            account_id=self.account_id,
            observed_at=self._clock(),
            activity=status.activity,
            battery_percent=max(0, min(100, math.floor(self._battery))),
            charging=status.activity is MowerActivity.CHARGING,
            position=to_geo_position(self._map.reference, self._x, self._y, self._theta),
            rtk_fix=self._rtk_fix,
            rtk_source=self._rtk_source,
            position_source=_POSITION_SOURCE.get(self._rtk_fix, PositionSource.UNKNOWN),
            connection=ConnectionKind.SIMULATED,
            blade_height_mm=self._blade_height,
            speed_mps=round(self._moving_speed, 3),
            error_code=status.error_code,
            error_message=status.error_message,
            drive_recoverable=self.stuck,
            firmware=self.config.firmware,
            light_on=self._light_on if self.config.light else None,
            raw=raw,
        )

    # --- Test hooks ---------------------------------------------------------------------------

    def set_rtk(self, fix: RtkFixStatus, source: RtkSource | None = None) -> None:
        """Set the RTK fix (and optionally the correction source). Without a fix (``none``) the
        mower holds its position while mowing."""
        self._rtk_fix = fix
        if source is not None:
            self._rtk_source = source
        self._publish()

    def set_battery(self, percent: float) -> None:
        """Set the battery level."""
        self._battery = max(0.0, min(100.0, percent))
        self._publish()

    def inject_error(self, code: str, message: str | None = None) -> None:
        """Put the mower into an error state, as if the firmware reported ``code``."""
        self._enter_error(code, message or f"simulated error {code}")

    def inject_stuck(self, message: str | None = None) -> None:
        """Get stuck (wheel slip on an obstacle): ``error`` with code ``stuck`` and
        ``drive_recoverable``. Forward moves and turns do nothing until the mower was driven
        backwards by at least ``unstuck_backward_m``; then it is ``idle`` again."""
        self._backed_off_m = 0.0
        self._enter_error(STUCK_ERROR_CODE, message or "the mower is stuck on an obstacle")

    def lift(self) -> None:
        """Trigger the lift sensor: blades and wheels stop, the mower reports an error. Only
        :meth:`clear_error` (someone puts the mower down and confirms) ends it."""
        self._enter_error(LIFT_ERROR_CODE, "the mower was lifted")

    def clear_error(self) -> None:
        """Clear an error. A job that was running stays paused until resumed."""
        status = self._status
        if status.activity is not MowerActivity.ERROR:
            return
        status.error_code = None
        status.error_message = None
        if self._job is not None or status.legs:
            status.activity = MowerActivity.PAUSED
        else:
            dock = self._map.dock
            at_dock = dock is not None and distance((self._x, self._y), (dock.x, dock.y)) < 1e-3
            status.activity = MowerActivity.DOCKED if at_dock else MowerActivity.IDLE
            status.resume_activity = None
        self._publish()

    # --- Internals: commands ------------------------------------------------------------------

    def _require_connected(self) -> None:
        if not self._connected:
            raise NotConnected("simulated mower is not connected")

    def _require_no_sensor_stop(self) -> None:
        status = self._status
        if status.activity is MowerActivity.ERROR and not self.stuck:
            msg = f"sensor stop active: {status.error_code}"
            raise SensorStop(msg)

    def _emergency_stop(self) -> Outcome:
        status = self._status
        self._blade_on = False
        self._moving_speed = 0.0
        self._end_drive_session()
        if status.activity in (MowerActivity.MOWING, MowerActivity.RETURNING):
            status.resume_activity = status.activity
            status.activity = MowerActivity.PAUSED
        status.emergency_stopped = True
        self._publish()
        return Outcome.executed("blades and wheels stopped")

    def _start_mow(self, command: StartMow) -> Outcome:
        if command.plan_ref is not None:
            return Outcome.rejected("the simulator cannot resolve plan references")
        if command.spot_plan is not None:
            if not self._capabilities.custom_paths:
                return Outcome.rejected("custom paths not supported")
            return self._start_spot(command.spot_plan)
        unknown = [a for a in command.area_ids if self._map.area(a) is None]
        if unknown:
            return Outcome.rejected(f"unknown area ids: {', '.join(unknown)}")
        if self._battery < self.config.min_start_battery_percent:
            return Outcome.rejected("battery too low to start")
        planned = self._plan_areas(command)
        if isinstance(planned, str):
            return Outcome.rejected(planned)
        job, legs, mow_length = planned
        path = self._route_path(legs, job.area_ids)

        def begin() -> None:
            status = self._status
            self._end_drive_session()
            self._job = _Job(area_ids=job.area_ids, pattern=job.pattern, mow_length_m=mow_length)
            self._path, self._path_done = path, 0
            status.legs = legs
            status.activity = MowerActivity.MOWING
            status.resume_activity = None
            status.emergency_stopped = False

        self._begin_or_defer(begin)
        return Outcome.executed(f"mowing {', '.join(job.area_ids)} with {job.pattern}")

    def _plan_areas(self, command: StartMow) -> tuple[NativeJob, list["_Leg"], float] | str:
        """The legs of a native area start, or why it is refused."""
        unknown = [a for a in command.area_ids if self._map.area(a) is None]
        if unknown:
            return f"unknown area ids: {', '.join(unknown)}"
        try:
            job = plan_to_native_job(MowRequest.from_start_mow(command), self._capabilities)
        except (UnsupportedPattern, ValueError) as exc:
            return str(exc)
        forbidden = [zone.boundary for zone in self._map.forbidden_zones()]
        legs: list[_Leg] = []
        mow_length = 0.0
        for area_id in job.area_ids:
            area = self._map.area(area_id)
            assert area is not None  # noqa: S101 - checked above
            for angle in job.passes:
                points = stripe_path(
                    area.boundary,
                    forbidden,
                    spacing_m=job.spacing_m,
                    angle_deg=angle,
                    margin_m=self.config.margin_m,
                )
                if not points:
                    continue
                legs.append(_Leg(points[0], blade=False))  # transfer, blades off
                for previous, point in itertools.pairwise(points):
                    legs.append(_Leg(point, blade=True, area_id=area_id))
                    mow_length += distance(previous, point)
        return job, legs, mow_length

    def _route_path(self, legs: Sequence["_Leg"], area_ids: Sequence[str]) -> MowPath:
        """The route of ``legs`` as a path: one point per leg, a new polyline at every transfer
        (blades off), so the progress is simply the number of legs done."""
        polylines: list[list[LocalPoint]] = []
        kinds: list[MowPathKind] = []
        for leg in legs:
            if not leg.blade or not polylines:
                polylines.append([])
                kinds.append("mow")
            polylines[-1].append(leg.target)
        digest = 0
        for leg in legs:
            digest = (digest * 31 + round(leg.target[0] * 100) * 7 + round(leg.target[1] * 100)) % (
                10**12
            )
        return MowPath(
            path_hash=f"sim-{digest}",
            polylines=polylines,
            kinds=kinds,
            points_total=len(legs),
            area_ids=list(area_ids),
            planned_at=self._clock(),
        )

    def _start_spot(self, plan: SpotMowPlan) -> Outcome:
        """Follow a Planner route segment by segment (blades and speed per segment)."""
        if self._battery < self.config.min_start_battery_percent:
            return Outcome.rejected("battery too low to start")
        limit = self._capabilities.max_speed_mps or self.config.mowing_speed_mps
        legs: list[_Leg] = []
        mow_length = 0.0
        here: LocalPoint | None = None
        for segment in plan.segments:
            points = segment.points()
            speed = min(segment.speed_mps, limit)
            if here is None or distance(here, points[0]) > _ARRIVAL_EPS:
                legs.append(_Leg(points[0], blade=False))  # to the start, blades off
            for previous, point in itertools.pairwise(points):
                area = SPOT_AREA_ID if segment.blades_on else None
                legs.append(_Leg(point, blade=segment.blades_on, area_id=area, speed=speed))
                if segment.blades_on:
                    mow_length += distance(previous, point)
            here = points[-1]
        pattern = "+".join(plan.pattern_chain)
        path = self._route_path(legs, (SPOT_AREA_ID,)) if legs else None

        def begin() -> None:
            status = self._status
            self._end_drive_session()
            self._path, self._path_done = path, 0
            self._job = _Job(
                area_ids=(SPOT_AREA_ID,),
                pattern=pattern,
                mow_length_m=mow_length,
                plan_hash=plan.plan_hash,
            )
            status.legs = legs
            status.activity = MowerActivity.MOWING
            status.resume_activity = None
            status.emergency_stopped = False

        self._begin_or_defer(begin)
        return Outcome.executed(f"mowing spot plan {plan.plan_hash[:12]} with {pattern}")

    def _begin_or_defer(self, begin: Callable[[], None]) -> None:
        """Start the job now, or after ``start_delay_s`` simulated seconds of ticks."""
        delay = self.config.start_delay_s
        if delay is None or delay <= 0:
            self._pending_start = None
            begin()
            return
        self._pending_start = _PendingStart(remaining_s=delay, begin=begin)

    def _pause(self) -> Outcome:
        status = self._status
        if self._pending_start is not None:
            self._pending_start = None
            return Outcome.executed("start cancelled before the mower began")
        if status.activity not in (MowerActivity.MOWING, MowerActivity.RETURNING):
            return Outcome.rejected(f"nothing to pause while {status.activity}")
        status.resume_activity = status.activity
        status.activity = MowerActivity.PAUSED
        self._blade_on = False
        return Outcome.executed("paused")

    def _resume(self) -> Outcome:
        status = self._status
        if status.activity is not MowerActivity.PAUSED or status.resume_activity is None:
            return Outcome.rejected(f"nothing to resume while {status.activity}")
        target = status.resume_activity

        def begin() -> None:
            status.activity = target
            status.resume_activity = None
            status.emergency_stopped = False

        if target is MowerActivity.MOWING:
            self._begin_or_defer(begin)
        else:
            begin()
        return Outcome.executed(f"resumed {target}")

    def _return_to_dock(self, *, end_job: bool = False) -> Outcome:
        status = self._status
        if status.activity in (MowerActivity.DOCKED, MowerActivity.CHARGING):
            return Outcome.executed("already docked")
        self._end_drive_session()
        # The simulator keeps no job on the way home (``_begin_return`` drops the job and its
        # progress), so a later ``resume_mow`` is refused either way; ``end_job`` only words it.
        self._begin_return()
        status.emergency_stopped = False
        return Outcome.executed("job ended, returning to dock" if end_job else "returning to dock")

    def _set_blade_height(self, mm: int) -> Outcome:
        if not self._capabilities.blade_height_allowed(mm):
            return Outcome.rejected(f"blade height {mm} mm is outside the supported range")
        self._blade_height = mm
        return Outcome.executed(f"blade height {mm} mm")

    def _set_light(self, *, on: bool) -> Outcome:
        if not self.config.light:
            return Outcome.rejected("this mower has no switchable light")
        self._light_on = on
        self._publish()
        return Outcome.executed("light on" if on else "light off")

    def _set_speed(self, mps: float) -> Outcome:
        if not self._capabilities.speed_allowed(mps):
            return Outcome.rejected(f"speed {mps} m/s is outside the supported range")
        self._mowing_speed = mps
        return Outcome.executed(f"speed {mps} m/s")

    def _begin_return(self) -> None:
        dock = self._map.dock
        assert dock is not None  # noqa: S101 - checked in __init__
        status = self._status
        self._job = None
        self._blade_on = False
        status.legs = [_Leg((dock.x, dock.y), blade=False)]
        status.activity = MowerActivity.RETURNING
        status.resume_activity = None

    def _enter_error(self, code: str, message: str) -> None:
        status = self._status
        self._blade_on = False
        self._moving_speed = 0.0
        self._end_drive_session()
        if status.activity in (MowerActivity.MOWING, MowerActivity.RETURNING):
            status.resume_activity = status.activity
        status.activity = MowerActivity.ERROR
        status.error_code = code
        status.error_message = message
        _log.warning("simulated mower %s error %s: %s", self.mower_id, code, message)
        self._publish()

    # --- Internals: driving -------------------------------------------------------------------

    def _drive_update(self, linear: float, angular: float, blade_on: bool) -> None:
        self._require_no_sensor_stop()
        allowed = blade_on and self._capabilities.manual_drive_blades and not self.stuck
        self._drive_cmd = _DriveCommand(
            linear=max(-1.0, min(1.0, linear)),
            angular=max(-1.0, min(1.0, angular)),
            blade_on=allowed,
        )
        self._blade_on = allowed

    def _drive_stop(self) -> None:
        self._drive_cmd = _DriveCommand()
        self._blade_on = False

    def _drive_closed(self, session: SimulatedDriveSession) -> None:
        if self._drive is session:
            self._drive = None
        self._publish()

    def _end_drive_session(self) -> None:
        if self._drive is not None:
            self._drive._end()
            self._drive = None
        self._drive_cmd = _DriveCommand()
        if self._status.activity is not MowerActivity.MOWING:
            self._blade_on = False

    # --- Internals: time ----------------------------------------------------------------------

    def _phase(self) -> str:
        status = self._status
        if self._drive is not None:
            return "manual_drive"
        if status.activity is MowerActivity.MOWING and status.legs:
            return "mowing" if status.legs[0].blade else "transfer"
        return str(status.activity)

    def _progress(self) -> int:
        job = self._job
        if job is None or job.mow_length_m <= 0:
            return 0
        return min(100, math.floor(100 * job.mowed_m / job.mow_length_m))

    def _drain(self, seconds: float, per_hour: float) -> None:
        self._battery = max(0.0, self._battery - per_hour * seconds / 3600)

    def _advance(self, t: float) -> float:
        """Advance up to ``t`` simulated seconds; return the seconds consumed (0 only when the
        activity changed and the rest must be simulated in the new activity)."""
        status = self._status
        activity = status.activity
        if activity in (MowerActivity.MOWING, MowerActivity.RETURNING):
            if self._rtk_fix is RtkFixStatus.NONE:
                self._blade_on = False
                self._drain(t, self.config.drain_idle_per_h)
                return t
            return self._follow_route(t)
        if activity in (MowerActivity.DOCKED, MowerActivity.CHARGING):
            return self._charge(t)
        if self._drive is not None and (activity is MowerActivity.IDLE or self.stuck):
            return self._drive_step(t)
        self._drain(t, self.config.drain_idle_per_h)
        return t

    def _charge(self, t: float) -> float:
        status = self._status
        if self._battery >= 100.0:
            self._battery = 100.0
            status.activity = MowerActivity.DOCKED
            return t
        status.activity = MowerActivity.CHARGING
        needed = (100.0 - self._battery) * 3600 / self.config.charge_per_h
        if needed <= t:
            self._battery = 100.0
            status.activity = MowerActivity.DOCKED
            return needed
        self._battery += self.config.charge_per_h * t / 3600
        return t

    def _follow_route(self, t: float) -> float:
        status = self._status
        budget = t
        while status.legs and budget > 1e-12:
            leg = status.legs[0]
            if leg.speed is not None:
                speed = leg.speed
            else:
                speed = self._mowing_speed if leg.blade else self.config.transfer_speed_mps
            self._blade_on = leg.blade
            remaining = distance((self._x, self._y), leg.target)
            if remaining > _ARRIVAL_EPS:
                self._theta = math.atan2(leg.target[1] - self._y, leg.target[0] - self._x)
            needed = remaining / speed
            step = min(needed, budget)
            if needed <= budget:
                self._x, self._y = leg.target
                status.legs.pop(0)
                if status.activity is MowerActivity.MOWING and self._job is not None:
                    self._path_done += 1
            else:
                fraction = budget / needed
                self._x += (leg.target[0] - self._x) * fraction
                self._y += (leg.target[1] - self._y) * fraction
            moved = speed * step
            if leg.blade and self._job is not None:
                self._job.mowed_m += moved
                self._job.mowing_s += step
                limit = self.config.stuck_after_seconds
                if limit is not None and not self._job.stuck_done and self._job.mowing_s >= limit:
                    self._job.stuck_done = True
                    self._moving_speed = speed
                    self.inject_stuck()
                    return t - budget + step
            self._moving_speed = speed
            self._drain(
                step,
                self.config.drain_mowing_per_h if leg.blade else self.config.drain_moving_per_h,
            )
            budget -= step
            if (
                status.activity is MowerActivity.MOWING
                and self._battery <= self.config.low_battery_percent
            ):
                _log.info("simulated mower %s battery low, returning", self.mower_id)
                self._begin_return()
                return t - budget
        if not status.legs:
            self._blade_on = False
            if status.activity is MowerActivity.MOWING:
                self._begin_return()
            else:
                status.activity = MowerActivity.DOCKED
                dock = self._map.dock
                assert dock is not None  # noqa: S101 - checked in __init__
                self._theta = math.radians(dock.heading_deg)
                return t  # docking takes the rest of this tick; charging starts with the next
        return t - budget

    def _drive_step(self, t: float) -> float:
        step = min(t, _DRIVE_STEP_S)
        cmd = self._drive_cmd
        if cmd.linear == 0 and cmd.angular == 0:
            self._drain(t, self.config.drain_idle_per_h)
            return t
        if self.stuck:
            return self._stuck_step(step, cmd)
        self._theta += cmd.angular * self.config.drive_max_turn_rad_s * step
        speed = cmd.linear * self.config.drive_max_speed_mps
        nx = self._x + math.cos(self._theta) * speed * step
        ny = self._y + math.sin(self._theta) * speed * step
        zones = self._map.forbidden_zones()
        if not any(point_in_polygon((nx, ny), zone.boundary) for zone in zones):
            self._x, self._y = nx, ny
            self._moving_speed = abs(speed)
        self._drain(
            step, self.config.drain_mowing_per_h if cmd.blade_on else self.config.drain_moving_per_h
        )
        return step

    def _stuck_step(self, step: float, cmd: _DriveCommand) -> float:
        """While stuck only a backward move does anything; enough of it frees the mower."""
        self._drain(step, self.config.drain_moving_per_h)
        if cmd.linear >= 0:
            return step  # wheels spin, the mower does not move or turn
        speed = cmd.linear * self.config.drive_max_speed_mps
        nx = self._x + math.cos(self._theta) * speed * step
        ny = self._y + math.sin(self._theta) * speed * step
        if any(point_in_polygon((nx, ny), z.boundary) for z in self._map.forbidden_zones()):
            return step
        self._x, self._y = nx, ny
        self._moving_speed = abs(speed)
        self._backed_off_m += abs(speed) * step
        if self._backed_off_m >= self.config.unstuck_backward_m - 1e-9:
            status = self._status
            status.activity = MowerActivity.IDLE
            status.error_code = None
            status.error_message = None
            status.resume_activity = None
            _log.info("simulated mower %s is free again", self.mower_id)
            self._publish()
        return step

    def _publish(self) -> None:
        self._changed.set()
        if self._connected:
            self._broadcaster.publish(self.snapshot())


def create_simulated_connection(config: ConnectionConfig) -> SimulatedMower:
    """Connection factory for :class:`~mowdeck_contracts.v1.ConnectionKind.SIMULATED`.

    ``config.options`` may override any :class:`SimulationConfig` field, for example
    ``{"time_scale": 60}``. Unknown options raise ``ValueError``.
    """
    names = {f.name for f in fields(SimulationConfig)}
    unknown = sorted(set(config.options) - names)
    if unknown:
        msg = f"unknown simulator options: {', '.join(unknown)}"
        raise ValueError(msg)
    sim_config = replace(SimulationConfig(), **dict(config.options))
    return SimulatedMower(mower_id=config.mower_id, account_id=config.account_id, config=sim_config)
