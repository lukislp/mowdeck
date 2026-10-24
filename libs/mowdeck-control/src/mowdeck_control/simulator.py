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
  backwards until a backward move of at least ``unstuck_backward_m`` frees the mower. Without a
  job it is ``idle`` again; a job the error interrupted keeps the error, like a LUBA: the mower
  can be driven freely, but the error clears only after ``ResumeMow`` once it drove
  ``resume_clear_m`` on its route (``ResumeMow`` on a mower that was not freed is accepted, the
  wheels spin and the error stays);
* manual driving through a :class:`SimulatedDriveSession`;
* a camera: :meth:`SimulatedMower.get_camera_session` returns a session with ``provider``
  ``fake`` (no video; the web app draws a test pattern) and two front cameras (``front_left``
  uid 1, ``front_right`` uid 2, a stereo pair like a LUBA 2 AWD X), so the camera path runs end
  to end; :meth:`SimulatedMower.release_camera_session` records every release in
  ``camera_releases`` and clears ``camera_publishing`` (ADR 0045);
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
  (:class:`~mowdeck_control.connection.PathPreviewCapable`);
* a Bluetooth bridge as an additional link (``SimulationConfig.bridge``, or a ``links`` option
  with a ``ble_bridge`` entry as the Device-Gateway passes it, ADR 0038): the state reports
  ``links["ble_bridge"]`` and ``connection`` ``ble_bridge`` while the bridge is connected,
  manual driving is offered only then, and the test hook :meth:`SimulatedMower.set_bridge`
  flips the link (without it a drive session raises ``NotConnected``, like a real drive
  link); ``bridge_rssi`` is the signal the link reports; :meth:`SimulatedMower.scan_bridge`
  answers a scan with the configured device list (``bridge_devices``) or the configured error
  (``bridge_scan_error``);
* firmware (:class:`~mowdeck_control.connection.FirmwarePort`): :meth:`check_firmware` reports
  the installed version and ``firmware_available`` as offered; ``FirmwareUpdate`` (after the
  controller's safety rules, which the simulator applies itself too) installs it over
  ``firmware_update_seconds`` simulated seconds with the activity ``updating`` and a progress,
  then the mower reports the new version and ``update_state`` ``idle``;
* its own settings (ADR 0046, ``SimulationConfig.settings``): one setting of each type
  (:data:`SIMULATED_SETTINGS`) with ``SetSetting`` and their values in ``MowerState.settings``,
  and a rain sensor (``rain_detected``, test hook :meth:`SimulatedMower.set_raining`) that, with
  ``rain_detection`` on, blocks the self-check (``rain``) and rejects a start.
* the job options of a native start (``SimulationConfig.start_options``, ADR 0030): it declares
  :data:`SIMULATED_START_OPTIONS`, keeps the options of the latest start in ``job_options``,
  starts the route at ``start_progress_pct`` (the mowed part before it is skipped and counted as
  mowed) and, with ``stuck_after_seconds``, gets stuck twice as fast with obstacle detection
  ``off`` and half as fast with ``sensitive``;
* maintenance commands (:class:`~mowdeck_control.connection.MaintenanceCapable`, ADR 0048):
  ``restart_mower`` keeps the mower ``offline`` for ``restart_seconds`` simulated seconds (every
  command but the emergency stop and the light is refused meanwhile), then it reports docked (or
  idle when it restarted out of its dock); ``sync_map``, ``relocate_dock`` and ``resync_rtk`` are
  recorded in ``maintenance_log``; ``leave_dock`` is the undock above. The safety rules are the
  controller's (:func:`~mowdeck_control.safety.maintenance_problem`), applied once more here.
* an RTK base station (ADR 0047) when the connection option ``base_station`` links one: the
  state's ``base_station`` reports corrections over LoRa (network RTK after
  :meth:`SimulatedMower.set_rtk` with the network source), satellites, position, Wi-Fi and
  firmware; :class:`~mowdeck_control.connection.BaseStationPort` checks the firmware against
  ``base_station_firmware_available`` and installs it over ``firmware_update_seconds``, during
  which the LoRa link is down.

``SimulationConfig.time_scale`` makes simulated time run faster than real time.
"""

import asyncio
import itertools
import logging
import math
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from mowdeck_contracts.v1 import (
    BASE_STATION_OPTION,
    LINK_BLE_BRIDGE,
    MAINTENANCE_ACTIONS,
    BaseStationPosition,
    BaseStationRef,
    BaseStationState,
    BridgeScanError,
    BridgeScanErrorCode,
    BridgeScanReply,
    CameraAccess,
    CameraInfo,
    CameraPlacement,
    CameraProvider,
    CameraSession,
    CommandStatus,
    ConnectionKind,
    DockPose,
    DriveSpeedSetting,
    EmergencyStop,
    FirmwareInfo,
    FirmwareUpdate,
    GeoReference,
    GnssSolution,
    GnssStatus,
    JobProgress,
    LinkStatus,
    LocalPoint,
    MapArea,
    MapPath,
    MapZone,
    MowerActivity,
    MowerCapabilities,
    MowerErrorCategory,
    MowerMap,
    MowerState,
    MowPath,
    MowPathKind,
    MowPathProgress,
    NetworkKind,
    ObstacleDetection,
    PathOrder,
    PauseMow,
    PositionSource,
    ProxyInfo,
    RelocateDock,
    RestartMower,
    ResumeMow,
    ResyncRtk,
    ReturnToDock,
    RtkCorrectionLink,
    RtkFixStatus,
    RtkSource,
    ScannedDevice,
    SelfCheck,
    SelfCheckResult,
    SetBladeHeight,
    SetLight,
    SetSpeed,
    SettingGroup,
    SettingSpec,
    SettingType,
    SettingValue,
    SignalStatus,
    SpotMowPlan,
    StartMow,
    StartOptions,
    SyncMap,
    TurnStyle,
    utc_now,
)

from mowdeck_control.broadcast import StateBroadcaster, StateSubscription
from mowdeck_control.connection import (
    ConnectionConfig,
    Dispatch,
    JobCommand,
    MaintenanceCommand,
    Outcome,
    Photo,
)
from mowdeck_control.errors import (
    CommandRejected,
    NotConnected,
    PhotoUnavailable,
    SensorStop,
    UnsupportedPattern,
)
from mowdeck_control.geo import distance, point_in_polygon, to_geo_position
from mowdeck_control.paths import stripe_path
from mowdeck_control.photo import render_scene
from mowdeck_control.safety import (
    FIRMWARE_UPDATE_RUNNING,
    base_station_firmware_update_problem,
    firmware_update_problem,
    maintenance_problem,
)
from mowdeck_control.translation import MowRequest, NativeJob, Pattern, plan_to_native_job

_log = logging.getLogger("mowdeck_control.simulator")

DEFAULT_MOWER_ID: Final = UUID("00000000-0000-4000-8000-00000000000a")
DEFAULT_ACCOUNT_ID: Final = UUID("00000000-0000-4000-8000-00000000000b")
SIMULATED_CUTTING_WIDTH_M: Final = 0.3
"""The simulated mower's cutting width (``MowerCapabilities.cutting_width_mm`` 300)."""
SIMULATED_WIFI_DBM: Final = -58
"""The Wi-Fi signal the simulated mower reports (``MowerState.signal``)."""
SIM_BASE_LORA_CHANNEL: Final = 7
SIM_BASE_SATELLITES: Final = 32
SIM_BASE_WIFI_DBM: Final = -61
"""What the simulated RTK base station reports (ADR 0047)."""
_SATELLITES: Final[Mapping[RtkFixStatus, int]] = {
    RtkFixStatus.FIXED: 29,
    RtkFixStatus.FLOAT: 21,
    RtkFixStatus.NONE: 7,
    RtkFixStatus.UNKNOWN: 0,
}
_GNSS_SOLUTION: Final[Mapping[RtkFixStatus, GnssSolution]] = {
    RtkFixStatus.FIXED: GnssSolution.FIXED,
    RtkFixStatus.FLOAT: GnssSolution.FLOAT,
    RtkFixStatus.NONE: GnssSolution.SINGLE,
    RtkFixStatus.UNKNOWN: GnssSolution.UNKNOWN,
}

SIMULATED_START_OPTIONS: Final = StartOptions(
    obstacle_detection=[
        ObstacleDetection.OFF,
        ObstacleDetection.STANDARD,
        ObstacleDetection.SENSITIVE,
    ],
    obstacle_laps_range=(0, 4),
    orders=[PathOrder.BORDER_FIRST, PathOrder.GRID_FIRST],
    start_progress_range_pct=(0, 99),
)
"""The job options the simulated mower offers: those of a LUBA 2 AWD on current firmware."""
_STUCK_FACTOR: Final[Mapping[ObstacleDetection, float]] = {
    ObstacleDetection.OFF: 0.5,
    ObstacleDetection.SENSITIVE: 2.0,
}
"""How much sooner (below 1) or later a job gets stuck with ``stuck_after_seconds``."""

LIFT_ERROR_CODE: Final = "lifted"
SPOT_AREA_ID: Final = "spot"
"""``raw.area_id`` and ``raw.job_areas`` while the simulator follows a spot plan."""
STUCK_ERROR_CODE: Final = "stuck"
_ERROR_CATEGORY: Final[Mapping[str, MowerErrorCategory]] = {
    LIFT_ERROR_CODE: MowerErrorCategory.LIFT_TILT,
    STUCK_ERROR_CODE: MowerErrorCategory.TRAPPED,
}
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
RESTARTING: Final = "the mower is restarting"
"""Rejection of every command but the emergency stop and the light while a restart runs."""
BRIDGE_NOT_CONNECTED: Final = "the Bluetooth bridge is not connected"
"""Rejection of a drive session while the simulated bridge is down."""
SIM_BRIDGE_NAME: Final = "sim-bridge"
SIM_BRIDGE_ESPHOME: Final = "2026.1.0"
SIM_BRIDGE_PROJECT: Final = "1"
SIM_BRIDGE_COMPILED: Final = "Jan 1 2026, 00:00:00"
BridgeDevice = tuple[str, str, int]
"""One advertisement the simulated bridge sees: ``(name, address, rssi)``."""
_PROXY_UNANSWERED: Final = frozenset(
    {BridgeScanErrorCode.UNREACHABLE, BridgeScanErrorCode.AUTH_FAILED}
)
"""Scan errors after which nothing about the bridge itself is known."""


SIMULATED_SETTINGS: Final[tuple[SettingSpec, ...]] = (
    SettingSpec(key="rain_detection", type=SettingType.BOOL, group=SettingGroup.WEATHER),
    SettingSpec(
        key="recharge_level",
        type=SettingType.INT,
        group=SettingGroup.BATTERY,
        min=15,
        max=30,
        step=1,
        unit="%",
    ),
    SettingSpec(key="night_light", type=SettingType.BOOL, group=SettingGroup.LIGHTS),
    SettingSpec(
        key="turn_style",
        type=SettingType.ENUM,
        group=SettingGroup.DRIVING,
        options=["in_place", "multi_point"],
    ),
    SettingSpec(
        key="voice_volume",
        type=SettingType.INT,
        group=SettingGroup.VOICE,
        min=0,
        max=100,
        step=1,
        unit="%",
    ),
    SettingSpec(key="non_work_hours", type=SettingType.TIME_WINDOW, group=SettingGroup.SCHEDULE),
)
"""The settings the simulator offers (``SimulationConfig.settings``, ADR 0046): one of each type."""
SIMULATED_SETTING_DEFAULTS: Final[Mapping[str, SettingValue]] = {
    "rain_detection": True,
    "recharge_level": 20,
    "night_light": False,
    "turn_style": "in_place",
    "voice_volume": 50,
    "non_work_hours": None,
}
"""The values the simulated mower starts with."""
RAIN_BLOCKS_START: Final = "rain detected: the mower does not start in the rain"
"""Rejection of a start while it rains and ``rain_detection`` is on."""


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
    """Ground speed at ``linear = 1`` while the drive speed setting is ``drive_speed_mps``
    (the full-scale speed a drive calibration measures)."""
    drive_max_turn_rad_s: float = 1.0
    """Yaw rate at ``angular = 1`` (the full-scale yaw rate a drive calibration measures)."""
    drive_speed_setting: bool = True
    """Offer the vendor-style speed setting for manual driving (capability
    ``drive_speed_setting``, ADR 0042): the full-scale speed scales with it."""
    drive_speed_mps: float = 0.5
    """Initial value of the speed setting (at which ``drive_max_speed_mps`` applies)."""
    drive_speed_range: tuple[float, float, float] = (0.2, 1.0, 0.1)
    """``(min, max, step)`` of the speed setting."""
    drive_response_delay_s: float = 0.0
    """Simulated seconds between a drive frame and the wheels following it (a real mower
    reacts one to two seconds late, ADR 0042); 0: at once. The blades and a session stop act
    at once."""
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
    photos: bool = True
    """Take still images of its own (:class:`PhotoCapture`, the garden drawn from the map).
    False: :meth:`SimulatedMower.capture_photo` raises :class:`PhotoUnavailable`, like a camera
    mower without own photos, so photos come from its live camera (the camera relay)."""
    camera_session_seconds: float = 300.0
    """Lifetime of a fake camera session (short values exercise the client's refresh)."""
    stuck_after_seconds: float | None = None
    """Development only: get stuck after this many simulated seconds of mowing in a job (once
    per job), so AI assist can be demonstrated; obstacle detection ``off`` halves it and
    ``sensitive`` doubles it. None: never."""
    start_options: bool = True
    """Offer the job options of a native start (:data:`SIMULATED_START_OPTIONS`)."""
    unstuck_backward_m: float = 0.3
    """Backward distance that frees a stuck mower."""
    resume_clear_m: float = 2.0
    """After ``ResumeMow`` on a freed mower whose job a stuck error interrupted: the distance
    driven on the route after which the error clears and the mower mows again (a LUBA clears
    its stuck error only once the resumed job moved it a few metres)."""
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
    bridge: bool = False
    """The mower has a Bluetooth bridge as an additional link (``links["ble_bridge"]`` in the
    state); manual driving is offered only while it is connected. Also switched on by a
    ``links`` option with a ``ble_bridge`` entry (what the Device-Gateway passes)."""
    bridge_connected: bool = True
    """Initial state of the bridge link (test hook :meth:`SimulatedMower.set_bridge`)."""
    state_period_s: float = 0.0
    """Report throttle: ``tick()`` publishes a state at most this often in simulated seconds
    (0: every tick), like a real mower that reports about once a second. Events (RTK, errors,
    commands) still publish at once."""
    position_noise_m: float = 0.0
    """Standard deviation of a Gaussian noise added to the reported position (metres), so
    control loops are tested with RTK-like scatter; the true position is not disturbed."""
    noise_seed: int = 1
    """Seed of the position noise (deterministic runs)."""
    bridge_rssi: int = -60
    """Signal strength the bridge reports for the mower while connected."""
    bridge_devices: tuple[BridgeDevice, ...] = (("Luba-SIM0001", "FC:23:45:67:89:AB", -62),)
    """What a bridge scan sees: ``(name, address, rssi)`` per advertisement."""
    bridge_scan_error: str | None = None
    """Answer every bridge scan with this error code (``unreachable``, ``auth_failed``,
    ``timeout``, ``busy``) instead of the device list."""
    firmware_available: str | None = None
    """A newer firmware the "manufacturer" offers (``FirmwareInfo.available``); None: up to
    date. Installing it takes ``firmware_update_seconds`` simulated seconds."""
    firmware_release_notes: str | None = None
    firmware_update_seconds: float = 5.0
    undock: bool = True
    """Offer ``leave_dock`` (capability ``undock``): from the dock the mower drives
    ``undock_distance_m`` straight out along the dock's heading and stands idle there."""
    undock_distance_m: float = 1.0
    drive_reverse: bool = True
    """The drive link can reverse (capability ``drive_reverse``, ADR 0043); without it a
    negative ``linear`` stands the mower still, like a link that cannot drive backwards."""
    turn_style: TurnStyle | None = None
    """The mower's own turn setting reported in ``MowerState.turn_style`` (None: none)."""
    settings: bool = True
    """Offer the mower's own settings (:data:`SIMULATED_SETTINGS`, ADR 0046) with ``SetSetting``."""
    raining: bool = False
    """Initial state of the rain sensor (``MowerState.rain_detected``; test hook
    :meth:`SimulatedMower.set_raining`)."""
    maintenance: bool = True
    """Offer the maintenance commands (capability ``maintenance``, ADR 0048; ``leave_dock`` only
    with ``undock``)."""
    restart_seconds: float = 5.0
    """Simulated seconds a ``restart_mower`` keeps the mower ``offline`` before it reports again
    (docked when it restarted in its dock, else idle)."""
    base_station_firmware: str = "sim-base-1.0.0"
    """The firmware the simulated RTK base station runs (ADR 0047; only with a linked base
    station, the connection option ``base_station``)."""
    base_station_firmware_available: str | None = None
    """A newer base station firmware the "manufacturer" offers; None: up to date. Installing
    it takes ``firmware_update_seconds`` simulated seconds."""

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
        if not 0 <= self.drive_response_delay_s <= 10:
            msg = "drive_response_delay_s must be within 0..10"
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
        if self.resume_clear_m <= 0:
            msg = "resume_clear_m must be positive"
            raise ValueError(msg)
        if self.start_delay_s is not None and self.start_delay_s < 0:
            msg = "start_delay_s must not be negative"
            raise ValueError(msg)
        if self.confirm_timeout_s <= 0:
            msg = "confirm_timeout_s must be positive"
            raise ValueError(msg)
        if self.firmware_update_seconds <= 0:
            msg = "firmware_update_seconds must be positive"
            raise ValueError(msg)
        if self.restart_seconds <= 0:
            msg = "restart_seconds must be positive"
            raise ValueError(msg)
        if self.bridge_scan_error is not None:
            BridgeScanErrorCode(self.bridge_scan_error)  # raises ValueError for unknown codes


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
    started_s: float = 0.0
    """Simulated seconds since the start of the simulator when the job began."""
    mowing_s: float = 0.0
    stuck_done: bool = False
    stuck_after_s: float | None = None
    """When this job gets stuck (``stuck_after_seconds`` scaled by its obstacle detection)."""


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
            if self._mower.config.bridge and not self._mower._bridge_connected:
                raise NotConnected(BRIDGE_NOT_CONNECTED)  # the drive link dropped
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
        base_station: BaseStationRef | None = None,
    ) -> None:
        self.mower_id = mower_id
        self.base_station = base_station
        """The RTK base station the mower is linked to (ADR 0047); None: none, the state then
        carries no ``base_station``."""
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
        self._drive_pending: list[tuple[float, _DriveCommand]] = []
        """Drive frames not yet followed (``drive_response_delay_s``): ``(due_s, command)``."""
        self._backed_off_m = 0.0
        self._unblocked = False
        """A stuck mower in a job was driven free; the error stays until the job resumes."""
        self._resuming = False
        """``ResumeMow`` came while the stuck error lasts: the route continues once free."""
        self._resume_moved_m = 0.0
        self._pending_start: _PendingStart | None = None
        self._path: MowPath | None = None
        self._path_done = 0
        self._changed = asyncio.Event()
        self._light_on = False
        self._bridge_connected = self.config.bridge and self.config.bridge_connected
        self._bridge_since: datetime | None = self._clock() if self.config.bridge else None
        self._firmware_installed = self.config.firmware
        self._firmware_available = self.config.firmware_available
        self._firmware_checked_at: datetime | None = None
        self._firmware_progress: int | None = None
        self._firmware_remaining_s = 0.0
        self._firmware_target: str | None = None
        self._last_report_s: float | None = None
        self._base_installed = self.config.base_station_firmware
        self._base_available = self.config.base_station_firmware_available
        self._base_checked_at: datetime | None = None
        self._base_target: str | None = None
        self._base_remaining_s = 0.0
        self._base_details_at: datetime | None = None
        self._noise = random.Random(self.config.noise_seed)  # noqa: S311 - not security
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
            links_supported=[LINK_BLE_BRIDGE],
            firmware_update=True,
            base_station_firmware_update=base_station is not None,
            undock=self.config.undock,
            maintenance=(
                frozenset(a for a in MAINTENANCE_ACTIONS if a != "leave_dock" or self.config.undock)
                if self.config.maintenance
                else frozenset()
            ),
            drive_reverse=self.config.drive_reverse,
            drive_speed_setting=(
                DriveSpeedSetting(
                    min_mps=self.config.drive_speed_range[0],
                    max_mps=self.config.drive_speed_range[1],
                    step_mps=self.config.drive_speed_range[2],
                )
                if self.config.drive_speed_setting
                else None
            ),
        )
        if self.config.start_options:
            self._capabilities = self._capabilities.model_copy(
                update={"start_options": SIMULATED_START_OPTIONS}
            )
        if self.config.settings:
            self._capabilities = self._capabilities.model_copy(
                update={"settings": list(SIMULATED_SETTINGS)}
            )
        self._settings: dict[str, SettingValue] = dict(SIMULATED_SETTING_DEFAULTS)
        self._raining = self.config.raining
        self._drive_speed = self.config.drive_speed_mps
        self._undock_target: LocalPoint | None = None
        self.camera_publishing = False
        """True between a camera session and its release (``release_camera_session``), like a
        mower that keeps publishing until it is told to stop (ADR 0045)."""
        self.camera_releases: list[str | None] = []
        """The ``channel`` of every :meth:`release_camera_session` call, in order."""
        self.job_options: dict[str, Any] = {}
        """The job options of the latest native start as ``MowPattern`` keys (only those it
        set), e.g. ``{"obstacle_detection": "sensitive", "start_progress_pct": 40}``."""
        self.maintenance_log: list[str] = []
        """The kind of every maintenance command the simulated mower ran, in order."""
        self._restart_remaining_s = 0.0
        self._restart_back = MowerActivity.DOCKED

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
            if self._firmware_checked_at is None:
                self._firmware_checked_at = self._clock()  # the "vendor" answers at connect
            if self.base_station is not None:
                now = self._clock()
                self._base_details_at = now  # the mower answers the base station request
                if self._base_checked_at is None:
                    self._base_checked_at = now
            self._publish()

    async def disconnect(self) -> None:
        if self._connected:
            self._end_drive_session()
            self._connected = False
            self._broadcaster.close()

    async def capabilities(self) -> MowerCapabilities:
        """The capabilities; with a bridge, manual driving follows the link state."""
        if not self.config.bridge or self._bridge_connected:
            return self._capabilities
        return self._capabilities.model_copy(
            update={"manual_drive": False, "manual_drive_blades": False}
        )

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
        self.camera_publishing = True
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

    async def release_camera_session(self, channel: str | None = None) -> None:
        """:class:`~mowdeck_control.connection.CameraSessionRelease`: record the release and
        stop "publishing"; idempotent, also without a camera or a session."""
        self._require_connected()
        self.camera_releases.append(channel)
        self.camera_publishing = False

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
        if self.restarting:
            return Dispatch.done(Outcome.rejected(RESTARTING))
        if self.stuck and isinstance(command, ResumeMow) and self._status.resume_activity:
            return Dispatch.done(self._resume_stuck())
        if self.stuck:
            return Dispatch.done(Outcome.rejected("the mower is stuck: drive it free first"))
        self._require_no_sensor_stop()
        if self._status.activity is MowerActivity.UPDATING:
            return Dispatch.done(Outcome.rejected(FIRMWARE_UPDATE_RUNNING))
        delayed = self.config.start_delay_s is not None
        match command:
            case FirmwareUpdate():
                outcome = self._firmware_update(command.version)
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

    async def set_setting(self, key: str, value: SettingValue) -> Outcome:
        """:class:`~mowdeck_control.connection.SettingsCapable` (ADR 0046): store the value; it
        never moves the mower. Works in every state, like a real mower's settings."""
        self._require_connected()
        spec = self._capabilities.setting(key)
        if spec is None:
            msg = f"this mower has no setting {key!r}"
            raise CommandRejected(msg)
        problem = spec.value_problem(value)
        if problem is not None:
            raise CommandRejected(problem)
        self._settings[key] = value
        self._publish()
        return Outcome.executed(f"{key} set")

    @property
    def settings(self) -> Mapping[str, SettingValue]:
        """The current values of the simulated settings."""
        return dict(self._settings)

    async def get_drive_speed(self) -> float | None:
        """:class:`~mowdeck_control.connection.DriveSpeedCapable`: the speed setting."""
        self._require_connected()
        if not self.config.drive_speed_setting:
            raise CommandRejected("this mower has no speed setting for manual driving")
        return self._drive_speed

    async def set_drive_speed(self, speed_mps: float) -> None:
        """:class:`~mowdeck_control.connection.DriveSpeedCapable`: change the speed setting
        (the full-scale drive speed scales with it)."""
        self._require_connected()
        setting = self._capabilities.drive_speed_setting
        if setting is None:
            raise CommandRejected("this mower has no speed setting for manual driving")
        self._drive_speed = setting.clamp(speed_mps)
        self._publish()

    @property
    def drive_full_speed_mps(self) -> float:
        """Ground speed at ``linear = 1`` with the current speed setting."""
        if not self.config.drive_speed_setting or self.config.drive_speed_mps <= 0:
            return self.config.drive_max_speed_mps
        return self.config.drive_max_speed_mps * self._drive_speed / self.config.drive_speed_mps

    async def leave_dock(self) -> None:
        """:class:`~mowdeck_control.connection.UndockCapable`: drive ``undock_distance_m``
        straight out of the dock along its heading (blades off), then stand ``idle``."""
        self._require_connected()
        self._require_no_sensor_stop()
        if not self.config.undock:
            raise CommandRejected("this mower cannot leave its dock by itself")
        status = self._status
        if status.activity not in (MowerActivity.DOCKED, MowerActivity.CHARGING):
            raise CommandRejected(f"the mower is {status.activity.value}, not in its dock")
        dock = self._map.dock
        assert dock is not None  # noqa: S101 - checked in __init__
        heading = math.radians(dock.heading_deg)
        distance_m = self.config.undock_distance_m
        self._undock_target = (
            dock.x + math.cos(heading) * distance_m,
            dock.y + math.sin(heading) * distance_m,
        )
        self._theta = heading
        self._blade_on = False
        status.activity = MowerActivity.IDLE
        status.emergency_stopped = False
        self._publish()

    @property
    def restarting(self) -> bool:
        """True while a ``restart_mower`` keeps the simulated mower offline."""
        return self._restart_remaining_s > 0

    async def run_maintenance(self, command: MaintenanceCommand) -> Outcome:
        """:class:`~mowdeck_control.connection.MaintenanceCapable` (ADR 0048), with the safety
        rules of :func:`~mowdeck_control.safety.maintenance_problem` applied once more for
        direct callers. Every action is recorded in ``maintenance_log``; a restart keeps the
        mower ``offline`` for ``restart_seconds`` simulated seconds."""
        self._require_connected()
        if command.kind not in self._capabilities.maintenance:
            raise CommandRejected(f"this mower does not support {command.kind}")
        if self.restarting:
            raise CommandRejected(RESTARTING)
        if self._status.activity is MowerActivity.UPDATING:
            raise CommandRejected(FIRMWARE_UPDATE_RUNNING)
        problem = maintenance_problem(
            command.kind, self.snapshot(), drive_session_open=self._drive is not None
        )
        if problem is not None:
            raise CommandRejected(problem)
        self.maintenance_log.append(command.kind)
        match command:
            case RestartMower():
                status = self._status
                self._end_drive_session()
                self._undock_target = None
                in_dock = status.activity in (MowerActivity.DOCKED, MowerActivity.CHARGING)
                self._restart_back = MowerActivity.DOCKED if in_dock else MowerActivity.IDLE
                self._restart_remaining_s = self.config.restart_seconds
                self._blade_on = False
                status.activity = MowerActivity.OFFLINE
                outcome = Outcome.accepted(
                    f"restart sent; the mower is back in about {self.config.restart_seconds:g} s"
                )
            case SyncMap():
                outcome = Outcome.executed(f"map read again: {len(self._map.areas)} areas")
            case RelocateDock():
                outcome = Outcome.accepted(
                    "charging station position deleted; the mower learns it again in its dock"
                )
            case ResyncRtk():
                outcome = Outcome.executed("RTK origin and dock pose read again")
        self._publish()
        return outcome

    async def start_drive_session(self) -> SimulatedDriveSession:
        self._require_connected()
        self._require_no_sensor_stop()
        if self.config.bridge and not self._bridge_connected:
            raise NotConnected(BRIDGE_NOT_CONNECTED)
        status = self._status
        if status.activity is MowerActivity.UPDATING:
            raise CommandRejected(FIRMWARE_UPDATE_RUNNING)
        if self.restarting:
            raise CommandRejected(RESTARTING)
        if status.activity in (MowerActivity.MOWING, MowerActivity.RETURNING):
            raise CommandRejected("pause or end the job before driving manually")
        self._end_drive_session()
        if self.stuck and status.resume_activity is not None:
            # Driving a stuck mower free is part of the job's recovery: the job the error
            # interrupted stays and waits for ``ResumeMow`` once the mower is free.
            _log.info("manual drive frees the mower, the interrupted job stays")
        else:
            if status.activity is MowerActivity.PAUSED:
                _log.info("manual drive cancels the paused job")
            self._job = None
            status.legs = []
            status.resume_activity = None
        status.emergency_stopped = False
        if not self.stuck:
            status.activity = MowerActivity.IDLE
        self._drive = SimulatedDriveSession(self)
        self._drive_pending.clear()
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
        status = self._status
        interrupted = status.activity is MowerActivity.ERROR and status.resume_activity is not None
        if status.activity not in _PATH_ACTIVITIES and not interrupted:
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
        """A still image of the surroundings drawn from the map (:class:`PhotoCapture`);
        :class:`PhotoUnavailable` with the option ``photos`` false."""
        self._require_connected()
        if not self.config.photos:
            msg = "the simulated mower takes no photos of its own (option photos: false)"
            raise PhotoUnavailable(msg)
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

    # --- Links and firmware (ADR 0038) ------------------------------------------------------

    @property
    def bridge_connected(self) -> bool:
        """True while the simulated bridge link is up (always False without a bridge)."""
        return self.config.bridge and self._bridge_connected

    def set_bridge(self, connected: bool) -> None:
        """Test hook: connect or drop the Bluetooth bridge. A drop ends an open drive session
        (frames would have nowhere to go) and manual driving disappears from the capabilities
        until the bridge is back; both changes are published at once."""
        if not self.config.bridge:
            msg = "this simulated mower has no bridge (SimulationConfig.bridge)"
            raise ValueError(msg)
        if connected == self._bridge_connected:
            return
        self._bridge_connected = connected
        self._bridge_since = self._clock()
        if not connected:
            self._end_drive_session()
        self._publish()

    def _link_status(self) -> dict[str, LinkStatus]:
        if not self.config.bridge:
            return {}
        return {
            LINK_BLE_BRIDGE: LinkStatus(
                connected=self._bridge_connected,
                since=self._bridge_since,
                rssi=self.config.bridge_rssi if self._bridge_connected else None,
                esphome_version=SIM_BRIDGE_ESPHOME,
                compiled_at=SIM_BRIDGE_COMPILED,
                project_version=SIM_BRIDGE_PROJECT,
            )
        }

    async def scan_bridge(
        self,
        *,
        host: str,
        port: int,
        secret: str,
        expected_name: str | None,
        timeout_s: float,
    ) -> BridgeScanReply:
        """:class:`~mowdeck_control.connection.BridgeScanCapable`: the configured device list
        (``bridge_devices``), or the configured error. The expected name counts as seen when it
        is in the list; otherwise the reply carries ``timeout`` and the proxy information."""
        del port, secret, timeout_s
        proxy = ProxyInfo(
            proxy_name=SIM_BRIDGE_NAME,
            esphome_version=SIM_BRIDGE_ESPHOME,
            compiled_at=SIM_BRIDGE_COMPILED,
            project_name="mowdeck.bridge",
            project_version=SIM_BRIDGE_PROJECT,
            bluetooth_mac="AA:BB:CC:DD:EE:01",
            checked_at=self._clock(),
        )
        error = self.config.bridge_scan_error
        if error is not None:
            code = BridgeScanErrorCode(error)
            return BridgeScanReply(
                proxy=None if code in _PROXY_UNANSWERED else proxy,
                error=BridgeScanError(code=code, detail=f"simulated bridge {host}: {code.value}"),
            )
        devices = [
            ScannedDevice(name=name, address=address, rssi=rssi)
            for name, address, rssi in self.config.bridge_devices
        ]
        if expected_name is not None and all(d.name != expected_name for d in devices):
            return BridgeScanReply(
                proxy=proxy,
                devices=devices,
                error=BridgeScanError(
                    code=BridgeScanErrorCode.TIMEOUT,
                    detail=f"{expected_name} did not advertise at the simulated bridge",
                ),
            )
        return BridgeScanReply(proxy=proxy, devices=devices)

    def firmware_info(self) -> FirmwareInfo | None:
        """The firmware state as the next snapshot reports it (None before the first check)."""
        if self._firmware_checked_at is None:
            return None
        if self._firmware_target is not None:
            state = "installing"
        elif self._firmware_available:
            state = "available"
        else:
            state = "idle"
        return FirmwareInfo(
            installed=self._firmware_installed,
            available=self._firmware_available,
            release_notes=self.config.firmware_release_notes if self._firmware_available else None,
            update_state=state,  # type: ignore[arg-type]
            progress=self._firmware_progress,
            checked_at=self._firmware_checked_at,
        )

    async def check_firmware(self) -> FirmwareInfo | None:
        """:class:`~mowdeck_control.connection.FirmwarePort`: the "vendor" answers at once."""
        self._require_connected()
        self._firmware_checked_at = self._clock()
        self._publish()
        return self.firmware_info()

    async def start_firmware_update(self, version: str) -> None:
        """:class:`~mowdeck_control.connection.FirmwarePort`: start installing ``version``.

        Direct callers get the same rules as the controller applies
        (:func:`~mowdeck_control.safety.firmware_update_problem`)."""
        self._require_connected()
        outcome = self._firmware_update(version)
        self._publish()
        if outcome.status is not CommandStatus.EXECUTED:
            raise CommandRejected(outcome.reason or "firmware update refused")

    def _firmware_update(self, version: str) -> Outcome:
        problem = firmware_update_problem(version, self.snapshot(), self._capabilities)
        if problem is not None:
            return Outcome.rejected(problem)
        self._firmware_target = version
        self._firmware_progress = 0
        self._firmware_remaining_s = self.config.firmware_update_seconds
        self._end_drive_session()
        self._status.activity = MowerActivity.UPDATING
        return Outcome.executed(f"firmware update to {version} started")

    def _update_step(self, t: float) -> float:
        """Advance a running firmware update by ``t`` simulated seconds."""
        total = self.config.firmware_update_seconds
        step = min(t, self._firmware_remaining_s)
        self._firmware_remaining_s -= step
        done = max(0.0, total - self._firmware_remaining_s)
        self._firmware_progress = min(100, math.floor(100 * done / total))
        if self._firmware_remaining_s <= 1e-9:
            target = self._firmware_target
            assert target is not None  # noqa: S101 - set when the update started
            self._firmware_installed = target
            self._firmware_available = None
            self._firmware_target = None
            self._firmware_progress = None
            self._firmware_checked_at = self._clock()
            self._status.activity = MowerActivity.DOCKED
            _log.info("simulated mower %s installed firmware %s", self.mower_id, target)
        return t

    # --- RTK base station (BaseStationPort, ADR 0047) -----------------------------------------

    def base_station_firmware_info(self) -> FirmwareInfo | None:
        """The base station's firmware state (None before the first check)."""
        if self._base_checked_at is None:
            return None
        progress: int | None = None
        if self._base_target is not None:
            state = "installing"
            total = self.config.firmware_update_seconds
            progress = min(100, math.floor(100 * (total - self._base_remaining_s) / total))
        elif self._base_available:
            state = "available"
        else:
            state = "idle"
        return FirmwareInfo(
            installed=self._base_installed,
            available=self._base_available,
            update_state=state,  # type: ignore[arg-type]
            progress=progress,
            checked_at=self._base_checked_at,
        )

    def _base_station_state(self) -> BaseStationState | None:
        """The linked base station as the simulated mower sees it: corrections over LoRa while
        the RTK source is the base station (the link drops while the base station updates)."""
        ref = self.base_station
        if ref is None:
            return None
        lora = self._rtk_source is RtkSource.BASE_STATION
        updating = self._base_target is not None
        where = to_geo_position(self._map.reference, -3.0, -2.0, 0.0)
        return BaseStationState(
            base_station_id=ref.base_station_id,
            correction_link=RtkCorrectionLink.LORA if lora else RtkCorrectionLink.NETWORK,
            link_up=(not updating) if lora else None,
            lora_channel=SIM_BASE_LORA_CHANNEL,
            satellites=None if updating else SIM_BASE_SATELLITES,
            rtk_status=GnssSolution.UNKNOWN if updating else GnssSolution.FIXED,
            position=BaseStationPosition(lat=where.lat, lon=where.lon),
            wifi_dbm=SIM_BASE_WIFI_DBM,
            moved=False,
            firmware=self._base_installed,
            firmware_info=self.base_station_firmware_info(),
            details_at=self._base_details_at,
        )

    async def check_base_station_firmware(self) -> FirmwareInfo | None:
        """:class:`~mowdeck_control.connection.BaseStationPort`: the "vendor" answers at once;
        None without a linked base station."""
        self._require_connected()
        if self.base_station is None:
            return None
        self._base_checked_at = self._clock()
        self._publish()
        return self.base_station_firmware_info()

    async def start_base_station_firmware_update(self, version: str) -> None:
        """:class:`~mowdeck_control.connection.BaseStationPort`: start installing ``version`` on
        the base station; direct callers get the controller's rules
        (:func:`~mowdeck_control.safety.base_station_firmware_update_problem`)."""
        self._require_connected()
        problem = base_station_firmware_update_problem(version, self.snapshot(), self._capabilities)
        if problem is not None:
            raise CommandRejected(problem)
        self._base_target = version
        self._base_remaining_s = self.config.firmware_update_seconds
        self._publish()

    def _base_update_step(self, t: float) -> None:
        """Advance a running base station update by ``t`` simulated seconds."""
        if self._base_target is None:
            return
        self._base_remaining_s -= t
        if self._base_remaining_s > 1e-9:
            return
        self._base_installed = self._base_target
        self._base_available = None
        self._base_target = None
        self._base_remaining_s = 0.0
        self._base_checked_at = self._clock()
        _log.info("simulated base station of %s updated to %s", self.mower_id, self._base_installed)

    def tick(self, dt: float) -> MowerState:
        """Advance the simulation by ``dt`` real seconds (times ``time_scale``) and publish."""
        if dt < 0:
            msg = "dt must not be negative"
            raise ValueError(msg)
        remaining = dt * self.config.time_scale
        self._elapsed_s += remaining
        self._base_update_step(remaining)
        pending = self._pending_start
        if pending is not None:
            pending.remaining_s -= remaining
            if pending.remaining_s <= 0:
                self._pending_start = None
                pending.begin()
        self._moving_speed = 0.0
        guard = 0
        cursor = self._elapsed_s - remaining
        while remaining > 1e-9:
            guard += 1
            if guard > 1_000_000:  # pragma: no cover - defensive, never reached
                break
            chunk = self._apply_due_drive(cursor, remaining)
            used = self._advance(chunk)
            remaining -= used
            cursor += used
        state = self.snapshot()
        self._changed.set()
        period = self.config.state_period_s
        due = (
            period <= 0
            or self._last_report_s is None
            or self._elapsed_s - self._last_report_s >= period - 1e-9
        )
        if self._connected and due:
            self._last_report_s = self._elapsed_s
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
        x, y = self._x, self._y
        if self.config.position_noise_m > 0:
            x += self._noise.gauss(0.0, self.config.position_noise_m)
            y += self._noise.gauss(0.0, self.config.position_noise_m)
        return MowerState(
            mower_id=self.mower_id,
            account_id=self.account_id,
            observed_at=self._clock(),
            activity=status.activity,
            battery_percent=max(0, min(100, math.floor(self._battery))),
            charging=status.activity in (MowerActivity.CHARGING, MowerActivity.UPDATING),
            position=to_geo_position(self._map.reference, x, y, self._theta),
            rtk_fix=self._rtk_fix,
            rtk_source=self._rtk_source,
            position_source=_POSITION_SOURCE.get(self._rtk_fix, PositionSource.UNKNOWN),
            connection=ConnectionKind.BLE_BRIDGE
            if self.bridge_connected
            else ConnectionKind.SIMULATED,
            blade_height_mm=self._blade_height,
            speed_mps=round(self._moving_speed, 3),
            error_code=status.error_code,
            error_message=status.error_message,
            drive_recoverable=self.stuck,
            firmware=self._firmware_installed,
            light_on=self._light_on if self.config.light else None,
            links=self._link_status(),
            firmware_info=self.firmware_info(),
            drive_speed_mps=self._drive_speed if self.config.drive_speed_setting else None,
            turn_style=self.config.turn_style,
            job=self._job_progress(leg),
            gnss=GnssStatus(
                satellites=_SATELLITES[self._rtk_fix],
                correction_age_s=None if self._rtk_fix is RtkFixStatus.NONE else 1.0,
                rtk_status=_GNSS_SOLUTION[self._rtk_fix],
            ),
            signal=SignalStatus(
                wifi_dbm=SIMULATED_WIFI_DBM, mobile_dbm=None, network=NetworkKind.WIFI
            ),
            self_check=SelfCheck(result=SelfCheckResult.BLOCKED, detail="rain")
            if self._rain_blocks()
            else SelfCheck(result=SelfCheckResult.OK),
            error_category=_ERROR_CATEGORY.get(status.error_code or "", MowerErrorCategory.OTHER)
            if status.activity is MowerActivity.ERROR
            else None,
            settings=dict(self._settings) if self.config.settings else {},
            rain_detected=self._raining,
            base_station=self._base_station_state(),
            raw=raw,
        )

    def _job_progress(self, leg: "_Leg | None") -> JobProgress | None:
        """The running job for ``MowerState.job``: progress, the map area of the current
        mowing leg (null on transfers and in spot runs), seconds since the start, the remaining
        mowing length at the mowing speed, and the mowed length times the cutting width."""
        job = self._job
        if job is None:
            return None
        area_id = leg.area_id if leg is not None and leg.blade else None
        if area_id is not None and self._map.area(area_id) is None:
            area_id = None
        left_m = max(0.0, job.mow_length_m - job.mowed_m)
        speed = self._mowing_speed if self._mowing_speed > 0 else self.config.mowing_speed_mps
        return JobProgress(
            progress_percent=self._progress(),
            area_id=area_id,
            elapsed_s=max(0, round(self._elapsed_s - job.started_s)),
            remaining_s=round(left_m / speed) if speed > 0 else None,
            mowed_m2=round(job.mowed_m * SIMULATED_CUTTING_WIDTH_M, 1),
        )

    # --- Test hooks ---------------------------------------------------------------------------

    def set_rtk(self, fix: RtkFixStatus, source: RtkSource | None = None) -> None:
        """Set the RTK fix (and optionally the correction source). Without a fix (``none``) the
        mower holds its position while mowing."""
        self._rtk_fix = fix
        if source is not None:
            self._rtk_source = source
        self._publish()

    def set_raining(self, raining: bool) -> None:
        """Let the rain sensor report rain (``MowerState.rain_detected``). With the setting
        ``rain_detection`` on, the self-check then reports ``blocked`` (``rain``) and a start is
        rejected; a running job goes on (the simulator does not send the mower home)."""
        self._raining = raining
        self._publish()

    def _rain_blocks(self) -> bool:
        return (
            self._raining and self.config.settings and self._settings.get("rain_detection") is True
        )

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
        backwards by at least ``unstuck_backward_m``; then it is ``idle`` again, or, when the
        error interrupted a job, free to drive with the error kept until ``ResumeMow`` moved it
        ``resume_clear_m`` on its route (like a LUBA)."""
        self._backed_off_m = 0.0
        self._unblocked = False
        self._resuming = False
        self._resume_moved_m = 0.0
        self._enter_error(STUCK_ERROR_CODE, message or "the mower is stuck on an obstacle")

    def lift(self) -> None:
        """Trigger the lift sensor: blades and wheels stop, the mower reports an error. Only
        :meth:`clear_error` (someone puts the mower down and confirms) ends it."""
        self._enter_error(LIFT_ERROR_CODE, "the mower was lifted")

    def clear_error(self) -> None:
        """Clear an error. A job that was running stays paused until resumed."""
        status = self._status
        self._unblocked = self._resuming = False
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
        if self._rain_blocks():
            return Outcome.rejected(RAIN_BLOCKS_START)
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
        skipped, skipped_m = _skip_to_progress(legs, job.start_progress_pct or 0, mow_length)
        stuck_after = self.config.stuck_after_seconds
        if stuck_after is not None and job.obstacle_detection is not None:
            stuck_after *= _STUCK_FACTOR.get(job.obstacle_detection, 1.0)

        def begin() -> None:
            status = self._status
            self._end_drive_session()
            self._job = _Job(
                area_ids=job.area_ids,
                pattern=job.pattern,
                mow_length_m=mow_length,
                mowed_m=skipped_m,
                started_s=self._elapsed_s,
                stuck_after_s=stuck_after,
            )
            self.job_options = _job_options(job)
            if skipped:
                # Drive (blades off) to where the skipped part ends, then mow on from there.
                self._path, self._path_done = path, skipped - 1
                status.legs = [_Leg(legs[skipped - 1].target, blade=False), *legs[skipped:]]
            else:
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
                started_s=self._elapsed_s,
                stuck_after_s=self.config.stuck_after_seconds,
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
        self._unblocked = self._resuming = False
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
        if linear < 0 and not self.config.drive_reverse:
            linear = 0.0  # this drive link cannot reverse
        command = _DriveCommand(
            linear=max(-1.0, min(1.0, linear)),
            angular=max(-1.0, min(1.0, angular)),
            blade_on=allowed,
        )
        self._blade_on = allowed
        delay = self.config.drive_response_delay_s
        if delay > 0:
            self._drive_pending.append((self._elapsed_s + delay, command))
        else:
            self._drive_cmd = command

    def _drive_stop(self) -> None:
        self._drive_pending.clear()
        self._drive_cmd = _DriveCommand()
        self._blade_on = False

    def _apply_due_drive(self, now_s: float, limit: float) -> float:
        """Follow the delayed drive frames due at ``now_s``; returns how far the simulation may
        advance before the next one is due (at most ``limit``)."""
        pending = self._drive_pending
        while pending and pending[0][0] <= now_s + 1e-9:
            self._drive_cmd = pending.pop(0)[1]
        if pending:
            return max(1e-6, min(limit, pending[0][0] - now_s))
        return limit

    def _drive_closed(self, session: SimulatedDriveSession) -> None:
        if self._drive is session:
            self._drive = None
        self._publish()

    def _end_drive_session(self) -> None:
        self._undock_target = None  # a stop, an error or a new command ends the undocking
        if self._drive is not None:
            self._drive._end()
            self._drive = None
        self._drive_pending.clear()
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
        if self.restarting:
            return self._restart_step(t)
        if activity is MowerActivity.UPDATING:
            return self._update_step(t)
        if activity is MowerActivity.ERROR and self._resuming:
            return self._resume_step(t)
        if activity in (MowerActivity.MOWING, MowerActivity.RETURNING):
            if self._rtk_fix is RtkFixStatus.NONE:
                self._blade_on = False
                self._drain(t, self.config.drain_idle_per_h)
                return t
            return self._follow_route(t)
        if activity in (MowerActivity.DOCKED, MowerActivity.CHARGING):
            return self._charge(t)
        if self._undock_target is not None and activity is MowerActivity.IDLE:
            return self._undock_step(t)
        # PAUSED: a stuck mower driven free keeps the interrupted job (``start_drive_session``).
        driveable = activity in (MowerActivity.IDLE, MowerActivity.PAUSED) or self.stuck
        if self._drive is not None and driveable:
            return self._drive_step(t)
        self._drain(t, self.config.drain_idle_per_h)
        return t

    def _restart_step(self, t: float) -> float:
        """The mower boots: ``offline`` until the restart time passed, then back as it was."""
        step = min(t, self._restart_remaining_s)
        self._restart_remaining_s -= step
        if self._restart_remaining_s <= 1e-9:
            self._restart_remaining_s = 0.0
            self._status.activity = self._restart_back
        return step

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
                limit = self._job.stuck_after_s
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

    def _undock_step(self, t: float) -> float:
        target = self._undock_target
        assert target is not None  # noqa: S101 - checked by the caller
        speed = self.config.transfer_speed_mps
        remaining = distance((self._x, self._y), target)
        needed = remaining / speed
        self._drain(min(t, needed), self.config.drain_moving_per_h)
        self._moving_speed = speed
        if needed <= t:
            self._x, self._y = target
            self._undock_target = None
            return needed
        fraction = t / needed
        self._x += (target[0] - self._x) * fraction
        self._y += (target[1] - self._y) * fraction
        return t

    def _drive_step(self, t: float) -> float:
        step = min(t, _DRIVE_STEP_S)
        cmd = self._drive_cmd
        if cmd.linear == 0 and cmd.angular == 0:
            self._drain(t, self.config.drain_idle_per_h)
            return t
        if self.stuck and not self._unblocked:
            return self._stuck_step(step, cmd)
        self._theta += cmd.angular * self.config.drive_max_turn_rad_s * step
        speed = cmd.linear * self.drive_full_speed_mps
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
        speed = cmd.linear * self.drive_full_speed_mps
        nx = self._x + math.cos(self._theta) * speed * step
        ny = self._y + math.sin(self._theta) * speed * step
        if any(point_in_polygon((nx, ny), z.boundary) for z in self._map.forbidden_zones()):
            return step
        self._x, self._y = nx, ny
        self._moving_speed = abs(speed)
        self._backed_off_m += abs(speed) * step
        if self._backed_off_m >= self.config.unstuck_backward_m - 1e-9:
            status = self._status
            if status.resume_activity is not None:
                # Like a LUBA: free to move, but the error lasts until the job resumes.
                self._unblocked = True
                _log.info("simulated mower %s is free, the error stays", self.mower_id)
            else:
                status.activity = MowerActivity.IDLE
                status.error_code = None
                status.error_message = None
                _log.info("simulated mower %s is free again", self.mower_id)
            self._publish()
        return step

    def _resume_stuck(self) -> Outcome:
        """``ResumeMow`` while a stuck error interrupts the job: accepted; the route continues
        once the mower is free and the error clears after ``resume_clear_m``."""
        self._end_drive_session()
        self._resuming = True
        self._resume_moved_m = 0.0
        self._publish()
        return Outcome.accepted("resume sent; the error clears once the mower moved")

    def _resume_step(self, t: float) -> float:
        """The resumed route while the stuck error lasts (blades off): wheels spin until the
        mower was freed, then the error clears after ``resume_clear_m``."""
        if not self._unblocked:
            self._drain(t, self.config.drain_moving_per_h)
            return t
        before = (self._x, self._y)
        used = self._follow_route(t)
        self._blade_on = False
        self._resume_moved_m += distance(before, (self._x, self._y))
        status = self._status
        if self._resume_moved_m >= self.config.resume_clear_m - 1e-9 or not status.legs:
            status.activity = status.resume_activity or MowerActivity.MOWING
            status.resume_activity = None
            status.error_code = None
            status.error_message = None
            self._unblocked = self._resuming = False
            _log.info("simulated mower %s resumed, the error cleared", self.mower_id)
            self._publish()
        return used

    def _publish(self) -> None:
        self._changed.set()
        if self._connected:
            self._broadcaster.publish(self.snapshot())


def create_simulated_connection(config: ConnectionConfig) -> SimulatedMower:
    """Connection factory for :class:`~mowdeck_contracts.v1.ConnectionKind.SIMULATED`.

    ``config.options`` may override any :class:`SimulationConfig` field, for example
    ``{"time_scale": 60}``. A ``links`` option with a ``ble_bridge`` entry (the additional links
    of the registration, as the Device-Gateway passes them) switches the simulated bridge on; a
    ``base_station`` option (a :class:`~mowdeck_contracts.v1.BaseStationRef`, ADR 0047) links the
    simulated RTK base station. Unknown options raise ``ValueError``.
    """
    options = dict(config.options)
    links = options.pop("links", None)
    base = options.pop(BASE_STATION_OPTION, None)
    names = {f.name for f in fields(SimulationConfig)}
    unknown = sorted(set(options) - names)
    if unknown:
        msg = f"unknown simulator options: {', '.join(unknown)}"
        raise ValueError(msg)
    if isinstance(links, Mapping) and LINK_BLE_BRIDGE in links:
        options.setdefault("bridge", True)  # a registered bridge link: simulate it
    if "bridge_devices" in options:
        options["bridge_devices"] = tuple(
            (str(d[0]), str(d[1]), int(d[2])) for d in options["bridge_devices"]
        )
    if "drive_speed_range" in options:
        low, high, step = options["drive_speed_range"]
        options["drive_speed_range"] = (float(low), float(high), float(step))
    sim_config = replace(SimulationConfig(), **options)
    return SimulatedMower(
        mower_id=config.mower_id,
        account_id=config.account_id,
        config=sim_config,
        base_station=BaseStationRef.model_validate(base) if base is not None else None,
    )


def _skip_to_progress(legs: Sequence[_Leg], percent: int, mow_length: float) -> tuple[int, float]:
    """How many legs a start at ``percent`` skips, and the mowing length they hold: whole legs
    until the next one would pass that share of ``mow_length``; none at 0 %."""
    if percent <= 0 or mow_length <= 0:
        return 0, 0.0
    goal = mow_length * percent / 100.0
    done = 0.0
    here: LocalPoint | None = None
    for index, leg in enumerate(legs):
        length = distance(here, leg.target) if leg.blade and here is not None else 0.0
        if done + length > goal:
            return index, done
        done += length
        here = leg.target
    return len(legs), done


def _job_options(job: NativeJob) -> dict[str, Any]:
    """The job options a native start set, as ``MowPattern`` keys."""
    options: dict[str, Any] = {
        "obstacle_detection": job.obstacle_detection.value if job.obstacle_detection else None,
        "obstacle_laps": job.obstacle_laps,
        "order": job.order.value if job.order else None,
        "start_progress_pct": job.start_progress_pct,
    }
    return {key: value for key, value in options.items() if value is not None}
