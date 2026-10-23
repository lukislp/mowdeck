"""RTK base station (ADR 0047) against the simulator: the state, the firmware port, the update
rules of the controller and the connection option."""

from datetime import timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    BASE_STATION_OPTION,
    BaseStationFirmwareUpdate,
    BaseStationRef,
    CommandEnvelope,
    CommandStatus,
    GnssSolution,
    ManualDrive,
    MowerCapabilities,
    MowerCommand,
    PrincipalKind,
    PrincipalRef,
    RtkCorrectionLink,
    RtkFixStatus,
    RtkSource,
    StartMow,
)
from mowdeck_control import (
    BaseStationPort,
    CommandRejected,
    ConnectionConfig,
    MowerController,
    SimulatedMower,
    SimulationConfig,
    base_station_firmware_update_problem,
    base_station_port_of,
    create_simulated_connection,
)
from mowdeck_control.testing import ManualClock, ManualTimers

ISSUER = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)
MOWER = uuid4()
ACCOUNT = uuid4()
BASE = BaseStationRef(base_station_id=uuid4(), name="Garden base", source="simulated")
NEW_BASE = "sim-base-1.1.0"


def _envelope(command: MowerCommand) -> CommandEnvelope:
    return CommandEnvelope.create(
        mower_id=MOWER,
        account_id=ACCOUNT,
        issued_by=ISSUER,
        command=command,
        ttl=timedelta(seconds=30),
    )


def _mower(*, base: BaseStationRef | None = BASE, **config: object) -> SimulatedMower:
    return SimulatedMower(
        mower_id=MOWER,
        account_id=ACCOUNT,
        config=SimulationConfig(**config),  # type: ignore[arg-type]
        base_station=base,
    )


async def _controller(mower: SimulatedMower) -> MowerController:
    clock = ManualClock()
    controller = MowerController(
        mower, mower_id=MOWER, clock=clock, timer_factory=ManualTimers(clock)
    )
    await controller.start()
    return controller


async def test_control_base_station_state_from_the_simulator() -> None:
    mower = _mower()
    await mower.connect()
    state = await mower.get_state()
    base = state.base_station
    assert base is not None
    assert base.base_station_id == BASE.base_station_id
    assert base.correction_link is RtkCorrectionLink.LORA and base.link_up is True
    assert base.satellites == 32 and base.rtk_status is GnssSolution.FIXED
    assert base.position is not None and base.wifi_dbm == -61 and base.moved is False
    assert base.firmware == "sim-base-1.0.0" and base.details_at is not None
    assert base.firmware_info is not None and base.firmware_info.update_state == "idle"
    assert (await mower.capabilities()).base_station_firmware_update is True

    mower.set_rtk(RtkFixStatus.FIXED, RtkSource.NETWORK)
    network = (await mower.get_state()).base_station
    assert network is not None
    assert network.correction_link is RtkCorrectionLink.NETWORK and network.link_up is None


async def test_control_base_station_absent_without_a_link() -> None:
    mower = _mower(base=None)
    await mower.connect()
    assert (await mower.get_state()).base_station is None
    assert (await mower.capabilities()).base_station_firmware_update is False
    assert await mower.check_base_station_firmware() is None


def test_control_base_station_option_links_the_simulated_base() -> None:
    config = ConnectionConfig(
        mower_id=MOWER,
        account_id=ACCOUNT,
        options={BASE_STATION_OPTION: BASE.model_dump(mode="json"), "time_scale": 2},
    )
    mower = create_simulated_connection(config)
    assert mower.base_station == BASE
    assert isinstance(base_station_port_of(mower), BaseStationPort)
    with pytest.raises(ValueError, match="unknown simulator options"):
        create_simulated_connection(
            ConnectionConfig(mower_id=MOWER, account_id=ACCOUNT, options={"base": 1})
        )


async def test_control_base_station_update_rules() -> None:
    mower = _mower(base_station_firmware_available=NEW_BASE)
    await mower.connect()
    state = await mower.get_state()
    caps = await mower.capabilities()
    assert base_station_firmware_update_problem(NEW_BASE, state, caps) is None
    assert "not supported" in (
        base_station_firmware_update_problem(NEW_BASE, state, MowerCapabilities()) or ""
    )
    assert "not the offered one" in (base_station_firmware_update_problem("x", state, caps) or "")
    assert "drive session" in (
        base_station_firmware_update_problem(NEW_BASE, state, caps, drive_session_open=True) or ""
    )
    without = state.model_copy(update={"base_station": None})
    assert "no base station" in (
        base_station_firmware_update_problem(NEW_BASE, without, caps) or ""
    )
    unchecked = state.model_copy(
        update={"base_station": state.base_station.model_copy(update={"firmware_info": None})}  # type: ignore[union-attr]
    )
    assert "check for updates" in (
        base_station_firmware_update_problem(NEW_BASE, unchecked, caps) or ""
    )


async def test_control_base_station_update_through_the_controller() -> None:
    mower = _mower(base_station_firmware_available=NEW_BASE, firmware_update_seconds=10)
    controller = await _controller(mower)
    try:
        port = base_station_port_of(mower)
        assert port is not None
        info = await port.check_base_station_firmware()
        assert info is not None and info.update_state == "available" and info.available == NEW_BASE

        stale = await controller.handle(_envelope(BaseStationFirmwareUpdate(version="x")))
        assert stale.status is CommandStatus.REJECTED

        result = await controller.handle(_envelope(BaseStationFirmwareUpdate(version=NEW_BASE)))
        assert result.status is CommandStatus.ACCEPTED, result.reason
        base = (await mower.get_state()).base_station
        assert base is not None and base.firmware_info is not None
        assert base.firmware_info.update_state == "installing" and base.link_up is False
        again = await controller.handle(_envelope(BaseStationFirmwareUpdate(version=NEW_BASE)))
        assert again.status is CommandStatus.REJECTED
        assert again.reason is not None and "already installing" in again.reason

        mower.tick(4)
        base = (await mower.get_state()).base_station
        assert base is not None and base.firmware_info is not None
        assert base.firmware_info.progress == 40
        mower.tick(6)
        base = (await mower.get_state()).base_station
        assert base is not None and base.firmware_info is not None
        assert base.firmware == NEW_BASE and base.firmware_info.update_state == "idle"
        assert base.link_up is True
    finally:
        await controller.close()


async def test_control_base_station_update_refused_while_mowing_or_driving() -> None:
    mower = _mower(base_station_firmware_available=NEW_BASE)
    controller = await _controller(mower)
    try:
        frame = await controller.handle(_envelope(ManualDrive(linear=0.0, angular=0.0)))
        assert frame.status is CommandStatus.ACCEPTED
        driving = await controller.handle(_envelope(BaseStationFirmwareUpdate(version=NEW_BASE)))
        assert driving.status is CommandStatus.REJECTED
        assert driving.reason is not None and "drive session" in driving.reason
        assert controller.drive_session_active  # the refusal does not end the session
        await controller.end_drive_session()

        started = await controller.handle(_envelope(StartMow(area_ids=["front"])))
        assert started.status is CommandStatus.EXECUTED
        mowing = await controller.handle(_envelope(BaseStationFirmwareUpdate(version=NEW_BASE)))
        assert mowing.status is CommandStatus.REJECTED
        assert mowing.reason is not None and "mowing" in mowing.reason
    finally:
        await controller.close()


async def test_control_base_station_update_without_a_base() -> None:
    mower = _mower(base=None)
    controller = await _controller(mower)
    try:
        result = await controller.handle(_envelope(BaseStationFirmwareUpdate(version=NEW_BASE)))
        assert result.status is CommandStatus.REJECTED
        assert result.reason == "this connection cannot update a base station"
    finally:
        await controller.close()
    direct = _mower(base_station_firmware_available=NEW_BASE)
    await direct.connect()
    with pytest.raises(CommandRejected, match="not the offered one"):
        await direct.start_base_station_firmware_update("x")
