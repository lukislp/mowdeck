"""Mower state (telemetry) and mower commands.

Value ranges here are generic sanity limits shared by all mower models. Model-specific limits
(for example the exact blade height range of a LUBA 2) are enforced by the device adapter, which
answers with a rejected :class:`~mowdeck_contracts.v1.envelopes.CommandResult`.
"""

from enum import StrEnum
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime
from mowdeck_contracts.v1.calibration import MAX_CALIBRATION_STEP_S, DriveCalibrationStatus
from mowdeck_contracts.v1.firmware import FirmwareInfo
from mowdeck_contracts.v1.links import LinkStatus
from mowdeck_contracts.v1.spot import (
    MAX_SPOT_MOW_SPEED_MPS,
    SpotCircle,
    SpotMowPlan,
    SpotPolygon,
    SpotRunStatus,
)


class MowerActivity(StrEnum):
    """What the mower is doing right now."""

    IDLE = "idle"
    MOWING = "mowing"
    PAUSED = "paused"
    RETURNING = "returning"
    DOCKED = "docked"
    CHARGING = "charging"
    ERROR = "error"
    OFFLINE = "offline"
    UPDATING = "updating"
    """A firmware update runs; every command but the emergency stop is refused meanwhile."""
    UNKNOWN = "unknown"


class RtkFixStatus(StrEnum):
    """Quality of the RTK position fix."""

    NONE = "none"
    FLOAT = "float"
    FIXED = "fixed"
    UNKNOWN = "unknown"


class RtkSource(StrEnum):
    """Where RTK correction data comes from."""

    BASE_STATION = "base_station"
    NETWORK = "network"
    UNKNOWN = "unknown"


class PositionSource(StrEnum):
    """Where :attr:`MowerState.position` comes from, so consumers can judge it.

    ``rtk`` is the mower's own positioning with an RTK fix (fixed or float), ``gnss`` the same
    positioning without an RTK fix (single point, metres of scatter), ``dock`` the known pose of
    the charging station while the mower stands in it (the adapter reports the dock instead of
    the scattering raw position), ``unknown`` when there is no position or its quality is not
    known.
    """

    RTK = "rtk"
    GNSS = "gnss"
    DOCK = "dock"
    UNKNOWN = "unknown"


class ConnectionKind(StrEnum):
    """How the Device-Gateway is connected to the mower."""

    BLE_BRIDGE = "ble_bridge"
    WIFI = "wifi"
    CLOUD = "cloud"
    SIMULATED = "simulated"


class GeoPosition(ContractModel):
    """A WGS84 position."""

    lat: float = Field(ge=-90, le=90, description="Latitude in degrees.")
    lon: float = Field(ge=-180, le=180, description="Longitude in degrees.")
    alt: float | None = Field(default=None, description="Altitude in metres, if known.")
    heading: float | None = Field(
        default=None, ge=0, lt=360, description="Heading in degrees, 0 = north, clockwise."
    )


class MowerState(ContractModel):
    """A telemetry snapshot of one mower, published on ``telemetry.mower.<id>``.

    Two times: ``observed_at`` is when the mower reported the values, ``heartbeat_at`` when the
    gateway's link to it was last alive. A docked mower reports rarely, so old values do not mean
    an offline mower (ADR 0040).
    """

    mower_id: UUID
    account_id: UUID
    observed_at: UtcDateTime = Field(description="When the device reported this state (UTC).")
    heartbeat_at: UtcDateTime | None = Field(
        default=None,
        description="When the Device-Gateway last knew its connection to the mower alive (UTC). "
        "The gateway sets it on every publish, never an adapter: it advances with each heartbeat "
        "while `observed_at` stays the time of the mower's last report. Clients judge online and "
        "offline by it (ADR 0040); null in states from before the field, then `observed_at` "
        "stands in.",
    )
    connected: bool = Field(
        default=True,
        description="False in the state the Device-Gateway publishes right after its connection "
        "to the mower ended: the last known values, with `heartbeat_at` the last time the link "
        "was alive. Set by the gateway, never by an adapter.",
    )
    activity: MowerActivity
    battery_percent: int = Field(ge=0, le=100)
    charging: bool = False
    position: GeoPosition | None = None
    rtk_fix: RtkFixStatus = RtkFixStatus.UNKNOWN
    rtk_source: RtkSource = RtkSource.UNKNOWN
    position_source: PositionSource = Field(
        default=PositionSource.UNKNOWN,
        description="Where `position` comes from; `unknown` whenever `position` is null.",
    )
    connection: ConnectionKind
    blade_height_mm: int | None = Field(default=None, ge=0, le=200)
    speed_mps: float | None = Field(default=None, ge=0, le=5)
    error_code: str | None = Field(default=None, max_length=64)
    error_message: str | None = Field(default=None, max_length=500)
    drive_recoverable: bool = Field(
        default=False,
        description="With activity `error`: the error is not a safety sensor stop, so manual "
        "driving may free the mower (stuck, wheel slip). Lift, tilt and collision stops are "
        "never drive recoverable; the controller refuses drive frames for them.",
    )
    firmware: str | None = Field(default=None, max_length=64)
    light_on: bool | None = Field(
        default=None,
        description="Whether the mower's light (the camera's headlamp) is on, as the mower "
        "reports it; null when not reported (no light, or no answer yet).",
    )
    links: dict[str, LinkStatus] = Field(
        default_factory=dict,
        description="State of the mower's additional links by kind (e.g. `ble_bridge`); empty "
        "for a mower with only its primary connection.",
    )
    firmware_info: FirmwareInfo | None = Field(
        default=None,
        description="Installed and available firmware and the update state; null when the "
        "adapter has not checked (or cannot check) the vendor.",
    )
    spot_run: SpotRunStatus | None = Field(
        default=None,
        description="The spot run MowDeck drives on this mower right now (`SpotMow`), with its "
        "final state kept until the next run; null when there never was one.",
    )
    drive_calibration: DriveCalibrationStatus | None = Field(
        default=None,
        description="The drive calibration MowDeck runs on this mower (`DriveCalibrate`, ADR "
        "0042), with its final state and result kept until the next one; null when there never "
        "was one.",
    )
    drive_speed_mps: float | None = Field(
        default=None,
        gt=0,
        le=5,
        description="The vendor's speed setting for manual driving "
        "(`MowerCapabilities.drive_speed_setting`); null when the connection has none or has "
        "not read it yet.",
    )
    raw: dict[str, Any] = Field(
        default_factory=dict,
        description="Adapter specific extras. Consumers must not rely on its structure.",
    )

    @model_validator(mode="after")
    def _source_needs_position(self) -> Self:
        if self.position is None and self.position_source is not PositionSource.UNKNOWN:
            msg = "position_source must be unknown when position is null"
            raise ValueError(msg)
        return self


# --- Commands ---------------------------------------------------------------------------------


NATIVE_PATTERN_NAMES: tuple[str, ...] = (
    "stripes",
    "diagonal",
    "checkerboard",
    "edge_only",
    "random_angle",
)
"""Pattern names a mower may run natively on its own map areas (``native_patterns``)."""

MAX_BORDER_LAPS = 4
"""Most border laps a native job runs (LUBA: 0 to 4)."""


class MowPattern(ContractModel):
    """The pattern settings of a native start (``StartMow.pattern``) that every client offers:
    the pattern, its base angle, the border laps and the path spacing.

    Adapters may accept further keys in ``StartMow.pattern``; this is the common, validated
    part, also kept per mower as the mowing defaults in Fleet (``MowerOut.mow_defaults``).
    Whether the mower runs the pattern is decided with ``MowerCapabilities.native_patterns``.
    """

    name: Literal["stripes", "diagonal", "checkerboard", "edge_only", "random_angle"] = Field(
        default="stripes", description="The native pattern."
    )
    angle_deg: float = Field(
        default=0.0,
        ge=0,
        le=180,
        description="Base angle of the stripes in degrees (diagonal adds 45, checkerboard "
        "crosses at 90; ignored for `edge_only` and `random_angle`).",
    )
    border_laps: int = Field(
        default=1,
        ge=0,
        le=MAX_BORDER_LAPS,
        description="Laps along the area edge before the fill (`edge_only`: at least one).",
    )
    spacing_m: float | None = Field(
        default=None,
        ge=0.05,
        le=2.0,
        description="Distance between neighbouring paths in metres; None for the mower's own "
        "default. Model limits: `MowerCapabilities.path_spacing_range_mm`.",
    )


class StartMow(ContractModel):
    """Start mowing the given areas, a stored plan or an inline spot plan (exactly one)."""

    kind: Literal["start_mow"] = "start_mow"
    area_ids: list[str] = Field(default_factory=list, description="Map area ids to mow, in order.")
    plan_ref: str | None = Field(default=None, description="Reference to a stored mowing plan.")
    pattern: dict[str, Any] | None = Field(
        default=None,
        description="Pattern settings (validated by the pattern's own JSON Schema).",
    )
    spot_plan: SpotMowPlan | None = Field(
        default=None,
        description="A route computed by the Planner (spot mowing). Runs only on mowers with "
        "`custom_paths`, or natively when the plan carries a `native` job.",
    )

    @model_validator(mode="after")
    def _exactly_one_target(self) -> Self:
        targets = [bool(self.area_ids), self.plan_ref is not None, self.spot_plan is not None]
        if sum(targets) != 1:
            msg = "exactly one of area_ids (non-empty), plan_ref or spot_plan must be given"
            raise ValueError(msg)
        return self


class PauseMow(ContractModel):
    """Pause the current job."""

    kind: Literal["pause_mow"] = "pause_mow"


class ResumeMow(ContractModel):
    """Resume a paused job."""

    kind: Literal["resume_mow"] = "resume_mow"


class ReturnToDock(ContractModel):
    """Pause or end the job and drive back to the charging station."""

    kind: Literal["return_to_dock"] = "return_to_dock"
    end_job: bool = False
    """Also end the running or paused job, so it does not resume later."""


class EmergencyStop(ContractModel):
    """Stop blades and wheels immediately. Always sent with ``priority = emergency``."""

    kind: Literal["emergency_stop"] = "emergency_stop"


class SetBladeHeight(ContractModel):
    """Set the cutting height."""

    kind: Literal["set_blade_height"] = "set_blade_height"
    mm: int = Field(ge=10, le=120, description="Blade height in millimetres.")


class SetSpeed(ContractModel):
    """Set the driving speed while mowing."""

    kind: Literal["set_speed"] = "set_speed"
    mps: float = Field(gt=0, le=2, description="Speed in metres per second.")


class SetLight(ContractModel):
    """Switch the mower's light (the camera's headlamp on Mammotion mowers) on or off.

    Needs ``MowerCapabilities.light``. Allowed in every activity, also during an error: the light
    is harmless and AI assist switches it on to photograph a stuck mower at night.
    """

    kind: Literal["set_light"] = "set_light"
    on: bool = Field(description="True switches the light on, False off.")


class FirmwareUpdate(ContractModel):
    """Install the firmware version the vendor offers (``MowerState.firmware_info.available``).

    The adapter refuses it unless ``update_state`` is ``available`` and ``version`` equals
    ``available`` (a stale client), the mower is docked with enough battery (it need not be
    charging: a full mower stops charging), no job or drive session runs and no update is
    already installing. While it runs the mower reports the activity ``updating``.
    """

    kind: Literal["firmware_update"] = "firmware_update"
    version: str = Field(
        min_length=1, max_length=64, description="The version to install, as offered."
    )


class SpotMow(ContractModel):
    """Mow a small spot by driving the mower along ``plan`` with joystick frames (ADR 0039).

    Unlike ``StartMow.spot_plan`` the mower runs no job of its own: MowDeck's gateway steers it
    over the drive link (needs ``MowerCapabilities.manual_drive``, a fixed RTK position and an
    idle mower, or a docked one when the connection can leave the dock by itself:
    ``MowerCapabilities.undock``, ADR 0041). A transit with the blades off takes the mower from
    where it stands to the start of the plan; with ``return_to_dock`` it is sent home after the
    run (also after an abort that left it healthy, never after a stop). The blades stay off (a
    dry run) unless ``blades`` and ``confirm_blades`` are
    both true and the mower allows blades while driving (``manual_drive_blades``). ``spot``
    records what the user chose; the route is ``plan``. Progress arrives in
    ``MowerState.spot_run``; ``SpotStop`` ends the run.
    """

    kind: Literal["spot_mow"] = "spot_mow"
    plan: SpotMowPlan
    blades: bool = Field(default=False, description="Cut while driving (else a dry run).")
    confirm_blades: bool = Field(
        default=False,
        description="The user confirmed the blades separately; required with `blades`.",
    )
    speed_mps: float = Field(
        default=0.2,
        gt=0,
        le=MAX_SPOT_MOW_SPEED_MPS,
        description="Driving speed of the run in metres per second (at most 0.3).",
    )
    spot: SpotCircle | SpotPolygon | None = Field(
        default=None, description="The spot as the user chose it (audit only)."
    )
    return_to_dock: bool = Field(
        default=True,
        description="Send the mower back to its charging station after the run (also after an "
        "abort that left it healthy; never after a stop).",
    )

    @model_validator(mode="after")
    def _blades_confirmed(self) -> Self:
        if self.blades and not self.confirm_blades:
            msg = "blades need confirm_blades"
            raise ValueError(msg)
        return self


class SpotStop(ContractModel):
    """End the running spot run or drive calibration: zero frame, blades off, the mower stays
    where it is."""

    kind: Literal["spot_stop"] = "spot_stop"


class DriveCalibrate(ContractModel):
    """Measure what the stick values mean on the ground (ADR 0042).

    MowDeck drives a few short steps with the blades off (forward at ``linear`` 0.3, 0.5, 0.7
    and 1.0, then turns in place at ``angular`` ±0.3, ±0.6 and ±1.0, each ``step_s`` long, zero
    frames in between) and fits the full-scale ground speed, the full-scale yaw rate and the
    dead zone. Needs ``MowerCapabilities.manual_drive``, an idle mower out of its dock with a
    fixed RTK position and about 2 m of free lawn straight ahead. Progress and the result arrive
    in ``MowerState.drive_calibration``; ``SpotStop`` and ``EmergencyStop`` end it.
    """

    kind: Literal["drive_calibrate"] = "drive_calibrate"
    step_s: float = Field(
        default=2.5,
        ge=1.0,
        le=MAX_CALIBRATION_STEP_S,
        description="Duration of each step in seconds.",
    )


class SetDriveSpeed(ContractModel):
    """Change the vendor's speed setting for manual driving
    (``MowerCapabilities.drive_speed_setting``; the mower scales the stick by it)."""

    kind: Literal["set_drive_speed"] = "set_drive_speed"
    speed_mps: float = Field(gt=0, le=5)


MANUAL_DRIVE_TTL_MIN_MS = 100
MANUAL_DRIVE_TTL_MAX_MS = 2000


class ManualDrive(ContractModel):
    """One joystick sample. The mower stops when no new sample arrives within ``ttl_ms``.

    This is the dead-man switch: clients resend samples continuously while the stick is held.
    """

    kind: Literal["manual_drive"] = "manual_drive"
    linear: float = Field(ge=-1, le=1, description="Forward (+) / backward (-), normalised.")
    angular: float = Field(ge=-1, le=1, description="Turn left (+) / right (-), normalised.")
    blade_on: bool = False
    ttl_ms: int = Field(
        default=1000,
        ge=MANUAL_DRIVE_TTL_MIN_MS,
        le=MANUAL_DRIVE_TTL_MAX_MS,
        description="Dead-man timeout in milliseconds.",
    )


MowerCommand = Annotated[
    StartMow
    | PauseMow
    | ResumeMow
    | ReturnToDock
    | EmergencyStop
    | SetBladeHeight
    | SetSpeed
    | SetLight
    | FirmwareUpdate
    | SpotMow
    | SpotStop
    | DriveCalibrate
    | SetDriveSpeed
    | ManualDrive,
    Field(discriminator="kind"),
]
"""Discriminated union of all mower commands (discriminator: ``kind``)."""

COMMAND_MODELS: tuple[type[ContractModel], ...] = (
    StartMow,
    PauseMow,
    ResumeMow,
    ReturnToDock,
    EmergencyStop,
    SetBladeHeight,
    SetSpeed,
    SetLight,
    FirmwareUpdate,
    SpotMow,
    SpotStop,
    DriveCalibrate,
    SetDriveSpeed,
    ManualDrive,
)
"""All command model classes, in a stable order."""
