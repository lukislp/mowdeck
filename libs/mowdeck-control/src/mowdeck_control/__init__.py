"""MowDeck control: drive any mower through one interface, with the safety rules built in.

* :class:`MowerConnection` and :class:`DriveSession`: the adapter interface.
* :class:`MowerController`: expiry, de-duplication, emergency stop priority, serialization,
  dead-man switch, blade rules and capability checks for every adapter.
* :class:`SimulatedMower`: a deterministic simulated mower (``ConnectionKind.SIMULATED``).
* :func:`plan_to_native_job`: neutral mowing request to a native mower job.
* :func:`register_connection` and :func:`create_connection`: the adapter registry, extended
  through the entry point group ``mowdeck.connections``.

The package depends only on ``mowdeck-contracts`` and the standard library (Pillow is an
optional extra, ``photo``, for the simulator's still images).
"""

from mowdeck_contracts.v1 import ConnectionKind

from mowdeck_control.broadcast import StateBroadcaster, StateSubscription
from mowdeck_control.connection import (
    CameraCapable,
    ConfirmFn,
    ConnectionConfig,
    Dispatch,
    DriveSession,
    JobCommand,
    MapRebuildable,
    MowerConnection,
    Outcome,
    Photo,
    PhotoCapture,
    TwoPhaseExecution,
    camera_session_of,
    current_map_of,
    dispatch_of,
)
from mowdeck_control.controller import MowerController, StateCallback
from mowdeck_control.errors import (
    CommandRejected,
    ControlError,
    NotConnected,
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
from mowdeck_control.safety import BladeDecision, decide_manual_blade
from mowdeck_control.simulator import (
    STUCK_ERROR_CODE,
    SimulatedDriveSession,
    SimulatedMower,
    SimulationConfig,
    create_simulated_connection,
    default_map,
)
from mowdeck_control.timers import AsyncioTimerFactory, Timer, TimerFactory
from mowdeck_control.translation import (
    MowOrder,
    MowRequest,
    NativeJob,
    Pattern,
    plan_to_native_job,
)

__version__ = "0.1.0"

register_connection(ConnectionKind.SIMULATED, create_simulated_connection)

__all__ = [
    "ENTRY_POINT_GROUP",
    "STUCK_ERROR_CODE",
    "AsyncioTimerFactory",
    "BladeDecision",
    "CameraCapable",
    "CommandRejected",
    "ConfirmFn",
    "ConnectionConfig",
    "ConnectionFactory",
    "ConnectionRegistry",
    "ControlError",
    "Dispatch",
    "DriveSession",
    "JobCommand",
    "MapRebuildable",
    "MowOrder",
    "MowRequest",
    "MowerConnection",
    "MowerController",
    "NativeJob",
    "NotConnected",
    "Outcome",
    "Pattern",
    "Photo",
    "PhotoCapture",
    "SensorStop",
    "SimulatedDriveSession",
    "SimulatedMower",
    "SimulationConfig",
    "StateBroadcaster",
    "StateCallback",
    "StateSubscription",
    "Timer",
    "TimerFactory",
    "TwoPhaseExecution",
    "UnknownConnectionKind",
    "UnsupportedPattern",
    "available_kinds",
    "camera_session_of",
    "create_connection",
    "create_simulated_connection",
    "current_map_of",
    "decide_manual_blade",
    "default_map",
    "default_registry",
    "dispatch_of",
    "flat_png",
    "from_wgs84",
    "local_angle",
    "plan_to_native_job",
    "point_in_polygon",
    "register_connection",
    "render_scene",
    "to_geo_position",
    "to_wgs84",
]
