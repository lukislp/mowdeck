"""Drive calibration and the manual-driving speed setting (ADR 0042).

A spot run (ADR 0039) steers a mower with normalised stick values (``linear``, ``angular``,
-1 to 1). What a stick value means on the ground differs per model, firmware and vendor setting,
so MowDeck measures it: a *drive calibration* (``DriveCalibrate``) drives a few short, fixed
steps (forward at several stick values, turns in place both ways), measures ground speed and yaw
rate from the RTK positions and the heading, and fits

* ``full_speed_mps``: the ground speed at ``linear = 1``;
* ``full_yaw_rate_dps``: the yaw rate at ``angular = ±1``;
* ``dead_zone`` and ``turn_dead_zone``: the stick value below which the mower does not move or
  turn (``speed(c) = full * (c - dead_zone) / (1 - dead_zone)`` for ``c > dead_zone``).

The result travels in ``MowerState.drive_calibration`` while it runs and afterwards; a client
stores the fitted values as options of the drive link (:meth:`DriveCalibration.to_options`, keys
:data:`DRIVE_CALIBRATION_OPTION_KEYS`), and the Device-Gateway hands them to every spot run.

Some vendors scale the stick by a speed setting for manual driving (the "movement speed" of an
app). A connection that can read and change it declares ``MowerCapabilities.drive_speed_setting``
(:class:`DriveSpeedSetting`), reports the current value in ``MowerState.drive_speed_mps`` and
accepts ``SetDriveSpeed``. A calibration records the setting it was measured at
(``drive_speed_setting``), so a spot run can rescale the calibrated speed when the setting
changes later (:meth:`DriveCalibration.full_speed_at`).

Nothing here is specific to a manufacturer.
"""

import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final, Literal, Self
from uuid import UUID

from pydantic import Field, ValidationError, model_validator

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime

DRIVE_CALIBRATION_OPTION_KEYS: Final = frozenset(
    {
        "drive_full_speed_mps",
        "drive_full_yaw_rate_dps",
        "drive_dead_zone",
        "drive_turn_dead_zone",
        "drive_speed_setting",
        "calibrated_at",
    }
)
"""Link option keys of a stored :class:`DriveCalibration` (``MowerRegistration.links``). The
Device-Gateway takes them off the options before it opens a link (an adapter never sees them)
and a change of only these keys does not reopen the link."""

MAX_CALIBRATION_STEP_S: Final = 5.0
"""Longest step of a calibration (forward or turn)."""


class DriveSpeedSetting(ContractModel):
    """The vendor's speed setting for manual driving, as far as the connection can change it
    (``MowerCapabilities.drive_speed_setting``): the accepted range and step in m/s."""

    min_mps: float = Field(gt=0, le=5)
    max_mps: float = Field(gt=0, le=5)
    step_mps: float = Field(default=0.1, gt=0, le=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.min_mps > self.max_mps:
            msg = "min_mps must not exceed max_mps"
            raise ValueError(msg)
        return self

    def clamp(self, value: float) -> float:
        """``value`` within the range, rounded to the step."""
        steps = round((value - self.min_mps) / self.step_mps)
        snapped = self.min_mps + steps * self.step_mps
        return round(min(self.max_mps, max(self.min_mps, snapped)), 3)


class DriveCalibration(ContractModel):
    """The fitted drive behaviour of one mower, as stored in the drive link's options."""

    full_speed_mps: float = Field(gt=0, le=5, description="Ground speed at `linear = 1`.")
    full_yaw_rate_dps: float = Field(
        gt=0, le=720, description="Yaw rate at `angular = ±1` in degrees per second."
    )
    dead_zone: float = Field(
        default=0.0, ge=0, le=0.9, description="`linear` below which the mower stands."
    )
    turn_dead_zone: float = Field(
        default=0.0, ge=0, le=0.9, description="`angular` below which the mower does not turn."
    )
    drive_speed_setting: float | None = Field(
        default=None,
        gt=0,
        le=5,
        description="The vendor's manual-driving speed setting during the calibration "
        "(`MowerState.drive_speed_mps`); null when the connection has none.",
    )
    calibrated_at: UtcDateTime

    def to_options(self) -> dict[str, Any]:
        """The link options (keys :data:`DRIVE_CALIBRATION_OPTION_KEYS`)."""
        options: dict[str, Any] = {
            "drive_full_speed_mps": round(self.full_speed_mps, 4),
            "drive_full_yaw_rate_dps": round(self.full_yaw_rate_dps, 3),
            "drive_dead_zone": round(self.dead_zone, 3),
            "drive_turn_dead_zone": round(self.turn_dead_zone, 3),
            "calibrated_at": self.calibrated_at.isoformat(),
        }
        if self.drive_speed_setting is not None:
            options["drive_speed_setting"] = round(self.drive_speed_setting, 3)
        return options

    @classmethod
    def from_options(cls, options: Mapping[str, Any] | None) -> "DriveCalibration | None":
        """The calibration stored in ``options``; None when there is none or it is invalid."""
        if not options or "drive_full_speed_mps" not in options:
            return None
        try:
            return cls.model_validate(
                {
                    "full_speed_mps": options["drive_full_speed_mps"],
                    "full_yaw_rate_dps": options.get("drive_full_yaw_rate_dps"),
                    "dead_zone": options.get("drive_dead_zone", 0.0),
                    "turn_dead_zone": options.get("drive_turn_dead_zone", 0.0),
                    "drive_speed_setting": options.get("drive_speed_setting"),
                    "calibrated_at": options.get("calibrated_at"),
                }
            )
        except (ValidationError, TypeError):
            return None

    def full_speed_at(self, drive_speed_setting: float | None) -> float:
        """The full-scale ground speed at the current speed setting: proportional to the
        setting when both the calibration and the mower know it (the vendor scales the stick
        by it), the calibrated value otherwise."""
        if (
            drive_speed_setting is None
            or self.drive_speed_setting is None
            or drive_speed_setting <= 0
        ):
            return self.full_speed_mps
        return self.full_speed_mps * drive_speed_setting / self.drive_speed_setting

    def setting_changed(self, drive_speed_setting: float | None) -> bool:
        """True when the mower's speed setting differs from the one measured at."""
        if drive_speed_setting is None or self.drive_speed_setting is None:
            return False
        return not math.isclose(drive_speed_setting, self.drive_speed_setting, abs_tol=1e-3)


CalibrationStepKind = Literal["forward", "turn"]


class DriveCalibrationStep(ContractModel):
    """One measured step of a calibration."""

    kind: CalibrationStepKind
    command: float = Field(ge=-1, le=1, description="`linear` (forward) or `angular` (turn).")
    duration_s: float = Field(ge=0, le=60)
    measured: float | None = Field(
        default=None,
        description="Ground speed in m/s (forward) or yaw rate in degrees per second, left "
        "positive (turn); null when too few reports arrived.",
    )
    reports: int = Field(default=0, ge=0, description="Position reports during the step.")
    report_interval_s: float | None = Field(
        default=None, ge=0, description="Mean time between those reports."
    )
    distance_m: float = Field(default=0.0, ge=0, description="Ground covered during the step.")
    turned_deg: float = Field(default=0.0, description="Heading change during the step.")
    skipped: bool = Field(
        default=False,
        description="Not driven: the forward distance budget was used up (room ahead).",
    )


class DriveCalibrationResult(ContractModel):
    """What a calibration measured and fitted."""

    calibration: DriveCalibration
    steps: list[DriveCalibrationStep] = Field(default_factory=list, max_length=32)
    forward_m: float = Field(default=0.0, ge=0, description="Ground covered going forward.")


DriveCalibrationState = Literal["preparing", "measuring", "stopping", "done", "aborted", "failed"]
"""Life cycle of a calibration: ``preparing`` (checks), ``measuring`` (driving the steps),
``stopping`` (a stop was requested), then ``done`` (``result`` holds the fit), ``aborted``
(``reason``: ``stopped_by_user``, ``emergency_stop``, ``sensor_stop``, ``geofence``,
``rtk_lost``, ``activity_change``, ``link_lost``, ``frame_rejected``) or ``failed`` (the
measurements did not allow a fit, ``reason`` says why)."""

DRIVE_CALIBRATION_FINAL_STATES: Final[frozenset[str]] = frozenset({"done", "aborted", "failed"})


class DriveCalibrationStatus(ContractModel):
    """Progress of the drive calibration on this mower (``MowerState.drive_calibration``)."""

    run_id: UUID
    state: DriveCalibrationState
    step_index: int = Field(default=0, ge=0, description="The step being driven (0-based).")
    steps_total: int = Field(default=0, ge=0)
    steps: list[DriveCalibrationStep] = Field(
        default_factory=list, max_length=32, description="The steps measured so far."
    )
    reason: str | None = Field(default=None, max_length=200)
    result: DriveCalibrationResult | None = Field(
        default=None, description="The fit, once the calibration is `done`."
    )
    started_at: UtcDateTime | None = None

    @property
    def final(self) -> bool:
        """True once the calibration has ended."""
        return self.state in DRIVE_CALIBRATION_FINAL_STATES


def calibration_of_links(links: Mapping[str, Mapping[str, Any]]) -> DriveCalibration | None:
    """The newest calibration stored on any of ``links`` (``MowerRegistration.links``)."""
    found = [c for c in (DriveCalibration.from_options(o) for o in links.values()) if c]
    if not found:
        return None
    return max(found, key=lambda c: c.calibrated_at or datetime.min)


__all__ = [
    "DRIVE_CALIBRATION_FINAL_STATES",
    "DRIVE_CALIBRATION_OPTION_KEYS",
    "MAX_CALIBRATION_STEP_S",
    "CalibrationStepKind",
    "DriveCalibration",
    "DriveCalibrationResult",
    "DriveCalibrationState",
    "DriveCalibrationStatus",
    "DriveCalibrationStep",
    "DriveSpeedSetting",
    "calibration_of_links",
]
