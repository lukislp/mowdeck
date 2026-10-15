"""Dual link and firmware (ADR 0038) on a fake port: bridge up and down, the drive session on
a bridge loss, scan error mapping, the firmware port calls and the update tracker."""

import asyncio
import time
from collections.abc import Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from mammotion_support import (
    BLE_OPTIONS,
    CLOUD_OPTIONS,
    MOWER_ID,
    NOW,
    ble,
    clock,
    cloud,
    config,
)
from mowdeck_contracts.v1 import (
    LINK_BLE_BRIDGE,
    BridgeScanErrorCode,
    BridgeScanReply,
    CommandEnvelope,
    CommandStatus,
    ConnectionKind,
    FirmwareUpdate,
    MowerActivity,
    PrincipalKind,
    PrincipalRef,
    SetBladeHeight,
)
from mowdeck_control import (
    BridgeScanCapable,
    CommandRejected,
    FirmwarePort,
    MowerController,
    NotConnected,
    bridge_scanner_of,
    firmware_port_of,
)
from mowdeck_control.testing import ManualClock, ManualTimers
from mowdeck_control_mammotion import esphome, mapping
from mowdeck_control_mammotion.cloud import MammotionCloudConnection, cloud_connection_factory
from mowdeck_control_mammotion.drive import BleDriveSession
from mowdeck_control_mammotion.dual import (
    BRIDGE_NOT_CONNECTED,
    MammotionDualConnection,
    dual_link_options,
    link_options_of,
)
from mowdeck_control_mammotion.esphome import (
    OUTDATED_ESPHOME,
    OUTDATED_NO_PROJECT,
    OUTDATED_TEMPLATE,
    EsphomeUnavailable,
    ProxyLink,
    outdated_reason,
    proxy_info_of,
    scan_error_code,
)
from mowdeck_control_mammotion.fake import (
    SAMPLE_FIRMWARE,
    SAMPLE_NEWER_FIRMWARE,
    FakeMammotionPort,
    firmware_offer,
    sample_device,
)
from mowdeck_control_mammotion.fake_cloud import (
    FAKE_CLOUD_PASSWORD,
    FAKE_DEVICES,
    fake_cloud_connection_factory,
    fake_pool,
)
from mowdeck_control_mammotion.options import CloudOptions, OptionsError
from mowdeck_control_mammotion.port import OTA_RESULT_FAILED, OTA_RESULT_SUCCESS, FirmwareRefused

ISSUER = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)
LINK_OPTIONS: dict[str, Any] = {**BLE_OPTIONS, "credentials": "fleet", "credentials_rev": "2"}
"""The bridge link as the Device-Gateway passes it: Fleet's options plus the merged key."""
KEY = "scan-key"  # the fake scanners ignore it


class _DeviceInfo(SimpleNamespace):
    """ESPHome ``device_info`` of a bridge built from the MowDeck template."""

    def __init__(self, **changes: Any) -> None:
        values = {
            "name": "mowdeck-bridge",
            "esphome_version": "2026.1.0",
            "compilation_time": "Jan 1 2026, 00:00:00",
            "project_name": esphome.BRIDGE_PROJECT_NAME,
            "project_version": esphome.BRIDGE_TEMPLATE_VERSION,
            "bluetooth_mac_address": "AA:BB:CC:DD:EE:01",
        }
        values.update(changes)
        super().__init__(**values)


class _Scanner:
    """A bleak scanner on the proxy: the advertisements it currently holds."""

    def __init__(self, *devices: tuple[str, str, int]) -> None:
        self.discovered_devices_and_advertisement_data = {
            address: (
                SimpleNamespace(address=address, name=name),
                SimpleNamespace(local_name=name, rssi=rssi),
            )
            for name, address, rssi in devices
        }


class _Opener:
    """The proxy opener of the tests: fails while ``error`` is set, else opens a link."""

    def __init__(self) -> None:
        self.error: Exception | None = None
        self.links: list[ProxyLink] = []
        self.calls: list[dict[str, Any]] = []
        self.scanner = _Scanner(("Luba-VPDRYRUN", "AA:BB:CC:DD:EE:FF", -58))
        self.info = _DeviceInfo()

    async def __call__(self, **kwargs: Any) -> ProxyLink:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        alive = {"up": True}

        async def close() -> None:
            alive["up"] = False

        link = ProxyLink(
            ble_device=SimpleNamespace(address=kwargs["ble_address"]),
            advertised_name="Luba-VPDRYRUN",
            proxy_name=self.info.name,
            close=close,
            is_alive=lambda: alive["up"],
            device_info=self.info,
            scanner=self.scanner,
            host=kwargs["host"],
            port=kwargs["port"],
            address=kwargs["ble_address"],
        )
        self.links.append(link)
        return link


class _Sleeps:
    """The watcher's sleep: polls yield briefly, backoff delays (a second or more) are recorded
    and, with ``hold``, block until the test calls :meth:`release`, so a test observes the
    "bridge down" window deterministically instead of racing the reconnect."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.hold = False
        self._gate = asyncio.Event()

    def release(self) -> None:
        """Let a held backoff delay pass (the watcher tries again)."""
        self._gate.set()

    async def __call__(self, seconds: float) -> None:
        if seconds >= 1:
            self.delays.append(seconds)
            if self.hold:
                self._gate.clear()
                await self._gate.wait()
                return
        await asyncio.sleep(0.002)


async def _wait(predicate: Callable[[], object], *, within: float = 30.0) -> None:
    """Poll ``predicate`` (a condition that stays true once reached); CI runners are slow, so
    the budget is generous."""
    deadline = time.monotonic() + within
    while not predicate():
        if time.monotonic() > deadline:
            msg = "condition not met in time"
            raise AssertionError(msg)
        await asyncio.sleep(0.005)


def _dual(
    port: FakeMammotionPort | None = None, **link_changes: Any
) -> tuple[MammotionDualConnection, FakeMammotionPort, _Opener, _Sleeps]:
    fake = port or FakeMammotionPort()
    opener, sleeps = _Opener(), _Sleeps()
    conn = MammotionDualConnection(
        fake,
        config(),
        CloudOptions.parse(CLOUD_OPTIONS),
        link_options_of({**LINK_OPTIONS, **link_changes}),
        clock=clock,
        opener=opener,
        sleep=sleeps,
        poll_s=0.01,
    )
    return conn, fake, opener, sleeps


def _envelope(command: Any) -> CommandEnvelope:
    return CommandEnvelope.create(
        mower_id=MOWER_ID,
        account_id=config().account_id,
        issued_by=ISSUER,
        command=command,
        ttl=timedelta(seconds=30),
    )


# --- bridge link ----------------------------------------------------------------------------


async def test_mammotion_dual_bridge_up_offers_manual_drive_over_ble() -> None:
    conn, port, opener, _ = _dual()
    assert conn.kind is ConnectionKind.CLOUD
    await conn.connect()
    try:
        await _wait(conn.ble_connected)
        assert port.attached == 1 and port.bridge is opener.links[0]
        assert opener.calls[0]["ble_address"] == "AA:BB:CC:DD:EE:FF"
        assert "noise_psk" in opener.calls[0]
        caps = await conn.capabilities()
        assert caps.manual_drive is True and caps.manual_drive_blades is False
        assert caps.links_supported == [LINK_BLE_BRIDGE] and caps.firmware_update is True
        state = await conn.get_state()
        assert state.connection is ConnectionKind.BLE_BRIDGE
        assert state.raw["link"] == LINK_BLE_BRIDGE
        link = state.links[LINK_BLE_BRIDGE]
        assert link.connected is True and link.since == NOW and link.rssi == -61
        assert link.esphome_version == "2026.1.0" and link.project_version == "1"
        assert link.outdated is False and link.outdated_reason is None
        assert conn.is_connected
    finally:
        await conn.disconnect()
    assert port.detached >= 1 and not opener.links[0].is_alive()


async def test_mammotion_dual_bridge_loss_keeps_the_cloud_and_ends_the_drive_session() -> None:
    conn, port, opener, sleeps = _dual()
    await conn.connect()
    try:
        await _wait(conn.ble_connected)
        subscription = conn.watch_state()
        first = await anext(subscription)
        assert first.connection is ConnectionKind.BLE_BRIDGE
        session = await conn.start_drive_session()
        await session.update(0.3, 0.0, False)
        assert port.names()[-1] == "send_movement"

        sleeps.hold = True  # the watcher waits for the test before it reconnects
        port.ble = False  # the Bluetooth transport dropped
        await _wait(lambda: port.detached == 1 and sleeps.delays)
        assert not conn.ble_connected()
        assert conn.is_connected  # the cloud carries on
        assert not session.is_open
        with pytest.raises(CommandRejected):
            await session.update(0.3, 0.0, False)
        state = await conn.get_state()
        assert state.connection is ConnectionKind.CLOUD and state.raw["link"] == "cloud"
        assert state.links[LINK_BLE_BRIDGE].connected is False
        assert state.links[LINK_BLE_BRIDGE].rssi is None
        assert state.links[LINK_BLE_BRIDGE].esphome_version == "2026.1.0"  # what we last saw
        assert (await conn.capabilities()).manual_drive is False
        with pytest.raises(CommandRejected, match=BRIDGE_NOT_CONNECTED):
            await conn.start_drive_session()
        published = [first]
        while True:
            try:
                published.append(await asyncio.wait_for(anext(subscription), 1.0))
            except TimeoutError:
                break
        kinds = [s.connection for s in published]
        assert ConnectionKind.CLOUD in kinds  # the link change was published
        await subscription.aclose()

        # The watcher reconnects on its own after the backoff.
        assert sleeps.delays[:1] == [5.0]
        sleeps.release()
        await _wait(lambda: len(opener.links) >= 2 and conn.ble_connected())
        assert (await conn.get_state()).connection is ConnectionKind.BLE_BRIDGE
    finally:
        await conn.disconnect()


async def test_mammotion_dual_without_a_bridge_stays_cloud_and_backs_off() -> None:
    conn, _, opener, sleeps = _dual()
    opener.error = ConnectionError("no route")
    await conn.connect()
    try:
        await _wait(lambda: len(sleeps.delays) >= 4)
        assert sleeps.delays[:4] == [5.0, 10.0, 20.0, 40.0]
        assert conn.watcher.last_error == "ConnectionError"
        state = await conn.get_state()
        assert state.connection is ConnectionKind.CLOUD
        assert state.links[LINK_BLE_BRIDGE].connected is False
        assert state.links[LINK_BLE_BRIDGE].esphome_version is None  # never seen
        assert (await conn.capabilities()).manual_drive is False
        with pytest.raises(CommandRejected, match=BRIDGE_NOT_CONNECTED):
            await conn.start_drive_session()
        opener.error = None
        await _wait(conn.ble_connected)
        assert (await conn.get_state()).connection is ConnectionKind.BLE_BRIDGE
    finally:
        await conn.disconnect()


async def test_mammotion_dual_attach_failure_closes_the_link_and_retries() -> None:
    conn, port, opener, sleeps = _dual()
    port.attach_error = RuntimeError("GATT connect failed")
    await conn.connect()
    try:
        await _wait(lambda: port.attached >= 2)
        assert not opener.links[0].is_alive()
        assert conn.watcher.last_error == "RuntimeError"
        assert sleeps.delays[0] == 5.0
        port.attach_error = None
        await _wait(conn.ble_connected)
    finally:
        await conn.disconnect()


async def test_mammotion_dual_drive_frames_never_fall_back_to_the_cloud() -> None:
    port = FakeMammotionPort()
    await port.open()
    up = {"ble": True}
    session = BleDriveSession(
        port,
        blades_allowed=False,
        blade_height_mm=60,
        sensor_stop=lambda: None,
        link_up=lambda: up["ble"],
    )
    await session.update(0.5, 0.0, False)
    up["ble"] = False
    with pytest.raises(NotConnected, match="bridge"):
        await session.update(0.5, 0.0, False)
    assert not session.is_open
    assert port.names() == ["send_movement"]  # the second frame was never sent


def test_mammotion_dual_link_options_ignore_the_credential_markers() -> None:
    options = link_options_of(LINK_OPTIONS)
    assert options.host == "mowdeck-bridge.local" and options.ble_address == "AA:BB:CC:DD:EE:FF"
    assert dual_link_options({"account": "a"}) is None
    assert dual_link_options({"links": {"other": {}}}) is None
    with pytest.raises(OptionsError, match="link ble_bridge"):
        dual_link_options({"links": {LINK_BLE_BRIDGE: {"host": "x"}}})


def test_mammotion_cloud_factory_picks_the_dual_connection_for_a_bridge_link() -> None:
    pool = fake_pool()
    create = cloud_connection_factory(pool)
    plain = create(config(dict(CLOUD_OPTIONS)))
    assert type(plain) is MammotionCloudConnection
    dual = create(config({**CLOUD_OPTIONS, "links": {LINK_BLE_BRIDGE: LINK_OPTIONS}}))
    assert isinstance(dual, MammotionDualConnection)
    assert dual.link_options.host == "mowdeck-bridge.local"


async def test_mammotion_fake_cloud_dual_connects_its_bridge_and_answers_scans() -> None:
    pool = fake_pool()
    options = {
        "account": "garden@fake.mowdeck.test",
        "password": FAKE_CLOUD_PASSWORD,
        "device_name": FAKE_DEVICES[0].device_name,
        "linked_account_id": "x",
        "links": {LINK_BLE_BRIDGE: {**LINK_OPTIONS, "device_name": FAKE_DEVICES[0].device_name}},
    }
    conn = fake_cloud_connection_factory(pool)(config(options))
    assert isinstance(conn, MammotionDualConnection)
    await conn.connect()
    try:
        await _wait(conn.ble_connected)
        state = await conn.get_state()
        assert state.connection is ConnectionKind.BLE_BRIDGE
        assert state.links[LINK_BLE_BRIDGE].rssi == -62
        info = state.firmware_info
        assert info is None  # no check yet
        checked = await conn.check_firmware()
        assert checked is not None and checked.update_state == "available"
        assert checked.available == SAMPLE_NEWER_FIRMWARE
        scanner = bridge_scanner_of(conn)
        assert isinstance(scanner, BridgeScanCapable)
        reply = await scanner.scan_bridge(
            host="192.0.2.10",
            port=6053,
            secret=LINK_OPTIONS["noise_psk"],
            expected_name=FAKE_DEVICES[0].device_name,
            timeout_s=5,
        )
        assert reply.ok and reply.proxy is not None and reply.proxy.outdated is False
        assert [d.name for d in reply.devices] == [FAKE_DEVICES[0].device_name]
        down = await scanner.scan_bridge(
            host="unreachable.example", port=6053, secret=KEY, expected_name=None, timeout_s=5
        )
        assert down.error is not None and down.error.code is BridgeScanErrorCode.UNREACHABLE
    finally:
        await conn.disconnect()
    await pool.close()


# --- bridge scans ---------------------------------------------------------------------------


async def test_mammotion_dual_scan_of_its_own_bridge_answers_from_the_live_scanner() -> None:
    conn, _, opener, _ = _dual()
    calls: list[str] = []

    async def other_scanner(**kwargs: Any) -> BridgeScanReply:
        calls.append(kwargs["host"])
        return BridgeScanReply()

    conn._scanner = other_scanner
    await conn.connect()
    try:
        await _wait(conn.ble_connected)
        reply = await conn.scan_bridge(
            host="MOWDECK-BRIDGE.local",
            port=6053,
            secret=KEY,
            expected_name="Luba-VPDRYRUN",
            timeout_s=10,
        )
        assert reply.ok and reply.proxy is not None
        assert reply.proxy.proxy_name == "mowdeck-bridge"
        assert [(d.name, d.rssi) for d in reply.devices] == [("Luba-VPDRYRUN", -58)]
        assert calls == []
        missing = await conn.scan_bridge(
            host="mowdeck-bridge.local", port=6053, secret=KEY, expected_name="Luba-X", timeout_s=1
        )
        assert missing.error is not None and missing.error.code is BridgeScanErrorCode.TIMEOUT
        assert missing.proxy is not None
        await conn.scan_bridge(
            host="other-bridge.local", port=6053, secret=KEY, expected_name=None, timeout_s=1
        )
        assert calls == ["other-bridge.local"]
        assert opener.links[0].rssi() == -58
    finally:
        await conn.disconnect()


async def test_mammotion_cloud_scan_errors_are_replies_not_exceptions() -> None:
    conn, _ = cloud()

    async def boom(**kwargs: Any) -> BridgeScanReply:
        raise OSError("socket")

    async def missing(**kwargs: Any) -> BridgeScanReply:
        raise EsphomeUnavailable("install the extra")

    conn._scanner = boom
    reply = await conn.scan_bridge(host="h", port=6053, secret=KEY, expected_name=None, timeout_s=1)
    assert reply.error is not None and reply.error.code is BridgeScanErrorCode.UNREACHABLE
    assert "OSError" in reply.error.detail and "k" not in reply.error.detail.split()
    conn._scanner = missing
    reply = await conn.scan_bridge(host="h", port=6053, secret=KEY, expected_name=None, timeout_s=1)
    assert reply.error is not None and reply.error.code is BridgeScanErrorCode.UNSUPPORTED


def test_mammotion_scan_error_code_maps_the_esphome_errors() -> None:
    core = pytest.importorskip("aioesphomeapi.core")
    assert scan_error_code(core.InvalidEncryptionKeyAPIError()) is BridgeScanErrorCode.AUTH_FAILED
    assert scan_error_code(core.RequiresEncryptionAPIError()) is BridgeScanErrorCode.AUTH_FAILED
    assert scan_error_code(core.APIConnectionError()) is BridgeScanErrorCode.UNREACHABLE
    assert scan_error_code(core.ResolveAPIError()) is BridgeScanErrorCode.UNREACHABLE
    assert scan_error_code(OSError()) is BridgeScanErrorCode.UNREACHABLE


def test_mammotion_proxy_info_marks_outdated_bridges() -> None:
    current = proxy_info_of(_DeviceInfo(), checked_at=NOW)
    assert current.proxy_name == "mowdeck-bridge" and current.outdated is False
    assert current.esphome_version == "2026.1.0" and current.project_version == "1"
    assert current.bluetooth_mac == "AA:BB:CC:DD:EE:01" and current.checked_at == NOW
    stock = proxy_info_of(_DeviceInfo(project_name="", project_version=""))
    assert stock.outdated is True and stock.outdated_reason == OUTDATED_NO_PROJECT
    old = proxy_info_of(_DeviceInfo(esphome_version="2024.12.1"))
    assert old.outdated_reason == OUTDATED_ESPHOME
    template = proxy_info_of(_DeviceInfo(project_version="0"))
    assert template.outdated_reason == OUTDATED_TEMPLATE
    other = proxy_info_of(_DeviceInfo(project_name="someone.else", project_version="0"))
    assert other.outdated is False
    assert outdated_reason("garbage", esphome.BRIDGE_PROJECT_NAME, "1") is None
    status = mapping.link_status_of(stock, connected=True, since=NOW, rssi=-70)
    assert status.outdated is True and status.outdated_reason == OUTDATED_NO_PROJECT
    assert mapping.link_status_of(None, connected=False, since=None, rssi=-70).rssi is None


# --- firmware -------------------------------------------------------------------------------


async def test_mammotion_cloud_check_firmware_publishes_the_offer() -> None:
    conn, port = cloud()
    port.firmware_check = firmware_offer()
    await conn.connect()
    assert (await conn.get_state()).firmware_info is None
    firmware = firmware_port_of(conn)
    assert isinstance(firmware, FirmwarePort)
    subscription = conn.watch_state()
    await anext(subscription)
    info = await firmware.check_firmware()
    assert info is not None
    assert info.installed == SAMPLE_FIRMWARE and info.available == SAMPLE_NEWER_FIRMWARE
    assert info.update_state == "available" and info.checked_at == NOW
    assert info.release_notes is not None and "Wi-Fi" in info.release_notes
    published = await asyncio.wait_for(anext(subscription), 1)
    assert published.firmware_info == info
    await subscription.aclose()
    assert port.firmware_checks == 1
    port.firmware_check = firmware_offer(available=None)
    idle = await firmware.check_firmware()
    assert idle is not None and idle.update_state == "idle" and idle.available is None
    port.firmware_check = None
    unknown = await firmware.check_firmware()
    assert unknown is not None and unknown.update_state == "unknown"
    port.firmware_check_error = RuntimeError("503")
    with pytest.raises(NotConnected, match="did not answer"):
        await firmware.check_firmware()
    await conn.disconnect()


async def test_mammotion_ble_connection_offers_no_firmware_service() -> None:
    conn, _ = ble()
    await conn.connect()
    caps = await conn.capabilities()
    assert caps.firmware_update is False and caps.links_supported == []
    assert firmware_port_of(conn) is None and bridge_scanner_of(conn) is None
    assert (await conn.get_state()).firmware_info is None
    with pytest.raises(CommandRejected, match="cloud link"):
        await conn.execute(FirmwareUpdate(version="1.30.31.19"))


async def test_mammotion_firmware_update_happy_path_through_the_controller() -> None:
    conn, port = cloud()
    port.firmware_check = firmware_offer()
    port.wait_real = True
    clock = ManualClock()
    controller = MowerController(
        conn, mower_id=MOWER_ID, clock=clock, timer_factory=ManualTimers(clock)
    )
    await controller.start()
    try:
        state = await conn.get_state()
        assert state.activity is MowerActivity.DOCKED and state.charging
        assert state.battery_percent == 87
        early = await controller.handle(_envelope(FirmwareUpdate(version=SAMPLE_NEWER_FIRMWARE)))
        assert early.status is CommandStatus.REJECTED
        assert early.reason == "no firmware check yet; check for updates first"
        await conn.check_firmware()
        stale = await controller.handle(_envelope(FirmwareUpdate(version="1.30.30.1")))
        assert stale.status is CommandStatus.REJECTED
        assert stale.reason is not None and "not the offered one" in stale.reason
        assert port.ota_started == []

        task = asyncio.create_task(
            controller.handle(_envelope(FirmwareUpdate(version=SAMPLE_NEWER_FIRMWARE)))
        )
        await _wait(lambda: port.ota_started == [SAMPLE_NEWER_FIRMWARE])
        assert port.device().device_firmwares.device_version == ""  # cleared for the re-read
        state = await conn.get_state()
        assert state.firmware_info is not None
        assert state.firmware_info.update_state == "installing"
        assert state.firmware_info.progress == 0
        blade = await controller.handle(_envelope(SetBladeHeight(mm=50)))
        assert blade.status is CommandStatus.ACCEPTED  # the mower has not reported the update

        await port.deliver_ota_progress(30, version=SAMPLE_NEWER_FIRMWARE)
        result = await asyncio.wait_for(task, 2)
        assert result.status is CommandStatus.EXECUTED, result.reason
        state = await conn.get_state()
        assert state.firmware_info is not None and state.firmware_info.progress == 30
        port.set_work_mode_updating(True)
        await port.emit()
        assert (await conn.get_state()).activity is MowerActivity.UPDATING
        locked = await controller.handle(_envelope(SetBladeHeight(mm=50)))
        assert (
            locked.status is CommandStatus.REJECTED and locked.reason == "firmware update running"
        )

        port.set_work_mode_updating(False)
        await port.deliver_ota_progress(
            100, result=OTA_RESULT_SUCCESS, version=SAMPLE_NEWER_FIRMWARE
        )
        state = await conn.get_state()
        assert state.firmware == SAMPLE_NEWER_FIRMWARE
        assert state.firmware_info is not None
        assert state.firmware_info.update_state == "idle"
        assert state.firmware_info.installed == SAMPLE_NEWER_FIRMWARE
        assert state.firmware_info.available is None
        assert state.firmware_info.progress is None
        assert state.activity is MowerActivity.DOCKED
        again = await controller.handle(_envelope(SetBladeHeight(mm=50)))
        assert again.status is CommandStatus.ACCEPTED
    finally:
        await controller.close()


async def test_mammotion_firmware_update_failure_and_refusal() -> None:
    conn, port = cloud()
    port.firmware_check = firmware_offer()
    await conn.connect()
    await conn.check_firmware()
    port.ota_error = FirmwareRefused("the firmware service refused the update (code 500)")
    with pytest.raises(CommandRejected, match="code 500"):
        await conn.start_firmware_update(SAMPLE_NEWER_FIRMWARE)
    port.ota_error = None
    await conn.start_firmware_update(SAMPLE_NEWER_FIRMWARE)
    await port.deliver_ota_progress(40, result=OTA_RESULT_FAILED, message="dl fw: checksum")
    state = await conn.get_state()
    assert state.firmware_info is not None
    assert state.firmware_info.update_state == "failed"
    assert state.firmware_info.failure_reason == "dl fw: checksum"
    assert state.firmware_info.installed == SAMPLE_FIRMWARE
    with pytest.raises(CommandRejected, match="no firmware update is available"):
        await conn.start_firmware_update(SAMPLE_NEWER_FIRMWARE)
    await conn.disconnect()


async def test_mammotion_firmware_update_started_in_the_vendor_app_is_tracked() -> None:
    conn, port = cloud()
    await conn.connect()
    assert (await conn.get_state()).firmware_info is None
    await port.deliver_ota_progress(10, version=SAMPLE_NEWER_FIRMWARE)
    state = await conn.get_state()
    assert state.firmware_info is not None
    assert state.firmware_info.update_state == "installing"
    assert state.firmware_info.progress == 10
    await port.deliver_ota_progress(100, result=OTA_RESULT_SUCCESS, version=SAMPLE_NEWER_FIRMWARE)
    state = await conn.get_state()
    assert state.firmware_info is not None and state.firmware_info.update_state == "idle"
    assert state.firmware_info.installed == SAMPLE_NEWER_FIRMWARE
    await conn.disconnect()


def test_mammotion_firmware_tracker_gives_up_after_the_maximum_time() -> None:
    device = sample_device()
    tracker = mapping.FirmwareTracker()
    tracker.checked(firmware_offer(), installed=SAMPLE_FIRMWARE, now=NOW)
    tracker.started(SAMPLE_NEWER_FIRMWARE, installed=SAMPLE_FIRMWARE, loop_time=100.0)
    assert tracker.observe(device, installed=SAMPLE_FIRMWARE, loop_time=200.0, now=NOW) is None
    late = 100.0 + mapping.FIRMWARE_UPDATE_MAX_S + 1
    assert tracker.observe(device, installed=SAMPLE_FIRMWARE, loop_time=late, now=NOW) == "unknown"
    assert tracker.info is not None and tracker.info.update_state == "unknown"
    assert tracker.info.installed == SAMPLE_FIRMWARE


def test_mammotion_firmware_info_mapping() -> None:
    info = mapping.firmware_info_of(None, installed="", checked_at=None)
    assert info.update_state == "unknown" and info.installed is None
    offer = firmware_offer(current="1.0.0.0")
    info = mapping.firmware_info_of(offer, installed="", checked_at=NOW)
    assert info.installed is None  # the placeholder is not a version
    assert info.available == SAMPLE_NEWER_FIRMWARE and info.update_state == "available"
    running = mapping.firmware_info_of(
        firmware_offer(isupgrading=True, progress=55), installed=SAMPLE_FIRMWARE, checked_at=NOW
    )
    assert running.update_state == "installing" and running.progress == 55
    assert mapping.available_version_of(firmware_offer(available=None)) is None
