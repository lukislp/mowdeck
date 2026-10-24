"""MowDeck control: drive any mower through one interface, with the safety rules built in.

* :class:`MowerConnection` and :class:`DriveSession`: the adapter interface.
* :class:`MowerController`: expiry, de-duplication, emergency stop priority, serialization,
  dead-man switch, blade rules, firmware update rules and capability checks for every adapter.
* :class:`FirmwarePort` and :class:`BridgeScanCapable`: optional capabilities of a connection
  (firmware checks and updates, scans through a Bluetooth bridge, ADR 0038);
  :class:`BaseStationPort` the firmware of the mower's RTK base station (ADR 0047).
* :class:`SpotMowRun`: spot mowing driven by MowDeck along a Planner route (ADR 0039), with
  its geofence and pure-pursuit steering, fed by the generic drive channel and state; it
  undocks, drives a transit planned by :func:`plan_transit` and sends the mower home
  (ADR 0041, :class:`UndockCapable`).
* :class:`DriveCalibrationRun`: measures what the stick values mean on the ground (full-scale
  speed, yaw rate, dead zone) for spot runs (ADR 0042); :class:`DriveSpeedCapable` is the
  optional vendor speed setting for manual driving, :class:`ReportRateCapable` lets it ask for
  reports faster while it measures.
* :class:`MaintenanceCapable`: optional maintenance commands (restart, map sync, dock
  relocation, RTK resync; ADR 0048) with their safety rules in :func:`maintenance_problem`.
* :class:`SimulatedMower`: a deterministic simulated mower (``ConnectionKind.SIMULATED``).
* :func:`plan_to_native_job`: neutral mowing request to a native mower job.
* :func:`register_connection` and :func:`create_connection`: the adapter registry, extended
  through the entry point group ``mowdeck.connections``.

The package depends only on ``mowdeck-contracts`` and the standard library (Pillow is an
optional extra, ``photo``, for the simulator's still images).
"""

from mowdeck_contracts.v1 import ConnectionKind

from mowdeck_control.broadcast import StateBroadcaster, StateSubscription
from mowdeck_control.calibration import (
    CalibrationConfig,
    DriveCalibrationRun,
    calibration_start_problem,
    room_ahead_problem,
)
from mowdeck_control.connection import (
    BaseStationPort,
    BridgeScanCapable,
    CameraCapable,
    CameraSessionRelease,
    ConfirmFn,
    ConnectionConfig,
    Dispatch,
    DriveSession,
    DriveSpeedCapable,
    FirmwarePort,
    FrameDeadlineCapable,
    JobCommand,
    MaintenanceCapable,
    MaintenanceCommand,
    MapRebuildable,
    MowerConnection,
    MowPathSource,
    Outcome,
    PathPreviewCapable,
    Photo,
    PhotoCapture,
    ReportRateCapable,
    SettingsCapable,
    TwoPhaseExecution,
    UndockCapable,
    base_station_port_of,
    bridge_scanner_of,
    camera_session_of,
    current_map_of,
    current_path_of,
    dispatch_of,
    drive_speed_port_of,
    firmware_port_of,
    frame_deadline_of,
    maintenance_port_of,
    release_camera_session_of,
    settings_port_of,
)
from mowdeck_control.controller import MowerController, StateCallback
from mowdeck_control.errors import (
    CommandRejected,
    ControlError,
    NotConnected,
    PathUnavailable,
    PhotoUnavailable,
    SensorStop,
    UnknownConnectionKind,
    UnsupportedPattern,
)
from mowdeck_control.geo import (
    from_wgs84,
    local_angle,
    point_in_polygon,
    to_geo_position,
    to_wgs84,
)
from mowdeck_control.photo import flat_png, render_scene
from mowdeck_control.registry import (
    ENTRY_POINT_GROUP,
    ConnectionFactory,
    ConnectionRegistry,
    available_kinds,
    create_connection,
    default_registry,
    register_connection,
)
from mowdeck_control.safety import (
    FIRMWARE_MIN_BATTERY_PERCENT,
    FIRMWARE_UPDATE_RUNNING,
    BladeDecision,
    base_station_firmware_update_problem,
    decide_manual_blade,
    firmware_update_problem,
    maintenance_problem,
)
from mowdeck_control.simulator import (
    STUCK_ERROR_CODE,
    SimulatedDriveSession,
    SimulatedMower,
    SimulationConfig,
    create_simulated_connection,
    default_map,
)
from mowdeck_control.spot_run import (
    FrameChannel,
    Geofence,
    Pose,
    SpotActions,
    SpotMowRun,
    SpotRunConfig,
    SpotRunReport,
    pose_of,
    refusal_reason,
    spot_start_problem,
    transit_start,
    weak_link_problem,
)
from mowdeck_control.timers import AsyncioTimerFactory, Timer, TimerFactory
from mowdeck_control.transit import (
    DrivableSpace,
    NoRouteError,
    TransitConfig,
    TransitRoute,
    plan_transit,
)
from mowdeck_control.translation import (
    MowOrder,
    MowRequest,
    NativeJob,
    Pattern,
    plan_to_native_job,
    without_undeclared_options,
)

__version__ = "0.1.0"

register_connection(ConnectionKind.SIMULATED, create_simulated_connection)

__all__ = [
    "ENTRY_POINT_GROUP",
    "FIRMWARE_MIN_BATTERY_PERCENT",
    "FIRMWARE_UPDATE_RUNNING",
    "STUCK_ERROR_CODE",
    "AsyncioTimerFactory",
    "BaseStationPort",
    "BladeDecision",
    "BridgeScanCapable",
    "CalibrationConfig",
    "CameraCapable",
    "CameraSessionRelease",
    "CommandRejected",
    "ConfirmFn",
    "ConnectionConfig",
    "ConnectionFactory",
    "ConnectionRegistry",
    "ControlError",
    "Dispatch",
    "DrivableSpace",
    "DriveCalibrationRun",
    "DriveSession",
    "DriveSpeedCapable",
    "FirmwarePort",
    "FrameChannel",
    "FrameDeadlineCapable",
    "Geofence",
    "JobCommand",
    "MaintenanceCapable",
    "MaintenanceCommand",
    "MapRebuildable",
    "MowOrder",
    "MowPathSource",
    "MowRequest",
    "MowerConnection",
    "MowerController",
    "NativeJob",
    "NoRouteError",
    "NotConnected",
    "Outcome",
    "PathPreviewCapable",
    "PathUnavailable",
    "Pattern",
    "Photo",
    "PhotoCapture",
    "PhotoUnavailable",
    "Pose",
    "ReportRateCapable",
    "SensorStop",
    "SettingsCapable",
    "SimulatedDriveSession",
    "SimulatedMower",
    "SimulationConfig",
    "SpotActions",
    "SpotMowRun",
    "SpotRunConfig",
    "SpotRunReport",
    "StateBroadcaster",
    "StateCallback",
    "StateSubscription",
    "Timer",
    "TimerFactory",
    "TransitConfig",
    "TransitRoute",
    "TwoPhaseExecution",
    "UndockCapable",
    "UnknownConnectionKind",
    "UnsupportedPattern",
    "available_kinds",
    "base_station_firmware_update_problem",
    "base_station_port_of",
    "bridge_scanner_of",
    "calibration_start_problem",
    "camera_session_of",
    "create_connection",
    "create_simulated_connection",
    "current_map_of",
    "current_path_of",
    "decide_manual_blade",
    "default_map",
    "default_registry",
    "dispatch_of",
    "drive_speed_port_of",
    "firmware_port_of",
    "firmware_update_problem",
    "flat_png",
    "frame_deadline_of",
    "from_wgs84",
    "local_angle",
    "maintenance_port_of",
    "maintenance_problem",
    "plan_to_native_job",
    "plan_transit",
    "point_in_polygon",
    "pose_of",
    "refusal_reason",
    "register_connection",
    "release_camera_session_of",
    "render_scene",
    "room_ahead_problem",
    "settings_port_of",
    "spot_start_problem",
    "to_geo_position",
    "to_wgs84",
    "transit_start",
    "weak_link_problem",
    "without_undeclared_options",
]
