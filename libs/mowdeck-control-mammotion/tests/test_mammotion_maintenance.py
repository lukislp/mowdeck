"""Maintenance commands (ADR 0048) on the Mammotion connections: what is declared, what goes
out (once, with user priority, never through ``send_and_wait``) and when it is refused."""

from datetime import timedelta
from uuid import uuid4

import pytest
from mammotion_support import ACCOUNT_ID, MOWER_ID, ble, cloud
from mowdeck_contracts.v1 import (
    MAINTENANCE_ACTIONS,
    CommandEnvelope,
    CommandStatus,
    LeaveDock,
    MowerActivity,
    MowerCommand,
    PrincipalKind,
    PrincipalRef,
    RelocateDock,
    RestartMower,
    ResyncRtk,
    SyncMap,
)
from mowdeck_control import (
    CommandRejected,
    MowerController,
    NotConnected,
    maintenance_port_of,
)
from mowdeck_control_mammotion import mapping
from mowdeck_control_mammotion.base import (
    DOCK_BUFFER_ARGS,
    DOCK_BUFFER_COMMAND,
    RELOCATE_DOCK_COMMAND,
    RESTART_COMMAND,
)
from mowdeck_control_mammotion.fake import FakeMammotionPort, dock_buffer
from mowdeck_control_mammotion.port import (
    NoTransportAvailableError,
    Priority,
    TransportError,
    WorkMode,
)


def _envelope(command: MowerCommand) -> CommandEnvelope:
    return CommandEnvelope.create(
        mower_id=MOWER_ID,
        account_id=ACCOUNT_ID,
        issued_by=PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER),
        command=command,
        ttl=timedelta(seconds=30),
    )


async def test_mammotion_connections_declare_every_maintenance_action() -> None:
    for make in (cloud, ble):
        conn, _ = make()
        assert maintenance_port_of(conn) is conn
        await conn.connect()
        try:
            assert (await conn.capabilities()).maintenance == frozenset(MAINTENANCE_ACTIONS)
        finally:
            await conn.disconnect()


def test_mammotion_luba1_has_no_remote_restart() -> None:
    luba1 = mapping.maintenance_actions_of("Luba-ABC123", undock=True)
    assert "restart_mower" not in luba1
    assert {"sync_map", "relocate_dock", "resync_rtk", "leave_dock"} <= luba1
    luba2 = mapping.maintenance_actions_of("Luba-VPDRYRUN")
    assert "restart_mower" in luba2
    assert "leave_dock" not in luba2  # only with undock


async def test_mammotion_restart_is_sent_once_with_user_priority() -> None:
    conn, port = cloud()
    await conn.connect()
    port.sent.clear()
    outcome = await conn.run_maintenance(RestartMower())
    assert outcome.status is CommandStatus.ACCEPTED
    assert [(c.name, c.via, c.priority, c.kwargs) for c in port.sent] == [
        (RESTART_COMMAND, "send", Priority.USER, {})
    ]


async def test_mammotion_restart_is_refused_while_mowing() -> None:
    conn, port = cloud()
    await conn.connect()
    port.set_mode(WorkMode.MODE_WORKING)
    port.sent.clear()
    with pytest.raises(CommandRejected, match="restart needs it docked, charging or idle"):
        await conn.run_maintenance(RestartMower())
    assert port.sent == []


async def test_mammotion_relocate_dock_is_sent_once_and_only_in_the_dock() -> None:
    conn, port = cloud()
    await conn.connect()
    port.sent.clear()
    outcome = await conn.run_maintenance(RelocateDock())
    assert outcome.status is CommandStatus.ACCEPTED
    assert [(c.name, c.via, c.priority) for c in port.sent] == [
        (RELOCATE_DOCK_COMMAND, "send", Priority.USER)
    ]
    await conn.leave_dock()
    assert (await conn.get_state()).activity is MowerActivity.IDLE
    port.sent.clear()
    with pytest.raises(CommandRejected, match="not in its dock"):
        await conn.run_maintenance(RelocateDock())
    assert port.sent == []


async def test_mammotion_resync_rtk_reads_the_dock_buffer_again() -> None:
    conn, port = cloud()
    await conn.connect()
    await port.deliver_dock_buffer()
    port.sent.clear()
    port.dock_answer = dock_buffer(dock_x=1.5, dock_y=-2.0)
    outcome = await conn.run_maintenance(ResyncRtk())
    assert outcome.status is CommandStatus.EXECUTED
    assert [(c.name, c.via, c.priority, c.kwargs) for c in port.sent] == [
        (DOCK_BUFFER_COMMAND, "send", Priority.USER, DOCK_BUFFER_ARGS)
    ]
    dock = mapping.dock_of(port.device())
    assert dock is not None
    assert dock.x == pytest.approx(1.5)
    assert dock.y == pytest.approx(-2.0)


async def test_mammotion_resync_rtk_without_a_new_answer_is_accepted() -> None:
    conn, port = cloud()
    await conn.connect()
    await port.deliver_dock_buffer()
    port.sent.clear()
    outcome = await conn.run_maintenance(ResyncRtk())  # the same values come back
    assert outcome.status is CommandStatus.ACCEPTED
    assert "did not change" in (outcome.reason or "")
    port.dock_answer = None
    port.sent.clear()
    outcome = await conn.run_maintenance(ResyncRtk())
    assert outcome.status is CommandStatus.ACCEPTED
    assert port.names() == [DOCK_BUFFER_COMMAND]


async def test_mammotion_resync_rtk_is_allowed_while_mowing() -> None:
    conn, port = cloud()
    await conn.connect()
    port.set_mode(WorkMode.MODE_WORKING)
    port.sent.clear()
    await conn.run_maintenance(ResyncRtk())
    assert port.names() == [DOCK_BUFFER_COMMAND]


async def test_mammotion_sync_map_reads_the_map_again_even_when_fresh() -> None:
    conn, port = cloud()
    await conn.connect()
    await conn.get_map()
    syncs = port.map_syncs
    await conn.get_map()
    assert port.map_syncs == syncs  # fresh: no second sync on a read
    outcome = await conn.run_maintenance(SyncMap())
    assert outcome.status is CommandStatus.EXECUTED
    assert outcome.reason is not None and outcome.reason.startswith("map read again")
    assert port.map_syncs == syncs + 1
    await conn.get_map()
    assert port.map_syncs == syncs + 1  # the sync counts as fresh


async def test_mammotion_sync_map_timeout_is_accepted() -> None:
    conn, port = cloud()
    await conn.connect()
    port.map_sync_error = TimeoutError("slow")
    outcome = await conn.run_maintenance(SyncMap())
    assert outcome.status is CommandStatus.ACCEPTED
    assert "did not finish" in (outcome.reason or "")


async def test_mammotion_maintenance_needs_a_link() -> None:
    conn, port = cloud()
    with pytest.raises(NotConnected):
        await conn.run_maintenance(RestartMower())
    await conn.connect()
    port.fail[RESTART_COMMAND] = NoTransportAvailableError("gone")
    with pytest.raises(NotConnected):
        await conn.run_maintenance(RestartMower())
    port.fail[RELOCATE_DOCK_COMMAND] = TransportError("broken")
    outcome = await conn.run_maintenance(RelocateDock())
    assert outcome.status is CommandStatus.FAILED


async def test_mammotion_maintenance_through_the_controller() -> None:
    port = FakeMammotionPort()
    conn, _ = cloud(port)
    async with MowerController(conn, mower_id=MOWER_ID) as controller:
        port.sent.clear()
        restart = await controller.handle(_envelope(RestartMower()))
        assert restart.status is CommandStatus.ACCEPTED
        relocate = await controller.handle(_envelope(RelocateDock()))
        assert relocate.status is CommandStatus.ACCEPTED
        leave = await controller.handle(_envelope(LeaveDock()))
        assert leave.status is CommandStatus.ACCEPTED
        # out of the dock now: relocating the dock is refused by the controller
        refused = await controller.handle(_envelope(RelocateDock()))
        assert refused.status is CommandStatus.REJECTED
        assert refused.reason == "the mower is idle, not in its dock"
    user = [c for c in port.sent if c.priority is Priority.USER]  # not the device info rounds
    assert [c.name for c in user] == [RESTART_COMMAND, RELOCATE_DOCK_COMMAND, "leave_dock"]
    assert all(c.via == "send" for c in user)
