"""RTK base station (ADR 0047) in the Mammotion adapter: the mapping from the mower's reports,
the ``basestation_info`` details, the base station's firmware through the vendor's firmware
service, the discovery's device type and the fake cloud. Fakes only, no network."""

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from mammotion_support import BLE_OPTIONS, CLOUD_OPTIONS, NOW, clock, config
from mowdeck_contracts.v1 import (
    BASE_STATION_OPTION,
    BaseStationRef,
    DeviceType,
    GnssSolution,
    RtkCorrectionLink,
)
from mowdeck_control import BaseStationPort, CommandRejected, base_station_port_of
from mowdeck_control_mammotion import mapping
from mowdeck_control_mammotion.ble import create_ble_bridge_connection
from mowdeck_control_mammotion.cloud import MammotionCloudConnection
from mowdeck_control_mammotion.discovery import device_of
from mowdeck_control_mammotion.fake import (
    SAMPLE_BASE_FIRMWARE,
    SAMPLE_BASE_IOT_ID,
    SAMPLE_BASE_LAT_DEG,
    SAMPLE_BASE_NEWER_FIRMWARE,
    FakeMammotionPort,
    firmware_offer,
)
from mowdeck_control_mammotion.fake_cloud import (
    FAKE_CLOUD_PASSWORD,
    FAKE_DEVICES,
    fake_cloud_connection_factory,
    fake_pool,
)
from mowdeck_control_mammotion.options import CloudOptions
from mowdeck_control_mammotion.port import RTKBaseStationDevice

REF = BaseStationRef(
    base_station_id=uuid4(), name="Garden base", device_name="RTK-1", iot_id=SAMPLE_BASE_IOT_ID
)


async def _wait(predicate: Callable[[], bool], timeout_s: float = 2.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not predicate():  # noqa: ASYNC110 - polls state the adapter changes
            await asyncio.sleep(0.01)


def _cloud(port: FakeMammotionPort, ref: BaseStationRef | None = REF) -> MammotionCloudConnection:
    options: dict[str, Any] = {BASE_STATION_OPTION: ref.model_dump(mode="json")} if ref else {}
    conn = MammotionCloudConnection(
        port, config(options), CloudOptions.parse(CLOUD_OPTIONS), clock=clock
    )
    conn.base_details_delay_s = 0.0
    return conn


# --- mapping ----------------------------------------------------------------------------------


def test_mammotion_device_types() -> None:
    assert mapping.is_rtk_base("RTK12345678")
    assert not mapping.is_rtk_base("Luba-VP12345")
    assert mapping.device_type_of("Luba-VP1", mower=True) is DeviceType.MOWER
    assert mapping.device_type_of("RTK12345678", mower=False) is DeviceType.RTK_BASE
    assert mapping.device_type_of("Spino-S1abc", mower=False) is DeviceType.OTHER


def test_mammotion_base_station_from_the_mower_reports() -> None:
    port = FakeMammotionPort()
    port.use_base_station(moved=True)
    device = port._device
    state = mapping.base_station_of(device, ref=REF)
    assert state.base_station_id == REF.base_station_id
    assert state.correction_link is RtkCorrectionLink.LORA and state.link_up is True
    assert state.lora_channel == 12 and state.moved is True
    assert state.position is not None and state.position.lat == SAMPLE_BASE_LAT_DEG
    assert state.firmware == SAMPLE_BASE_FIRMWARE
    assert state.satellites is None and state.rtk_status is GnssSolution.UNKNOWN

    port.use_base_station(switch="RTK_USED_NRTK", lat_deg=0.0, lon_deg=0.0, version=(0, 0, 0, 0))
    network = mapping.base_station_of(device, ref=REF)
    assert network.correction_link is RtkCorrectionLink.NETWORK and network.link_up is None
    assert network.position is None and network.firmware is None
    no_gga = port
    no_gga.use_base_station(lat_deg=-1.0, lon_deg=-1.0, lora_connected=False)
    down = mapping.base_station_of(device, ref=REF)
    assert down.link_up is False and down.position is None

    before = mapping.base_station_of(device, ref=REF, has_report=False)
    assert before.correction_link is RtkCorrectionLink.UNKNOWN and before.link_up is None


def test_mammotion_base_details_from_the_answer() -> None:
    port = FakeMammotionPort()
    device = port._device
    assert mapping.base_details_of(None, device, now=NOW) is None  # nothing answered yet
    info = device.report_data.basestation_info
    info.sats_num, info.rtk_status, info.wifi_rssi, info.lora_channel = 33, 4, -60, 9
    details = mapping.base_details_of(None, device, now=NOW)
    assert details is not None
    assert details.satellites == 33 and details.rtk_status is GnssSolution.FIXED
    assert details.wifi_dbm == -60 and details.lora_channel == 9 and details.at == NOW
    reply = SimpleNamespace(
        base=SimpleNamespace(
            to_app=SimpleNamespace(sats_num=20, rtk_status=5, wifi_rssi=0, lora_channel=0)
        )
    )
    from_reply = mapping.base_details_of(reply, device, now=NOW)
    assert from_reply is not None and from_reply.satellites == 20
    assert from_reply.rtk_status is GnssSolution.FLOAT and from_reply.wifi_dbm is None


# --- the connection ---------------------------------------------------------------------------


async def test_mammotion_cloud_reports_the_linked_base_station() -> None:
    port = FakeMammotionPort()
    port.use_base_station()
    conn = _cloud(port)
    await conn.connect()
    try:
        await _wait(lambda: any(c.name == "basestation_info" for c in port.sent))
        sent = next(c for c in port.sent if c.name == "basestation_info")
        assert sent.expected_field == "to_app"
        state = await conn.get_state()
        base = state.base_station
        assert base is not None and base.satellites == 34 and base.wifi_dbm == -57
        assert base.rtk_status is GnssSolution.FIXED and base.details_at == NOW
        assert base.correction_link is RtkCorrectionLink.LORA and base.link_up is True
        assert (await conn.capabilities()).base_station_firmware_update is True
        # The next report replaces PyMammotion's copy; the adapter keeps the answer.
        port._device.report_data.basestation_info.sats_num = 0
        await port.emit()
        kept = (await conn.get_state()).base_station
        assert kept is not None and kept.satellites == 34
    finally:
        await conn.disconnect()


async def test_mammotion_cloud_without_a_base_station() -> None:
    port = FakeMammotionPort()
    conn = _cloud(port, None)
    await conn.connect()
    try:
        assert (await conn.get_state()).base_station is None
        assert (await conn.capabilities()).base_station_firmware_update is False
        assert await conn.check_base_station_firmware() is None
        with pytest.raises(CommandRejected, match="base station"):
            await conn.start_base_station_firmware_update("1.0")
        await asyncio.sleep(0.05)
        assert "basestation_info" not in port.names()
    finally:
        await conn.disconnect()


async def test_mammotion_base_station_firmware_check_and_update() -> None:
    port = FakeMammotionPort()
    port.use_base_station()
    port.device_firmware[SAMPLE_BASE_IOT_ID] = firmware_offer(
        current=SAMPLE_BASE_FIRMWARE,
        available=SAMPLE_BASE_NEWER_FIRMWARE,
        iot_id=SAMPLE_BASE_IOT_ID,
    )
    conn = _cloud(port)
    conn.base_details_interval_s = 3600
    conn.base_install_recheck_s = 0.05
    assert isinstance(base_station_port_of(conn), BaseStationPort)
    await conn.connect()
    try:
        info = await conn.check_base_station_firmware()
        assert info is not None and info.update_state == "available"
        assert (
            info.installed == SAMPLE_BASE_FIRMWARE and info.available == SAMPLE_BASE_NEWER_FIRMWARE
        )
        assert port.device_firmware_checks == [SAMPLE_BASE_IOT_ID]
        assert port.firmware_checks == 0  # the mower's own check is not touched
        with pytest.raises(CommandRejected, match="not the offered one"):
            await conn.start_base_station_firmware_update("9.9")

        await conn.start_base_station_firmware_update(SAMPLE_BASE_NEWER_FIRMWARE)
        assert port.device_ota_started == [(SAMPLE_BASE_IOT_ID, SAMPLE_BASE_NEWER_FIRMWARE)]
        base = (await conn.get_state()).base_station
        assert base is not None and base.firmware_info is not None
        assert base.firmware_info.update_state == "installing"
        with pytest.raises(CommandRejected, match="already installing"):
            await conn.start_base_station_firmware_update(SAMPLE_BASE_NEWER_FIRMWARE)

        port.finish_device_update(SAMPLE_BASE_IOT_ID, SAMPLE_BASE_NEWER_FIRMWARE)

        def done() -> bool:
            state = conn._last_state
            return bool(
                state
                and state.base_station
                and state.base_station.firmware_info
                and state.base_station.firmware_info.update_state == "idle"
            )

        await _wait(done)  # the install re-check asked the vendor again
        final = (await conn.get_state()).base_station
        assert final is not None and final.firmware_info is not None
        assert final.firmware_info.installed == SAMPLE_BASE_NEWER_FIRMWARE
    finally:
        await conn.disconnect()


async def test_mammotion_base_station_update_ends_with_the_reported_version() -> None:
    port = FakeMammotionPort()
    port.use_base_station()
    port.device_firmware[SAMPLE_BASE_IOT_ID] = firmware_offer(
        current=SAMPLE_BASE_FIRMWARE, available="1.2.5.0", iot_id=SAMPLE_BASE_IOT_ID
    )
    conn = _cloud(port)
    conn.base_details_delay_s = 3600
    await conn.connect()
    try:
        await conn.check_base_station_firmware()
        await conn.start_base_station_firmware_update("1.2.5.0")
        port.use_base_station(version=(1, 2, 5, 0))
        await port.emit()
        base = (await conn.get_state()).base_station
        assert base is not None and base.firmware_info is not None
        assert base.firmware_info.update_state == "idle" and base.firmware == "1.2.5.0"
    finally:
        await conn.disconnect()


async def test_mammotion_base_station_update_refused_while_mowing() -> None:
    port = FakeMammotionPort()
    port.use_base_station()
    port.device_firmware[SAMPLE_BASE_IOT_ID] = firmware_offer(
        current=SAMPLE_BASE_FIRMWARE,
        available=SAMPLE_BASE_NEWER_FIRMWARE,
        iot_id=SAMPLE_BASE_IOT_ID,
    )
    conn = _cloud(port)
    conn.base_details_delay_s = 3600
    await conn.connect()
    try:
        await conn.check_base_station_firmware()
        await port.send("start_job", priority=port_priority())
        with pytest.raises(CommandRejected, match="mowing"):
            await conn.start_base_station_firmware_update(SAMPLE_BASE_NEWER_FIRMWARE)
        assert port.device_ota_started == []
    finally:
        await conn.disconnect()


def port_priority() -> Any:
    from mowdeck_control_mammotion.port import Priority

    return Priority.USER


def test_mammotion_ble_connection_ignores_the_base_station_option() -> None:
    conn = create_ble_bridge_connection(
        config({**BLE_OPTIONS, BASE_STATION_OPTION: REF.model_dump(mode="json")})
    )
    assert conn.options.host == BLE_OPTIONS["host"]


# --- discovery and the fake cloud -------------------------------------------------------------


def test_mammotion_discovery_marks_base_stations() -> None:
    handle = SimpleNamespace(
        device_name="RTK12345678",
        product_key="",
        iot_id="iot-rtk",
        snapshot=SimpleNamespace(raw=RTKBaseStationDevice(name="RTK12345678")),
    )
    found = device_of(handle)
    assert found.is_mower is False and found.device_type is DeviceType.RTK_BASE
    pool_cleaner = SimpleNamespace(
        device_name="Spino-S1abc", product_key="", iot_id="iot-pool", snapshot=None
    )
    assert device_of(pool_cleaner).device_type is DeviceType.OTHER
    assert FAKE_DEVICES[2].device_type is DeviceType.RTK_BASE


async def test_mammotion_fake_cloud_mower_with_its_base_station() -> None:
    pool = fake_pool()
    base = REF.model_copy(update={"device_name": FAKE_DEVICES[2].device_name})
    options = {
        "account": "garden@fake.mowdeck.test",
        "password": FAKE_CLOUD_PASSWORD,
        "device_name": FAKE_DEVICES[0].device_name,
        "linked_account_id": "x",
        BASE_STATION_OPTION: base.model_dump(mode="json"),
    }
    conn = fake_cloud_connection_factory(pool)(config(options))
    await conn.connect()
    try:
        state = await conn.get_state()
        assert state.base_station is not None
        assert state.base_station.correction_link is RtkCorrectionLink.LORA
        info = await conn.check_base_station_firmware()
        assert info is not None and info.available == SAMPLE_BASE_NEWER_FIRMWARE
    finally:
        await conn.disconnect()
    await pool.close()
