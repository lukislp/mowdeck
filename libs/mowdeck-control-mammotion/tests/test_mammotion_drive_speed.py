"""The speed setting for manual driving (ADR 0042) on the Mammotion connections."""

from datetime import timedelta
from uuid import uuid4

import pytest
from mammotion_support import ACCOUNT_ID, MOWER_ID, ble, cloud
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandStatus,
    DriveSpeedSetting,
    PrincipalKind,
    PrincipalRef,
    SetDriveSpeed,
)
from mowdeck_control import CommandRejected, MowerController, NotConnected, drive_speed_port_of
from mowdeck_control_mammotion import mapping
from mowdeck_control_mammotion.base import DRIVE_SPEED_REPLY
from mowdeck_control_mammotion.fake import FakeMammotionPort, sample_device
from mowdeck_control_mammotion.port import (
    NoTransportAvailableError,
    Priority,
    TransportError,
    model_config_of,
)


def _envelope(command: SetDriveSpeed) -> CommandEnvelope:
    return CommandEnvelope.create(
        mower_id=MOWER_ID,
        account_id=ACCOUNT_ID,
        issued_by=PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER),
        command=command,
        ttl=timedelta(seconds=30),
    )


async def test_mammotion_connections_declare_the_drive_speed_setting() -> None:
    for make in (cloud, ble):
        conn, port = make()
        assert drive_speed_port_of(conn) is conn
        await conn.connect()
        try:
            setting = (await conn.capabilities()).drive_speed_setting
            config = model_config_of(port.device(), port.product_key())
            assert config is not None
            assert setting == DriveSpeedSetting(
                min_mps=config["working_speed_min"],
                max_mps=config["working_speed_max"],
                step_mps=0.1,
            )
        finally:
            await conn.disconnect()


def test_mammotion_drive_speed_range_falls_back_without_a_model() -> None:
    device = sample_device(sub_model_id="", product_key="unknown")
    setting = mapping.drive_speed_setting_of(device, "unknown")
    assert (setting.min_mps, setting.max_mps, setting.step_mps) == (0.2, 1.2, 0.1)


def _unread() -> FakeMammotionPort:
    """A mower that has not reported its speed setting yet."""
    device = sample_device()
    device.mower_state.travel_speed = 0.0
    return FakeMammotionPort(device)


async def test_mammotion_get_drive_speed_reads_and_reports_it() -> None:
    conn, port = cloud(_unread())
    await conn.connect()
    assert (await conn.get_state()).drive_speed_mps is None
    port.speed_answer = 0.7
    assert await conn.get_drive_speed() == pytest.approx(0.7)
    assert port.names() == ["get_speed"]
    sent = port.sent[0]
    assert sent.expected_field == DRIVE_SPEED_REPLY
    assert sent.priority is Priority.USER and sent.kwargs == {}
    assert (await conn.get_state()).drive_speed_mps == pytest.approx(0.7)


async def test_mammotion_get_drive_speed_returns_none_without_an_answer() -> None:
    conn, port = cloud(_unread())
    port.speed_answer = None
    await conn.connect()
    assert await conn.get_drive_speed() is None
    assert (await conn.get_state()).drive_speed_mps is None


async def test_mammotion_set_drive_speed_sends_once_and_reads_back() -> None:
    conn, port = ble()
    await conn.connect()
    await conn.set_drive_speed(0.9)
    assert [(c.name, c.via, c.priority) for c in port.sent] == [
        ("set_speed", "send", Priority.USER),
        ("get_speed", "send_and_wait", Priority.USER),
    ]
    assert port.sent[0].kwargs == {"speed": 0.9}
    assert (await conn.get_state()).drive_speed_mps == pytest.approx(0.9)


async def test_mammotion_set_drive_speed_survives_a_missing_read_back() -> None:
    conn, port = cloud()
    await conn.connect()
    port.speed_answer = None
    await conn.set_drive_speed(0.5)
    assert port.names() == ["set_speed", "get_speed"]


async def test_mammotion_drive_speed_needs_a_link() -> None:
    conn, port = cloud()
    with pytest.raises(NotConnected):
        await conn.get_drive_speed()
    with pytest.raises(NotConnected):
        await conn.set_drive_speed(0.5)
    await conn.connect()
    port.fail["set_speed"] = NoTransportAvailableError("gone")
    with pytest.raises(NotConnected):
        await conn.set_drive_speed(0.5)
    port.fail["get_speed"] = TransportError("broken")
    with pytest.raises(CommandRejected, match="transport error"):
        await conn.get_drive_speed()


async def test_mammotion_set_drive_speed_through_the_controller_is_clamped() -> None:
    port = FakeMammotionPort()
    conn, _ = cloud(port)
    async with MowerController(conn, mower_id=MOWER_ID) as controller:
        setting = (await controller.capabilities()).drive_speed_setting
        assert setting is not None
        result = await controller.handle(_envelope(SetDriveSpeed(speed_mps=3.0)))
        assert result.status is CommandStatus.EXECUTED
    writes = [c for c in port.sent if c.name == "set_speed"]
    assert [c.kwargs for c in writes] == [{"speed": setting.max_mps}]
    assert all(c.link == "cloud" for c in port.sent)
