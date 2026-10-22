"""Typed parts of :class:`~mowdeck_contracts.v1.MowerState` beyond position and activity: the
running job, the satellite positioning, the mower's own network signal, its self-check and the
category of its error.

Every part is optional on the state and every field inside is nullable: an adapter fills what its
mower reports and leaves the rest null, never a made-up zero. Units are part of the field names
(``_s`` seconds, ``_m2`` square metres, ``_dbm`` dBm).
"""

from enum import StrEnum

from pydantic import Field

from mowdeck_contracts.v1._base import ContractModel


class JobProgress(ContractModel):
    """The mowing job the mower runs right now (``MowerState.job``).

    Null on the state when no job runs or the connection reports none. Times have the
    resolution the vendor reports (Mammotion: whole minutes, given here in seconds).
    """

    progress_percent: int | None = Field(
        default=None, ge=0, le=100, description="Share of the job done, 0 to 100."
    )
    area_id: str | None = Field(
        default=None,
        max_length=128,
        description="The map area (`MowerMap.areas[].id`) the mower works in now; null when it "
        "is not one of the mower's map areas or not reported.",
    )
    elapsed_s: int | None = Field(default=None, ge=0, description="Seconds the job has run so far.")
    remaining_s: int | None = Field(
        default=None, ge=0, description="Seconds the mower estimates the job still needs."
    )
    mowed_m2: float | None = Field(
        default=None, ge=0, description="Area mowed in this job so far, in square metres."
    )


class GnssSolution(StrEnum):
    """The satellite position solution as the receiver reports it (finer than
    :class:`~mowdeck_contracts.v1.RtkFixStatus`, which folds ``single`` into ``none``)."""

    NONE = "none"
    SINGLE = "single"
    """A plain GNSS position without corrections (metres of scatter)."""
    FLOAT = "float"
    FIXED = "fixed"
    UNKNOWN = "unknown"


class GnssStatus(ContractModel):
    """The mower's satellite positioning (``MowerState.gnss``)."""

    satellites: int | None = Field(
        default=None, ge=0, le=255, description="Satellites the receiver uses."
    )
    correction_age_s: float | None = Field(
        default=None,
        ge=0,
        description="Age of the last RTK correction data in seconds; null without corrections.",
    )
    rtk_status: GnssSolution = Field(
        default=GnssSolution.UNKNOWN, description="The position solution of the receiver."
    )


class NetworkKind(StrEnum):
    """How the mower itself reaches the internet (its own uplink, not MowDeck's connection to
    it, which is ``MowerState.connection``)."""

    WIFI = "wifi"
    MOBILE = "mobile"
    BLE = "ble"
    """Only Bluetooth: the mower has no internet uplink of its own right now."""
    NONE = "none"


class SignalStatus(ContractModel):
    """The mower's own radio signal (``MowerState.signal``)."""

    wifi_dbm: int | None = Field(
        default=None, ge=-150, le=-1, description="Wi-Fi signal in dBm; null when not reported."
    )
    mobile_dbm: int | None = Field(
        default=None,
        ge=-150,
        le=-1,
        description="Mobile network (LTE) signal in dBm; null when not reported.",
    )
    network: NetworkKind | None = Field(
        default=None, description="The uplink the mower uses now; null when not reported."
    )


class SelfCheckResult(StrEnum):
    """Outcome of the mower's start self-check."""

    OK = "ok"
    """Nothing stops the mower from starting."""
    BLOCKED = "blocked"
    """A condition (``SelfCheck.detail``) stops the mower from starting a job."""


class SelfCheck(ContractModel):
    """The mower's start self-check (``MowerState.self_check``)."""

    result: SelfCheckResult
    detail: str | None = Field(
        default=None,
        max_length=64,
        description="With `blocked`: what stops the mower as a lower-case key, e.g. `rain`, "
        "`lifted`, `low_battery`, `outside_task_area`, `non_working_hours`, `rtk_not_ready`; "
        "the adapter's documentation lists its keys. Null with `ok`.",
    )


class MowerErrorCategory(StrEnum):
    """What kind of fault ``MowerState.error_code`` names, manufacturer neutral."""

    TRAPPED = "trapped"
    OBSTACLE = "obstacle"
    OUTSIDE = "outside"
    WHEEL_SLIP = "wheel_slip"
    LIFT_TILT = "lift_tilt"
    COLLISION = "collision"
    BATTERY = "battery"
    BLADE_MOTOR = "blade_motor"
    RAIN = "rain"
    POSITIONING = "positioning"
    OTHER = "other"
