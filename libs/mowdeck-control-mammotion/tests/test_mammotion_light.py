"""The camera headlamp: capability, the connect-time read, ``SetLight`` and the mapped state."""

import asyncio

import pytest
from mammotion_support import cloud
from mowdeck_contracts.v1 import CommandStatus, MowerState, SetLight
from mowdeck_control import CommandRejected
from mowdeck_control_mammotion import base as mammotion_base
from mowdeck_control_mammotion import mapping
from mowdeck_control_mammotion.base import LIGHT_COMMAND, LIGHT_READ_COMMAND
from mowdeck_control_mammotion.fake import FakeMammotionPort, sample_device
from mowdeck_control_mammotion.port import LampInfo, Priority, TransportError, WorkMode

READ = {"ids": 1126}


def _reads(port: FakeMammotionPort) -> list[dict[str, object]]:
    return [c.kwargs for c in port.sent if c.name == LIGHT_READ_COMMAND and c.kwargs == READ]


@pytest.mark.parametrize(
    ("name", "product_key", "expected"),
    [
        ("Luba-VPDRYRUN", "a1iMygIwxFC", True),
        ("Luba-VPAWDX01", "", True),  # a LUBA 2 AWD X
        ("Luba-VA1234", "", True),
        ("Luba-12345", "", False),  # Luba 1: no headlamp
        ("Yuka-12345", "", False),
        ("RTK-12345", "", False),
    ],
)
def test_mammotion_light_capability_follows_the_fill_light_gate(
    name: str, product_key: str, expected: bool
) -> None:
    assert mapping.has_light(name, product_key) is expected


def test_mammotion_light_state_mapping() -> None:
    device = sample_device()
    assert mapping.light_on_of(device) is None  # PyMammotion's default: not reported
    assert mapping.light_on_of(device, reported=True) is False  # an answered all-zero read
    device.mower_state.lamp_info = LampInfo(lamp_bright=80, manual_light=True)
    assert mapping.light_reported(device)
    assert mapping.light_on_of(device) is True
    device.mower_state.lamp_info = LampInfo(manual_light=False)
    assert mapping.light_on_of(device) is None  # "off" looks like the default
    assert mapping.light_on_of(device, reported=True) is False


async def test_mammotion_light_is_read_after_connect() -> None:
    port = FakeMammotionPort()
    port.light_answer = True
    conn, _ = cloud(port)
    assert (await _connected(conn)).light_on is True
    assert (await conn.capabilities()).light is True
    reads = [c for c in port.sent if c.name == LIGHT_READ_COMMAND and c.kwargs == READ]
    assert len(reads) == 1
    assert reads[0].priority is Priority.BACKGROUND
    assert reads[0].expected_field == "get_lamp_rsp"
    state = await conn.get_state()
    assert state.raw["manual_light"] is True
    assert state.raw["lamp_bright"] == 80
    assert not [k for k in state.raw if k.startswith("side_led")]
    await conn.disconnect()


async def test_mammotion_light_read_is_retried_while_unknown_and_bounded() -> None:
    port = FakeMammotionPort()
    port.light_answer = None  # the mower does not answer
    conn, _ = cloud(port)
    conn._device_info_retry_s = 0.01
    await conn.connect()
    await asyncio.sleep(0.2)
    assert len(_reads(port)) == mammotion_base.DEVICE_INFO_ATTEMPTS
    state = await conn.get_state()
    assert state.light_on is None
    assert "manual_light" not in state.raw
    await conn.disconnect()


async def test_mammotion_light_read_failures_are_ignored() -> None:
    port = FakeMammotionPort()
    port.fail[LIGHT_READ_COMMAND] = TransportError("offline")
    conn, _ = cloud(port)
    conn._device_info_retry_s = 0.01
    await conn.connect()
    await asyncio.sleep(0.1)
    assert (await conn.get_state()).light_on is None
    await conn.disconnect()


async def test_mammotion_set_light_writes_then_reads_back() -> None:
    port = FakeMammotionPort()
    conn, _ = cloud(port)
    assert (await _connected(conn)).light_on is False
    outcome = await conn.execute(SetLight(on=True))
    assert outcome.status is CommandStatus.EXECUTED
    assert outcome.reason == "light is on"
    assert port.light_writes() == [True]
    write = next(c for c in port.sent if c.name == LIGHT_COMMAND)
    assert write.kwargs == {"manual_ctrl": True}
    assert write.priority is Priority.USER
    assert port.names()[-2:] == [LIGHT_COMMAND, LIGHT_READ_COMMAND]  # write, then read
    assert (await conn.get_state()).light_on is True
    assert (await conn.execute(SetLight(on=False))).status is CommandStatus.EXECUTED
    assert port.light_writes() == [True, False]
    assert (await conn.get_state()).light_on is False
    await conn.disconnect()


async def test_mammotion_set_light_unconfirmed_is_accepted() -> None:
    port = FakeMammotionPort()
    port.light_follows_writes = False  # the mower keeps reporting off
    conn, _ = cloud(port)
    await _connected(conn)
    outcome = await conn.execute(SetLight(on=True))
    assert outcome.status is CommandStatus.ACCEPTED
    port.light_answer = None  # no answer at all
    assert (await conn.execute(SetLight(on=True))).status is CommandStatus.ACCEPTED
    await conn.disconnect()


async def test_mammotion_set_light_runs_during_an_error() -> None:
    conn, port = cloud()
    await _connected(conn)
    port.set_mode(WorkMode.MODE_SYSTEM_ERROR)
    assert (await conn.execute(SetLight(on=True))).status is CommandStatus.EXECUTED
    port.fail[LIGHT_COMMAND] = TransportError("x")
    assert (await conn.execute(SetLight(on=False))).status is CommandStatus.FAILED
    await conn.disconnect()


async def test_mammotion_set_light_without_a_light_is_rejected() -> None:
    port = FakeMammotionPort(
        device=sample_device("Yuka-12345", product_key=""),
        device_name="Yuka-12345",
        product_key="",
    )
    conn, _ = cloud(port)
    await conn.connect()
    await asyncio.sleep(0.01)
    assert (await conn.capabilities()).light is False
    assert not [c for c in port.sent if c.name == LIGHT_READ_COMMAND]  # no read either
    with pytest.raises(CommandRejected, match="no switchable light"):
        await conn.execute(SetLight(on=True))
    assert (await conn.get_state()).light_on is None
    await conn.disconnect()


async def _connected(conn: mammotion_base.MammotionBase) -> MowerState:
    await conn.connect()
    await asyncio.sleep(0.01)  # the connect-time read runs in the background
    return await conn.get_state()
