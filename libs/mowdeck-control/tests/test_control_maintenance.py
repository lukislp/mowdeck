"""Maintenance commands (ADR 0048): the safety rules, the controller's checks and the simulator."""

import asyncio
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    MAINTENANCE_ACTIONS,
    CommandEnvelope,
    CommandStatus,
    FirmwareUpdate,
    LeaveDock,
    MaintenanceAction,
    ManualDrive,
    MowerActivity,
    MowerCommand,
    MowerState,
    PrincipalKind,
    PrincipalRef,
    RelocateDock,
    RestartMower,
    ResyncRtk,
    SetBladeHeight,
    StartMow,
    SyncMap,
)
from mowdeck_control import (
    CommandRejected,
    MaintenanceCapable,
    MaintenanceCommand,
    MowerController,
    Outcome,
    SimulatedMower,
    SimulationConfig,
    maintenance_port_of,
    maintenance_problem,
)
from mowdeck_control.simulator import RESTARTING
from mowdeck_control.testing import ManualClock, ManualTimers

ISSUER = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)
MOWER = uuid4()
ACCOUNT = uuid4()


def _envelope(command: MowerCommand) -> CommandEnvelope:
    return CommandEnvelope.create(
        mower_id=MOWER,
        account_id=ACCOUNT,
        issued_by=ISSUER,
        command=command,
        ttl=timedelta(seconds=30),
    )


def _mower(**config: Any) -> SimulatedMower:
    return SimulatedMower(mower_id=MOWER, account_id=ACCOUNT, config=SimulationConfig(**config))


async def _controller(mower: SimulatedMower) -> MowerController:
    clock = ManualClock()
    controller = MowerController(
        mower, mower_id=MOWER, clock=clock, timer_factory=ManualTimers(clock)
    )
    await controller.start()
    return controller


def _state(activity: MowerActivity, **extra: object) -> MowerState:
    mower = _mower()
    state = mower.snapshot()
    return state.model_copy(update={"activity": activity, **extra})


# --- the rules --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("activity", "allowed"),
    [
        (MowerActivity.DOCKED, True),
        (MowerActivity.CHARGING, True),
        (MowerActivity.IDLE, True),
        (MowerActivity.MOWING, False),
        (MowerActivity.PAUSED, False),
        (MowerActivity.RETURNING, False),
        (MowerActivity.ERROR, False),
        (MowerActivity.OFFLINE, False),
    ],
)
def test_control_restart_only_at_rest(activity: MowerActivity, allowed: bool) -> None:
    problem = maintenance_problem("restart_mower", _state(activity))
    assert (problem is None) is allowed


@pytest.mark.parametrize("action", ["relocate_dock", "leave_dock"])
@pytest.mark.parametrize(
    ("activity", "allowed"),
    [
        (MowerActivity.DOCKED, True),
        (MowerActivity.CHARGING, True),
        (MowerActivity.IDLE, False),
        (MowerActivity.MOWING, False),
        (MowerActivity.PAUSED, False),
        (MowerActivity.ERROR, False),
    ],
)
def test_control_dock_actions_only_in_the_dock(
    action: MaintenanceAction, activity: MowerActivity, allowed: bool
) -> None:
    problem = maintenance_problem(action, _state(activity))
    assert (problem is None) is allowed


def test_control_error_names_the_code() -> None:
    state = _state(MowerActivity.ERROR, error_code="lifted")
    assert maintenance_problem("leave_dock", state) == (
        "the mower reports an error (lifted); clear it at the mower first"
    )


@pytest.mark.parametrize("activity", list(MowerActivity))
def test_control_resync_rtk_is_always_allowed(activity: MowerActivity) -> None:
    state = _state(activity)
    assert maintenance_problem("resync_rtk", state, drive_session_open=True) is None
    assert maintenance_problem("resync_rtk", state, confirmation_pending=True) is None


def test_control_sync_map_any_activity_but_not_while_driving() -> None:
    assert maintenance_problem("sync_map", _state(MowerActivity.MOWING)) is None
    assert (
        maintenance_problem("sync_map", _state(MowerActivity.IDLE), confirmation_pending=True)
        is None
    )
    assert maintenance_problem("sync_map", _state(MowerActivity.IDLE), drive_session_open=True) == (
        "a manual drive session is open; stop driving first"
    )


def test_control_drive_session_and_pending_confirmation_block() -> None:
    docked = _state(MowerActivity.DOCKED)
    in_session: tuple[MaintenanceAction, ...] = ("restart_mower", "relocate_dock")
    for action in in_session:
        assert maintenance_problem(action, docked, drive_session_open=True) is not None
    pending: tuple[MaintenanceAction, ...] = ("restart_mower", "relocate_dock", "leave_dock")
    for action in pending:
        reason = maintenance_problem(action, docked, confirmation_pending=True)
        assert reason == "a command is still waiting for its confirmation"
    assert maintenance_problem("leave_dock", docked, drive_session_open=True) is None


# --- the simulator ----------------------------------------------------------------------------


async def test_control_simulator_declares_maintenance() -> None:
    mower = _mower()
    assert isinstance(mower, MaintenanceCapable)
    assert maintenance_port_of(mower) is mower
    assert (await mower.capabilities()).maintenance == frozenset(MAINTENANCE_ACTIONS)
    without_undock = await _mower(undock=False).capabilities()
    assert without_undock.maintenance == frozenset(MAINTENANCE_ACTIONS) - {"leave_dock"}
    assert (await _mower(maintenance=False).capabilities()).maintenance == frozenset()


async def test_control_simulator_restart_goes_offline_and_comes_back() -> None:
    mower = _mower(restart_seconds=4.0)
    await mower.connect()
    outcome = await mower.run_maintenance(RestartMower())
    assert outcome.status is CommandStatus.ACCEPTED
    assert (await mower.get_state()).activity is MowerActivity.OFFLINE
    assert mower.restarting
    blocked = await mower.execute(StartMow(area_ids=["front"]))
    assert blocked.status is CommandStatus.REJECTED and blocked.reason == RESTARTING
    with pytest.raises(CommandRejected, match="restarting"):
        await mower.start_drive_session()
    mower.tick(3.0)
    assert mower.restarting
    mower.tick(1.5)
    assert not mower.restarting
    assert (await mower.get_state()).activity in (MowerActivity.DOCKED, MowerActivity.CHARGING)
    assert mower.maintenance_log == ["restart_mower"]


async def test_control_simulator_refuses_what_it_does_not_offer() -> None:
    mower = _mower(maintenance=False)
    await mower.connect()
    with pytest.raises(CommandRejected, match="does not support sync_map"):
        await mower.run_maintenance(SyncMap())


# --- the controller ---------------------------------------------------------------------------


async def test_control_restart_through_the_controller() -> None:
    mower = _mower()
    controller = await _controller(mower)
    try:
        result = await controller.handle(_envelope(RestartMower()))
        assert result.status is CommandStatus.ACCEPTED
        assert result.reason is not None and "restart sent" in result.reason
        again = await controller.handle(_envelope(RestartMower()))
        assert again.status is CommandStatus.REJECTED
        assert again.reason is not None and "the mower is offline" in again.reason
    finally:
        await controller.close()


async def test_control_restart_refused_while_mowing() -> None:
    mower = _mower()
    controller = await _controller(mower)
    try:
        started = await controller.handle(_envelope(StartMow(area_ids=["front"])))
        assert started.status is CommandStatus.EXECUTED
        result = await controller.handle(_envelope(RestartMower()))
        assert result.status is CommandStatus.REJECTED
        assert result.reason is not None and "the mower is mowing" in result.reason
        assert mower.maintenance_log == []
    finally:
        await controller.close()


async def test_control_unsupported_maintenance_is_rejected() -> None:
    mower = _mower(maintenance=False)
    controller = await _controller(mower)
    try:
        for command in (RestartMower(), SyncMap(), RelocateDock(), ResyncRtk(), LeaveDock()):
            result = await controller.handle(_envelope(command))
            assert result.status is CommandStatus.REJECTED
            assert result.reason == f"this connection does not support {command.kind}"
    finally:
        await controller.close()


async def test_control_relocate_dock_only_in_the_dock() -> None:
    mower = _mower()
    controller = await _controller(mower)
    try:
        result = await controller.handle(_envelope(RelocateDock()))
        assert result.status is CommandStatus.ACCEPTED
        frame = await controller.handle(_envelope(ManualDrive(linear=0.0, angular=0.0)))
        assert frame.status is CommandStatus.ACCEPTED  # the session leaves the dock (idle)
        refused = await controller.handle(_envelope(RelocateDock()))
        assert refused.status is CommandStatus.REJECTED
        assert refused.reason == "the mower is idle, not in its dock"
        assert controller.drive_session_active  # a refusal does not end the session
        assert mower.maintenance_log == ["relocate_dock"]
    finally:
        await controller.close()


async def test_control_leave_dock_is_a_motion_command() -> None:
    mower = _mower()
    controller = await _controller(mower)
    try:
        result = await controller.handle(_envelope(LeaveDock()))
        assert result.status is CommandStatus.ACCEPTED
        assert (await mower.get_state()).activity is MowerActivity.IDLE
        again = await controller.handle(_envelope(LeaveDock()))
        assert again.status is CommandStatus.REJECTED
        assert again.reason == "the mower is idle, not in its dock"
        frame = await controller.handle(_envelope(ManualDrive(linear=0.0, angular=0.0)))
        assert frame.status is CommandStatus.ACCEPTED
        await controller.handle(_envelope(LeaveDock()))
        assert not controller.drive_session_active  # ended like by a job command
    finally:
        await controller.close()


async def test_control_leave_dock_refused_during_a_sensor_stop() -> None:
    mower = _mower()
    controller = await _controller(mower)
    try:
        mower.lift()
        result = await controller.handle(_envelope(LeaveDock()))
        assert result.status is CommandStatus.REJECTED
        assert result.reason is not None and "reports an error" in result.reason
    finally:
        await controller.close()


async def test_control_resync_rtk_keeps_a_drive_session_and_runs_while_mowing() -> None:
    mower = _mower()
    controller = await _controller(mower)
    try:
        frame = await controller.handle(_envelope(ManualDrive(linear=0.0, angular=0.0)))
        assert frame.status is CommandStatus.ACCEPTED
        result = await controller.handle(_envelope(ResyncRtk()))
        assert result.status is CommandStatus.EXECUTED
        assert controller.drive_session_active
        await controller.end_drive_session()
        started = await controller.handle(_envelope(StartMow(area_ids=["front"])))
        assert started.status is CommandStatus.EXECUTED
        while_mowing = await controller.handle(_envelope(ResyncRtk()))
        assert while_mowing.status is CommandStatus.EXECUTED
        assert mower.maintenance_log == ["resync_rtk", "resync_rtk"]
    finally:
        await controller.close()


async def test_control_sync_map_refreshes_the_controller_map() -> None:
    mower = _mower()
    controller = await _controller(mower)
    try:
        result = await controller.handle(_envelope(SyncMap()))
        assert result.status is CommandStatus.EXECUTED
        assert result.reason == "map read again: 2 areas"
        assert await controller.mower_map() == await mower.get_map()
        frame = await controller.handle(_envelope(ManualDrive(linear=0.0, angular=0.0)))
        assert frame.status is CommandStatus.ACCEPTED
        refused = await controller.handle(_envelope(SyncMap()))
        assert refused.status is CommandStatus.REJECTED
        assert refused.reason == "a manual drive session is open; stop driving first"
    finally:
        await controller.close()


class _SlowSync(SimulatedMower):
    """A simulated mower whose map sync waits for ``gate``."""

    def __init__(self) -> None:
        super().__init__(mower_id=MOWER, account_id=ACCOUNT)
        self.gate = asyncio.Event()
        self.gate_waiting = asyncio.Event()

    async def run_maintenance(self, command: MaintenanceCommand) -> Outcome:
        if isinstance(command, SyncMap):
            self.gate_waiting.set()
            await self.gate.wait()
        return await super().run_maintenance(command)


async def test_control_sync_map_runs_outside_the_queue_one_at_a_time() -> None:
    mower = _SlowSync()
    controller = await _controller(mower)
    try:
        sync = asyncio.create_task(controller.handle(_envelope(SyncMap())))
        async with asyncio.timeout(2):
            await mower.gate_waiting.wait()
        height = await controller.handle(_envelope(SetBladeHeight(mm=50)))
        assert height.status is CommandStatus.EXECUTED  # not held up by the transfer
        second = await controller.handle(_envelope(SyncMap()))
        assert second.status is CommandStatus.REJECTED
        assert second.reason == "a map sync is already running"
        restart = await controller.handle(_envelope(RestartMower()))
        assert restart.status is CommandStatus.REJECTED
        assert restart.reason == "a map sync is running; restart once it is done"
        mower.gate.set()
        first = await sync
        assert first.status is CommandStatus.EXECUTED
        assert mower.maintenance_log == ["sync_map"]
    finally:
        await controller.close()


async def test_control_maintenance_refused_during_a_firmware_update() -> None:
    mower = _mower(firmware_available="sim-1.1.0")
    controller = await _controller(mower)
    try:
        update = await controller.handle(_envelope(FirmwareUpdate(version="sim-1.1.0")))
        assert update.status is CommandStatus.EXECUTED
        for command in (ResyncRtk(), SyncMap(), RestartMower()):
            result = await controller.handle(_envelope(command))
            assert result.status is CommandStatus.REJECTED
            assert result.reason == "firmware update running"
    finally:
        await controller.close()
