"""Additional links (Bluetooth bridge scans) and firmware checks (ADR 0038)."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import (
    BRIDGE_SCAN_MAX_TIMEOUT_S,
    LINK_BLE_BRIDGE,
    LINK_KINDS,
    BridgeScanError,
    BridgeScanErrorCode,
    BridgeScanReply,
    BridgeScanRequest,
    CommandEnvelope,
    CommandRequest,
    FirmwareCheckError,
    FirmwareCheckErrorCode,
    FirmwareCheckReply,
    FirmwareCheckRequest,
    FirmwareInfo,
    FirmwareUpdate,
    LinkStatus,
    PrincipalKind,
    PrincipalRef,
    ProxyInfo,
    ScannedDevice,
)
from mowdeck_contracts.v1.subjects import (
    ALL_BRIDGE_SCANS,
    ALL_FIRMWARE_CHECKS,
    bridge_scan_subject,
    firmware_check_subject,
    parse_bridge_scan_subject,
    parse_firmware_check_subject,
    stream_for_subject,
    subject_matches,
)
from pydantic import ValidationError

MOWER = UUID("6f1c2b8e-3d4a-4f5b-8c9d-0e1f2a3b4c5d")
KEY = "base64-noise-psk=="
AT = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def test_link_status_defaults_and_round_trip() -> None:
    assert LinkStatus() == LinkStatus(connected=False, outdated=False)
    status = LinkStatus(
        connected=True,
        since=AT,
        rssi=-62,
        esphome_version="2026.9.1",
        compiled_at="Oct 10 2026, 18:02:11",
        project_version="1",
    )
    assert LinkStatus.model_validate_json(status.model_dump_json()) == status
    with pytest.raises(ValidationError):
        LinkStatus(rssi=50)
    assert frozenset({LINK_BLE_BRIDGE}) == LINK_KINDS and LINK_BLE_BRIDGE == "ble_bridge"


def test_bridge_scan_request_hides_the_key_and_limits_the_timeout() -> None:
    request = BridgeScanRequest(
        mower_id=MOWER, account_id=uuid4(), host="bridge.local", noise_psk=KEY
    )
    assert request.port == 6053 and request.timeout_s == 15 and request.expected_name is None
    assert KEY not in repr(request)
    with pytest.raises(ValidationError):
        BridgeScanRequest(
            mower_id=MOWER,
            account_id=uuid4(),
            host="bridge.local",
            noise_psk=KEY,
            timeout_s=BRIDGE_SCAN_MAX_TIMEOUT_S + 1,
        )
    with pytest.raises(ValidationError):
        BridgeScanRequest(mower_id=MOWER, account_id=uuid4(), host="", noise_psk=KEY)


def test_bridge_scan_reply_carries_proxy_devices_and_error() -> None:
    proxy = ProxyInfo(proxy_name="mowdeck-bridge", esphome_version="2026.9.1", checked_at=AT)
    found = BridgeScanReply(
        proxy=proxy, devices=[ScannedDevice(name="Mower-01", address="FC:23:00:00:00:01", rssi=-62)]
    )
    assert found.ok and found.devices[0].rssi == -62
    assert BridgeScanReply.model_validate_json(found.model_dump_json()) == found
    timed_out = BridgeScanReply(
        proxy=proxy,
        error=BridgeScanError(code=BridgeScanErrorCode.TIMEOUT, detail="no mower advertised"),
    )
    assert not timed_out.ok and timed_out.proxy is not None and timed_out.devices == []
    assert BridgeScanReply().ok
    with pytest.raises(ValidationError):
        BridgeScanReply.model_validate({"error": {"code": "exploded", "detail": "x"}})
    with pytest.raises(ValidationError):
        ScannedDevice(name="", address="FC:23", rssi=-10)


def test_bridge_scan_subjects() -> None:
    subject = bridge_scan_subject(MOWER)
    assert subject == f"gateway.mower.{MOWER}.bridge.scan"
    assert parse_bridge_scan_subject(subject) == MOWER
    assert subject_matches(ALL_BRIDGE_SCANS, subject)
    assert stream_for_subject(subject) is None
    for bad in (f"gateway.mower.{MOWER}.camera.session", "gateway.mower.x.bridge.scan.extra"):
        with pytest.raises(ValueError, match="bridge scan"):
            parse_bridge_scan_subject(bad)


def test_firmware_info_defaults_and_ranges() -> None:
    assert FirmwareInfo().update_state == "unknown"
    info = FirmwareInfo(
        installed="1.11.3",
        available="1.12.0",
        release_notes="Better edges.",
        update_state="available",
        checked_at=AT,
    )
    assert FirmwareInfo.model_validate_json(info.model_dump_json()) == info
    with pytest.raises(ValidationError):
        FirmwareInfo.model_validate({"update_state": "exploding"})
    with pytest.raises(ValidationError):
        FirmwareInfo(update_state="installing", progress=101)


def test_firmware_check_request_reply_and_subjects() -> None:
    request = FirmwareCheckRequest(mower_id=MOWER, account_id=uuid4())
    assert request.mower_id == MOWER
    reply = FirmwareCheckReply(info=FirmwareInfo(installed="1.11.3", update_state="idle"))
    assert FirmwareCheckReply.model_validate_json(reply.model_dump_json()) == reply
    failed = FirmwareCheckReply(
        error=FirmwareCheckError(code=FirmwareCheckErrorCode.UNSUPPORTED, detail="no cloud")
    )
    assert failed.info is None
    with pytest.raises(ValidationError, match="exactly one"):
        FirmwareCheckReply()
    with pytest.raises(ValidationError, match="exactly one"):
        FirmwareCheckReply(info=FirmwareInfo(), error=failed.error)
    subject = firmware_check_subject(MOWER)
    assert subject == f"gateway.mower.{MOWER}.firmware.check"
    assert parse_firmware_check_subject(subject) == MOWER
    assert subject_matches(ALL_FIRMWARE_CHECKS, subject)
    assert stream_for_subject(subject) is None
    with pytest.raises(ValueError, match="firmware check"):
        parse_firmware_check_subject(bridge_scan_subject(MOWER))


def test_firmware_update_command_in_request_and_envelope() -> None:
    request = CommandRequest.model_validate(
        {"command": {"kind": "firmware_update", "version": "1.12.0"}}
    )
    assert isinstance(request.command, FirmwareUpdate)
    assert request.command.version == "1.12.0"
    with pytest.raises(ValidationError):
        CommandRequest.model_validate({"command": {"kind": "firmware_update"}})
    envelope = CommandEnvelope.create(
        mower_id=MOWER,
        account_id=uuid4(),
        issued_by=PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER),
        command=FirmwareUpdate(version="1.12.0"),
    )
    decoded = CommandEnvelope.model_validate_json(envelope.model_dump_json())
    assert decoded.command == FirmwareUpdate(version="1.12.0")
    assert decoded.priority == envelope.priority
