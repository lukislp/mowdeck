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
from mowdeck_contracts.v1.spot import SpotMowPlan


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
    """A telemetry snapshot of one mower, published on ``telemetry.mower.<id>``."""

    mower_id: UUID
    account_id: UUID
    observed_at: UtcDateTime = Field(description="When the device reported this state (UTC).")
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
    """Stop the job and drive back to the charging station."""

    kind: Literal["return_to_dock"] = "return_to_dock"


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
    ManualDrive,
)
"""All command model classes, in a stable order."""
