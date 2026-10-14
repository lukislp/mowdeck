"""BLE proxy connection: drive session frames, stop, blade gating, link loss, proxy port."""

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from mammotion_support import ACCOUNT_ID, BLE_OPTIONS, MOWER_ID, ble
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandStatus,
    ConnectionKind,
    ManualDrive,
    PrincipalKind,
    PrincipalRef,
)
from mowdeck_control import CommandRejected, MowerController, NotConnected, SensorStop
from mowdeck_control.testing import ManualClock, ManualTimers
from mowdeck_control_mammotion import ble as ble_module
from mowdeck_control_mammotion.drive import BleDriveSession
from mowdeck_control_mammotion.esphome import ProxyLink
from mowdeck_control_mammotion.fake import FakeMammotionPort
from mowdeck_control_mammotion.options import BleProxyOptions
from mowdeck_control_mammotion.port import (
    NoTransportAvailableError,
    Priority,
    TransportError,
    WorkMode,
)


async def test_mammotion_ble_capabilities_offer_manual_drive_without_blades() -> None:
    conn, _ = ble()
    assert conn.kind is ConnectionKind.BLE_BRIDGE
    await conn.connect()
    caps = await conn.capabilities()
    assert caps.manual_drive is True
    assert caps.manual_drive_blades is False
    assert (await conn.get_state()).raw["link"] == "ble_proxy"
    allowed, _ = ble(allow_manual_blades=True)
    await allowed.connect()
    assert (await allowed.capabilities()).manual_drive_blades is True


async def test_mammotion_drive_frames_stop_and_close() -> None:
    conn, port = ble()
    await conn.connect()
    session = await conn.start_drive_session()
    await session.update(0.5, 0.0, False)
    await session.update(0.0, -1.0, False)
    assert [(c.name, c.kwargs, c.priority) for c in port.sent] == [
        ("send_movement", {"linear_speed": 350, "angular_speed": 0}, Priority.USER),
        ("send_movement", {"linear_speed": 0, "angular_speed": 382}, Priority.USER),
    ]
    port.sent.clear()
    await session.stop()
    assert port.names() == ["send_movement", "operate_on_device"]
    assert port.sent[0].kwargs == {"linear_speed": 0, "angular_speed": 0}
    assert port.sent[1].kwargs["cut_knife_ctrl"] == 0
    assert {c.priority for c in port.sent} == {Priority.EMERGENCY}
    assert session.is_open
    await session.close()
    await session.close()  # idempotent
    assert not session.is_open
    with pytest.raises(CommandRejected, match="closed"):
        await session.update(0.1, 0.0, False)


async def test_mammotion_blades_stay_off_unless_allowed() -> None:
    conn, port = ble()
    await conn.connect()
    session = await conn.start_drive_session()
    await session.update(0.5, 0.0, True)
    assert port.names() == ["send_movement"]  # no blade command at all
    assert isinstance(session, BleDriveSession) and not session.blades_on


async def test_mammotion_blades_follow_the_frames_when_allowed() -> None:
    conn, port = ble(allow_manual_blades=True, manual_blade_height_mm=50)
    await conn.connect()
    session = await conn.start_drive_session()
    await session.update(0.5, 0.0, True)
    await session.update(0.5, 0.0, True)  # already on: not sent again
    await session.update(0.5, 0.0, False)
    assert port.names() == [
        "operate_on_device",
        "send_movement",
        "send_movement",
        "operate_on_device",
        "send_movement",
    ]
    on, off = port.sent[0].kwargs, port.sent[3].kwargs
    assert (on["main_ctrl"], on["cut_knife_ctrl"], on["cut_knife_height"]) == (1, 1, 50)
    assert (off["main_ctrl"], off["cut_knife_ctrl"]) == (0, 0)


async def test_mammotion_drive_sensor_stop_ends_the_session() -> None:
    conn, port = ble()
    await conn.connect()
    session = await conn.start_drive_session()
    port.set_mode(WorkMode.MODE_LOCK)
    with pytest.raises(SensorStop):
        await session.update(0.3, 0.0, False)
    assert not session.is_open
    assert port.names() == ["send_movement", "operate_on_device"]  # stopped first
    with pytest.raises(SensorStop):
        await conn.start_drive_session()


async def test_mammotion_drive_link_loss_raises_not_connected_and_stop_never_raises() -> None:
    conn, port = ble()
    await conn.connect()
    session = await conn.start_drive_session()
    port.fail["send_movement"] = NoTransportAvailableError("BLE gone")
    with pytest.raises(NotConnected):
        await session.update(0.5, 0.0, False)
    port.fail["operate_on_device"] = TransportError("gone")
    await session.stop()  # logs, does not raise
    await session.close()


async def test_mammotion_new_session_replaces_the_old_one_and_disconnect_closes_it() -> None:
    conn, _ = ble()
    await conn.connect()
    first = await conn.start_drive_session()
    second = await conn.start_drive_session()
    assert not first.is_open and second.is_open
    await conn.disconnect()
    assert not second.is_open


async def test_mammotion_ble_link_loss_ends_the_connection() -> None:
    conn, port = ble()
    await conn.connect()
    assert conn.is_connected
    port.link = False
    assert not conn.is_connected  # the Device-Gateway sees it and reconnects with backoff
    with pytest.raises(NotConnected):
        await conn.get_state()
    with pytest.raises(NotConnected):
        await conn.start_drive_session()


async def test_mammotion_dead_man_through_the_controller_stops_the_mower() -> None:
    conn, port = ble()
    clock = ManualClock()
    timers = ManualTimers(clock)
    async with MowerController(
        conn, mower_id=MOWER_ID, clock=clock, timer_factory=timers
    ) as controller:
        frame = CommandEnvelope.create(
            mower_id=MOWER_ID,
            account_id=ACCOUNT_ID,
            issued_by=PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER),
            command=ManualDrive(linear=0.6, angular=0.0, blade_on=True, ttl_ms=1000),
            ttl=timedelta(seconds=2),
            now=clock(),
        )
        result = await controller.handle(frame)
        assert result.status is CommandStatus.ACCEPTED
        assert "blade" in (result.reason or "")  # forced off: not allowed on this link
        assert port.names() == ["send_movement"]
        await timers.advance(1.5)
        assert not controller.drive_session_active
    assert port.names()[1:3] == ["send_movement", "operate_on_device"]
    assert port.sent[1].kwargs == {"linear_speed": 0, "angular_speed": 0}


# --- the real proxy port, with the ESPHome and PyMammotion calls replaced -------------------


class _Client:
    def __init__(self) -> None:
        self.stopped = False
        self.reauth_required = None

    async def stop(self) -> None:
        self.stopped = True


class _Transport:
    is_connected = True


class _Handle:
    device_name = "Luba-VPADV"
    has_usable_transport = True

    def is_transport_connected(self, _kind: Any) -> bool:
        return True


async def test_mammotion_ble_proxy_port_opens_link_then_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    alive = {"api": True}

    async def fake_open_proxy(**kwargs: Any) -> ProxyLink:
        calls.append(f"proxy {kwargs['host']}:{kwargs['port']} {kwargs['ble_address']}")

        async def close() -> None:
            calls.append("proxy closed")
            alive["api"] = False

        return ProxyLink(
            ble_device=object(),
            advertised_name="Luba-VPADV",
            proxy_name="mowdeck-bridge",
            close=close,
            is_alive=lambda: alive["api"],
        )

    client = _Client()

    async def fake_open_ble_client(*, device_name: str, ble_device: object) -> Any:
        calls.append(f"ble {device_name}")
        return client, _Handle()

    monkeypatch.setattr(ble_module, "open_proxy", fake_open_proxy)
    monkeypatch.setattr(ble_module, "open_ble_client", fake_open_ble_client)
    options = {k: v for k, v in BLE_OPTIONS.items() if k != "device_name"}
    port = ble_module.BleProxyPort(BleProxyOptions.parse(options))
    await port.open()
    assert calls == ["proxy mowdeck-bridge.local:6053 AA:BB:CC:DD:EE:FF", "ble Luba-VPADV"]
    assert port.is_open and port.proxy_alive and port.device_name == "Luba-VPADV"
    alive["api"] = False
    assert not port.is_open  # proxy API gone: the connection is down
    await port.close()
    assert client.stopped and calls[-1] == "proxy closed"
    assert not port.proxy_alive


async def test_mammotion_ble_proxy_port_needs_a_name(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_open_proxy(**_kwargs: Any) -> ProxyLink:
        async def close() -> None:
            return None

        return ProxyLink(object(), None, "proxy", close, lambda: True)

    monkeypatch.setattr(ble_module, "open_proxy", fake_open_proxy)
    options = {k: v for k, v in BLE_OPTIONS.items() if k != "device_name"}
    port = ble_module.BleProxyPort(BleProxyOptions.parse(options))
    with pytest.raises(CommandRejected, match="device_name"):
        await port.open()
    await port.close()


def test_mammotion_ble_factory_builds_a_proxy_connection() -> None:
    from mammotion_support import config

    conn = ble_module.create_ble_bridge_connection(config(dict(BLE_OPTIONS)))
    assert isinstance(conn, ble_module.MammotionBleProxyConnection)
    assert isinstance(conn.port, ble_module.BleProxyPort)
    assert conn.options.port == 6053


def test_mammotion_fake_port_is_a_mammotion_port() -> None:
    from mowdeck_control_mammotion.port import MammotionPort

    assert isinstance(FakeMammotionPort(), MammotionPort)


def test_mammotion_bleak_wrappers_route_pymammotion_through_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PyMammotion's bare ``BleakClient(address)`` must resolve through habluetooth."""
    import sys
    import types

    bleak = pytest.importorskip("bleak")
    habluetooth = pytest.importorskip("habluetooth")
    usage = pytest.importorskip("habluetooth.usage")
    from mowdeck_control_mammotion.esphome import install_bleak_wrappers

    # A module that bound the names at import time, like pymammotion.bluetooth.ble.
    bound = types.ModuleType("pymammotion._bound_for_test")
    bound.BleakClient = usage.ORIGINAL_BLEAK_CLIENT  # type: ignore[attr-defined]
    bound.BleakScanner = usage.ORIGINAL_BLEAK_SCANNER  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, bound.__name__, bound)
    try:
        install_bleak_wrappers()
        install_bleak_wrappers()  # idempotent
        assert bleak.BleakClient is habluetooth.HaBleakClientWrapper
        assert bound.BleakClient is habluetooth.HaBleakClientWrapper
        assert bound.BleakScanner is habluetooth.HaBleakScannerWrapper
    finally:
        usage.uninstall_multiple_bleak_catcher()
