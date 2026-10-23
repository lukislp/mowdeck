"""RTK base station contracts (ADR 0047)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    BaseStationFirmwareUpdate,
    BaseStationPosition,
    BaseStationRef,
    BaseStationState,
    CommandRequest,
    ConnectionKind,
    DeviceType,
    DiscoveredDevice,
    FirmwareCheckRequest,
    FirmwareInfo,
    GnssSolution,
    MowerActivity,
    MowerCapabilities,
    MowerRegistration,
    MowerState,
    RtkCorrectionLink,
)
from pydantic import ValidationError


def test_discovered_device_type_defaults_from_the_mower_flag() -> None:
    assert DiscoveredDevice(device_name="Luba-1", is_mower=True).device_type is DeviceType.MOWER
    assert DiscoveredDevice(device_name="Pool-1", is_mower=False).device_type is DeviceType.OTHER
    cached = DiscoveredDevice.model_validate({"device_name": "RTK-1", "is_mower": False})
    assert cached.device_type is DeviceType.OTHER
    base = DiscoveredDevice(device_name="RTK-1", is_mower=False, device_type=DeviceType.RTK_BASE)
    assert base.model_dump(mode="json")["device_type"] == "rtk_base"
    assert DiscoveredDevice.model_validate(base.model_dump(mode="json")) == base


def test_state_fields_are_optional_and_bounded() -> None:
    empty = BaseStationState()
    assert empty.correction_link is RtkCorrectionLink.UNKNOWN
    assert empty.rtk_status is GnssSolution.UNKNOWN
    assert empty.link_up is None and empty.position is None and empty.firmware_info is None
    full = BaseStationState(
        base_station_id=uuid4(),
        correction_link=RtkCorrectionLink.LORA,
        link_up=True,
        lora_channel=12,
        satellites=34,
        rtk_status=GnssSolution.FIXED,
        position=BaseStationPosition(lat=48.1, lon=11.5),
        wifi_dbm=-58,
        moved=False,
        firmware="1.2.3.4",
        firmware_info=FirmwareInfo(installed="1.2.3.4", update_state="idle"),
        details_at=datetime(2026, 10, 23, tzinfo=UTC),
    )
    assert BaseStationState.model_validate(full.model_dump(mode="json")) == full
    with pytest.raises(ValidationError):
        BaseStationState(wifi_dbm=0)
    with pytest.raises(ValidationError):
        BaseStationState(satellites=300)
    with pytest.raises(ValidationError):
        BaseStationPosition(lat=91, lon=0)


def test_mower_state_carries_the_base_station() -> None:
    state = MowerState(
        mower_id=uuid4(),
        account_id=uuid4(),
        observed_at=datetime(2026, 10, 23, tzinfo=UTC),
        activity=MowerActivity.DOCKED,
        battery_percent=80,
        connection=ConnectionKind.CLOUD,
        base_station=BaseStationState(correction_link=RtkCorrectionLink.LORA, link_up=True),
    )
    dumped = state.model_dump(mode="json")
    assert dumped["base_station"]["correction_link"] == "lora"
    assert MowerState.model_validate(dumped) == state
    legacy = {k: v for k, v in dumped.items() if k != "base_station"}
    assert MowerState.model_validate(legacy).base_station is None


def test_registration_reference_and_defaults() -> None:
    ref = BaseStationRef(base_station_id=uuid4(), name="Garden base", device_name="RTK-1")
    assert ref.source == "cloud"
    registration = MowerRegistration(
        mower_id=uuid4(),
        account_id=uuid4(),
        name="Luba",
        connection_kind=ConnectionKind.CLOUD,
        base_station=ref,
    )
    assert MowerRegistration.model_validate(registration.model_dump(mode="json")) == registration
    plain = MowerRegistration(
        mower_id=uuid4(), account_id=uuid4(), name="Sim", connection_kind=ConnectionKind.SIMULATED
    )
    assert plain.base_station is None
    with pytest.raises(ValidationError):
        BaseStationRef(base_station_id=uuid4(), name="x", source="other")  # type: ignore[arg-type]


def test_firmware_check_target_and_command() -> None:
    request = FirmwareCheckRequest(mower_id=uuid4(), account_id=uuid4())
    assert request.target == "mower"
    base = FirmwareCheckRequest(mower_id=uuid4(), account_id=uuid4(), target="base_station")
    assert base.model_dump(mode="json")["target"] == "base_station"
    with pytest.raises(ValidationError):
        FirmwareCheckRequest(mower_id=uuid4(), account_id=uuid4(), target="dock")  # type: ignore[arg-type]
    command = CommandRequest.model_validate(
        {"command": {"kind": "base_station_firmware_update", "version": "1.4.0"}}
    ).command
    assert isinstance(command, BaseStationFirmwareUpdate)
    with pytest.raises(ValidationError):
        BaseStationFirmwareUpdate(version="")
    assert MowerCapabilities().base_station_firmware_update is False
