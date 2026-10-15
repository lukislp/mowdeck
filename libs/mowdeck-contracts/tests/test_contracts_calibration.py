"""Drive calibration contracts (ADR 0042): link options round trip, the speed-setting rescale,
the speed-setting capability and the new commands."""

from datetime import UTC, datetime

import pytest
from mowdeck_contracts.v1 import (
    DRIVE_CALIBRATION_OPTION_KEYS,
    DriveCalibrate,
    DriveCalibration,
    DriveCalibrationStep,
    DriveSpeedSetting,
    MowerCapabilities,
    SetDriveSpeed,
    calibration_of_links,
)
from pydantic import TypeAdapter, ValidationError

AT = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def test_contracts_calibration_options_round_trip() -> None:
    calibration = DriveCalibration(
        full_speed_mps=0.12,
        full_yaw_rate_dps=3.4,
        dead_zone=0.15,
        turn_dead_zone=0.2,
        drive_speed_setting=0.3,
        calibrated_at=AT,
    )
    options = calibration.to_options()
    assert set(options) == DRIVE_CALIBRATION_OPTION_KEYS
    assert DriveCalibration.from_options({"host": "bridge", **options}) == calibration


def test_contracts_calibration_from_options_is_lenient() -> None:
    assert DriveCalibration.from_options(None) is None
    assert DriveCalibration.from_options({"host": "bridge"}) is None
    assert DriveCalibration.from_options({"drive_full_speed_mps": "fast"}) is None
    assert DriveCalibration.from_options({"drive_full_speed_mps": 0.2}) is None  # no yaw, date


def test_contracts_calibration_rescales_with_the_speed_setting() -> None:
    calibration = DriveCalibration(
        full_speed_mps=0.1, full_yaw_rate_dps=3.0, drive_speed_setting=0.2, calibrated_at=AT
    )
    assert calibration.full_speed_at(0.4) == pytest.approx(0.2)
    assert calibration.full_speed_at(None) == pytest.approx(0.1)
    assert calibration.setting_changed(0.4) and not calibration.setting_changed(0.2)
    unknown = calibration.model_copy(update={"drive_speed_setting": None})
    assert unknown.full_speed_at(0.4) == pytest.approx(0.1)
    assert not unknown.setting_changed(0.4)


def test_contracts_calibration_of_links_takes_the_newest() -> None:
    old = DriveCalibration(full_speed_mps=0.1, full_yaw_rate_dps=3.0, calibrated_at=AT)
    new = old.model_copy(update={"full_speed_mps": 0.2, "calibrated_at": AT.replace(hour=13)})
    links = {"ble_bridge": new.to_options(), "other": old.to_options(), "none": {"host": "x"}}
    assert calibration_of_links(links) == new
    assert calibration_of_links({}) is None


def test_contracts_drive_speed_setting_and_commands() -> None:
    setting = DriveSpeedSetting(min_mps=0.2, max_mps=1.2, step_mps=0.1)
    assert setting.clamp(0.04) == 0.2 and setting.clamp(0.46) == 0.5 and setting.clamp(9) == 1.2
    with pytest.raises(ValidationError):
        DriveSpeedSetting(min_mps=1.0, max_mps=0.5)
    caps = MowerCapabilities(drive_speed_setting=setting)
    assert MowerCapabilities.model_validate_json(caps.model_dump_json()) == caps
    assert MowerCapabilities().drive_speed_setting is None
    adapter: TypeAdapter[DriveCalibrate | SetDriveSpeed] = TypeAdapter(
        DriveCalibrate | SetDriveSpeed
    )
    assert adapter.validate_python({"kind": "drive_calibrate"}) == DriveCalibrate(step_s=6.0)
    assert adapter.validate_python({"kind": "set_drive_speed", "speed_mps": 0.4}) == (
        SetDriveSpeed(speed_mps=0.4)
    )
    for too_short_or_long in (2.5, 4.9, 12.5):
        with pytest.raises(ValidationError):
            DriveCalibrate(step_s=too_short_or_long)
    step = DriveCalibrationStep(kind="forward", command=0.5, duration_s=6.0)
    assert step.outcome == "measured" and step.note is None and step.window_s is None
    invalid = DriveCalibrationStep(
        kind="turn", command=-0.3, duration_s=6.0, reports=2, outcome="invalid", note="few"
    )
    assert DriveCalibrationStep.model_validate_json(invalid.model_dump_json()) == invalid
