"""Links and firmware (ADR 0038) against the simulator only: the bridge switch, the firmware
safety rules of the controller, the update flow and the fake bridge scan."""

from datetime import timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    LINK_BLE_BRIDGE,
    BridgeScanErrorCode,
    CommandEnvelope,
    CommandStatus,
    ConnectionKind,
    EmergencyStop,
    FirmwareUpdate,
    ManualDrive,
    MowerActivity,
    MowerCapabilities,
    MowerCommand,
    PrincipalKind,
    PrincipalRef,
    SetBladeHeight,
    StartMow,
)
from mowdeck_control import (
    FIRMWARE_UPDATE_RUNNING,
    BridgeScanCapable,
    CommandRejected,
    ConnectionConfig,
    FirmwarePort,
    MowerController,
    SimulatedMower,
    SimulationConfig,
    bridge_scanner_of,
    create_simulated_connection,
    firmware_port_of,
    firmware_update_problem,
)
from mowdeck_control.testing import ManualClock, ManualTimers

ISSUER = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)
MOWER = uuid4()
ACCOUNT = uuid4()
NEW_VERSION = "sim-1.1.0"
KEY = "not-a-real-key"  # the simulated bridge ignores it


def _envelope(command: MowerCommand) -> CommandEnvelope:
    return CommandEnvelope.create(
        mower_id=MOWER,
        account_id=ACCOUNT,
        issued_by=ISSUER,
        command=command,
        ttl=timedelta(seconds=30),
    )


def _mower(**config: object) -> SimulatedMower:
    return SimulatedMower(
        mower_id=MOWER,
        account_id=ACCOUNT,
        config=SimulationConfig(**config),  # type: ignore[arg-type]
    )


def _charging_mower(**config: object) -> SimulatedMower:
    """A docked mower that charges (battery below 100 %) with an update on offer."""
    mower = _mower(battery_percent=80, firmware_available=NEW_VERSION, **config)
    mower.tick(0.1)  # the dock starts charging on the first tick
    return mower


# --- bridge link ----------------------------------------------------------------------------


async def test_control_bridge_switch_flips_manual_drive_and_connection() -> None:
    mower = _mower(bridge=True)
    await mower.connect()
    caps = await mower.capabilities()
    assert caps.manual_drive is True
    assert caps.links_supported == [LINK_BLE_BRIDGE]
    state = await mower.get_state()
    assert state.connection is ConnectionKind.BLE_BRIDGE
    link = state.links[LINK_BLE_BRIDGE]
    assert link.connected is True and link.rssi == -60 and link.project_version == "1"

    session = await mower.start_drive_session()
    mower.set_bridge(False)
    assert not session.is_open  # the frames would have nowhere to go
    caps = await mower.capabilities()
    assert caps.manual_drive is False and caps.manual_drive_blades is False
    state = await mower.get_state()
    assert state.connection is ConnectionKind.SIMULATED
    assert state.links[LINK_BLE_BRIDGE].connected is False
    assert state.links[LINK_BLE_BRIDGE].rssi is None
    with pytest.raises(CommandRejected, match="bridge is not connected"):
        await mower.start_drive_session()

    mower.set_bridge(True)
    assert (await mower.capabilities()).manual_drive is True
    assert (await mower.get_state()).connection is ConnectionKind.BLE_BRIDGE


async def test_control_bridge_switch_is_published_and_absent_without_a_bridge() -> None:
    plain = _mower()
    await plain.connect()
    state = await plain.get_state()
    assert state.links == {} and state.connection is ConnectionKind.SIMULATED
    assert (await plain.capabilities()).manual_drive is True  # no bridge: driving as before
    with pytest.raises(ValueError, match="no bridge"):
        plain.set_bridge(False)

    mower = _mower(bridge=True)
    await mower.connect()
    subscription = mower.watch_state()
    first = await anext(subscription)
    assert first.links[LINK_BLE_BRIDGE].connected is True
    mower.set_bridge(False)
    changed = await anext(subscription)
    assert changed.links[LINK_BLE_BRIDGE].connected is False
    await subscription.aclose()


def test_control_links_option_switches_the_simulated_bridge_on() -> None:
    config = ConnectionConfig(
        mower_id=MOWER,
        account_id=ACCOUNT,
        options={"links": {LINK_BLE_BRIDGE: {"host": "192.0.2.10", "credentials": "fleet"}}},
    )
    mower = create_simulated_connection(config)
    assert mower.config.bridge is True and mower.bridge_connected is True
    plain = create_simulated_connection(ConnectionConfig(mower_id=MOWER, account_id=ACCOUNT))
    assert plain.config.bridge is False


# --- bridge scan ----------------------------------------------------------------------------


async def test_control_bridge_scan_answers_the_configured_devices() -> None:
    mower = _mower()
    await mower.connect()
    scanner = bridge_scanner_of(mower)
    assert isinstance(scanner, BridgeScanCapable)
    reply = await scanner.scan_bridge(
        host="192.0.2.10", port=6053, secret=KEY, expected_name="Luba-SIM0001", timeout_s=5
    )
    assert reply.ok and reply.proxy is not None
    assert reply.proxy.proxy_name == "sim-bridge" and reply.proxy.project_version == "1"
    assert [(d.name, d.address, d.rssi) for d in reply.devices] == [
        ("Luba-SIM0001", "FC:23:45:67:89:AB", -62)
    ]
    other = await scanner.scan_bridge(
        host="192.0.2.10", port=6053, secret=KEY, expected_name="Luba-OTHER", timeout_s=5
    )
    assert other.error is not None and other.error.code is BridgeScanErrorCode.TIMEOUT
    assert other.proxy is not None and len(other.devices) == 1  # the bridge itself answered


@pytest.mark.parametrize(
    ("error", "proxy_known"),
    [("unreachable", False), ("auth_failed", False), ("busy", True), ("timeout", True)],
)
async def test_control_bridge_scan_configured_errors(error: str, proxy_known: bool) -> None:
    mower = _mower(bridge_scan_error=error)
    reply = await mower.scan_bridge(
        host="192.0.2.10", port=6053, secret=KEY, expected_name=None, timeout_s=5
    )
    assert reply.error is not None and reply.error.code.value == error
    assert (reply.proxy is not None) is proxy_known
    assert reply.devices == []


def test_control_bridge_scan_error_must_be_a_known_code() -> None:
    with pytest.raises(ValueError):
        SimulationConfig(bridge_scan_error="nope")


# --- firmware rules (pure) ------------------------------------------------------------------


async def test_control_firmware_update_problem_rules() -> None:
    mower = _charging_mower()
    caps = MowerCapabilities(firmware_update=True)
    state = mower.snapshot()
    assert state.charging and state.firmware_info is None  # not checked before connect
    assert firmware_update_problem(NEW_VERSION, state, caps) == (
        "no firmware check yet; check for updates first"
    )
    await mower.connect()
    ok = await mower.get_state()
    info = ok.firmware_info
    assert info is not None and info.update_state == "available"
    assert firmware_update_problem(NEW_VERSION, ok, caps) is None
    assert firmware_update_problem(NEW_VERSION, ok, MowerCapabilities()) == (
        "firmware updates are not supported on this connection"
    )
    assert "not the offered one" in (firmware_update_problem("sim-9", ok, caps) or "")
    assert "drive session" in (
        firmware_update_problem(NEW_VERSION, ok, caps, drive_session_open=True) or ""
    )
    low = ok.model_copy(update={"battery_percent": 49})
    assert "below the 50 %" in (firmware_update_problem(NEW_VERSION, low, caps) or "")
    idle = ok.model_copy(update={"activity": MowerActivity.IDLE, "charging": False})
    assert "docked and charging" in (firmware_update_problem(NEW_VERSION, idle, caps) or "")
    docked = ok.model_copy(update={"activity": MowerActivity.DOCKED, "charging": False})
    assert "must be charging" in (firmware_update_problem(NEW_VERSION, docked, caps) or "")
    installing = ok.model_copy(
        update={"firmware_info": info.model_copy(update={"update_state": "installing"})}
    )
    assert "already installing" in (firmware_update_problem(NEW_VERSION, installing, caps) or "")
    none = ok.model_copy(update={"firmware_info": info.model_copy(update={"update_state": "idle"})})
    assert firmware_update_problem(NEW_VERSION, none, caps) == "no firmware update is available"


# --- firmware through the controller ----------------------------------------------------------


async def _controller(mower: SimulatedMower) -> MowerController:
    clock = ManualClock()
    controller = MowerController(
        mower, mower_id=MOWER, clock=clock, timer_factory=ManualTimers(clock)
    )
    await controller.start()
    return controller


async def test_control_firmware_update_rejections_through_the_controller() -> None:
    mower = _charging_mower()
    controller = await _controller(mower)
    try:
        stale = await controller.handle(_envelope(FirmwareUpdate(version="sim-9")))
        assert stale.status is CommandStatus.REJECTED
        assert stale.reason is not None and "not the offered one" in stale.reason

        mower.set_battery(30)
        low = await controller.handle(_envelope(FirmwareUpdate(version=NEW_VERSION)))
        assert low.status is CommandStatus.REJECTED
        assert low.reason is not None and "below the 50 %" in low.reason

        mower.set_battery(100)
        mower.tick(0.1)  # full: docked, not charging
        assert (await mower.get_state()).activity is MowerActivity.DOCKED
        full = await controller.handle(_envelope(FirmwareUpdate(version=NEW_VERSION)))
        assert full.status is CommandStatus.REJECTED
        assert full.reason is not None and "must be charging" in full.reason

        started = await controller.handle(_envelope(StartMow(area_ids=["front"])))
        assert started.status is CommandStatus.EXECUTED
        job = await controller.handle(_envelope(FirmwareUpdate(version=NEW_VERSION)))
        assert job.status is CommandStatus.REJECTED
        assert job.reason is not None and "it is mowing" in job.reason
        assert (await mower.get_state()).activity is MowerActivity.MOWING  # the job goes on
    finally:
        await controller.close()


async def test_control_firmware_update_refused_while_driving() -> None:
    mower = _charging_mower()
    controller = await _controller(mower)
    try:
        frame = await controller.handle(_envelope(ManualDrive(linear=0.0, angular=0.0)))
        assert frame.status is CommandStatus.ACCEPTED
        assert controller.drive_session_active
        result = await controller.handle(_envelope(FirmwareUpdate(version=NEW_VERSION)))
        assert result.status is CommandStatus.REJECTED
        assert result.reason is not None and "drive session" in result.reason
        assert controller.drive_session_active  # the refusal did not end the session
    finally:
        await controller.close()


async def test_control_firmware_happy_path_available_installing_idle() -> None:
    mower = _charging_mower(firmware_update_seconds=10)
    controller = await _controller(mower)
    try:
        port = firmware_port_of(mower)
        assert isinstance(port, FirmwarePort)
        info = await port.check_firmware()
        assert info is not None
        assert info.update_state == "available" and info.available == NEW_VERSION
        assert info.installed == "sim-1.0.0" and info.checked_at is not None

        result = await controller.handle(_envelope(FirmwareUpdate(version=NEW_VERSION)))
        assert result.status is CommandStatus.EXECUTED, result.reason
        state = await mower.get_state()
        assert state.activity is MowerActivity.UPDATING
        assert state.firmware_info is not None
        assert state.firmware_info.update_state == "installing"
        assert state.firmware_info.progress == 0

        # Rule 11: everything but the emergency stop is refused while updating.
        blade = await controller.handle(_envelope(SetBladeHeight(mm=50)))
        assert blade.status is CommandStatus.REJECTED and blade.reason == FIRMWARE_UPDATE_RUNNING
        frame = await controller.handle(_envelope(ManualDrive(linear=0.5, angular=0.0)))
        assert frame.status is CommandStatus.REJECTED and frame.reason == FIRMWARE_UPDATE_RUNNING
        again = await controller.handle(_envelope(FirmwareUpdate(version=NEW_VERSION)))
        assert again.status is CommandStatus.REJECTED and again.reason == FIRMWARE_UPDATE_RUNNING
        stop = await controller.handle(_envelope(EmergencyStop()))
        assert stop.status is CommandStatus.EXECUTED

        mower.tick(4)
        state = await mower.get_state()
        assert state.activity is MowerActivity.UPDATING
        assert state.firmware_info is not None and state.firmware_info.progress == 40
        mower.tick(6)
        state = await mower.get_state()
        assert state.activity in (MowerActivity.DOCKED, MowerActivity.CHARGING)
        assert state.firmware == NEW_VERSION
        assert state.firmware_info is not None
        assert state.firmware_info.update_state == "idle"
        assert state.firmware_info.installed == NEW_VERSION
        assert state.firmware_info.available is None
        assert state.firmware_info.progress is None
        # Up to date now: a second update is refused, the other commands work again.
        repeat = await controller.handle(_envelope(FirmwareUpdate(version=NEW_VERSION)))
        assert repeat.status is CommandStatus.REJECTED
        assert repeat.reason == "no firmware update is available"
        blade = await controller.handle(_envelope(SetBladeHeight(mm=50)))
        assert blade.status is CommandStatus.EXECUTED
    finally:
        await controller.close()


async def test_control_firmware_port_direct_call_applies_the_same_rules() -> None:
    mower = _charging_mower()
    await mower.connect()
    with pytest.raises(CommandRejected, match="not the offered one"):
        await mower.start_firmware_update("sim-9")
    await mower.start_firmware_update(NEW_VERSION)
    assert (await mower.get_state()).activity is MowerActivity.UPDATING
    with pytest.raises(CommandRejected, match="already installing"):
        await mower.start_firmware_update(NEW_VERSION)
    with pytest.raises(CommandRejected, match=FIRMWARE_UPDATE_RUNNING):
        await mower.start_drive_session()


async def test_control_firmware_without_an_offer_is_idle() -> None:
    mower = _mower()
    await mower.connect()
    info = await mower.check_firmware()
    assert info is not None and info.update_state == "idle" and info.available is None
    result = await mower.execute(FirmwareUpdate(version="sim-2"))
    assert result.status is CommandStatus.REJECTED
    assert result.reason == "no firmware update is available"
