"""BLE proxy connection: drive session frames, stop, blade gating, link loss, proxy port."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
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
from mowdeck_control import (
    CommandRejected,
    MowerController,
    NotConnected,
    ReportRateCapable,
    SensorStop,
    TrackingRateCapable,
)
from mowdeck_control.testing import ManualClock, ManualTimers
from mowdeck_control_mammotion import base as mammotion_base
from mowdeck_control_mammotion import ble as ble_module
from mowdeck_control_mammotion.drive import (
    MANUAL_RUN_SPEED_CAP_MPS,
    BleDriveSession,
    manual_run_speed,
)
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
    port.sent.clear()  # the opening operate_on_device, see the max_run_speed tests
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


async def test_mammotion_drive_session_opens_with_max_run_speed() -> None:
    conn, port = ble(manual_blade_height_mm=50)
    await conn.connect()
    port.device().mower_state.travel_speed = 0.4
    port.sent.clear()
    session = await conn.start_drive_session()
    assert [(c.name, c.kwargs, c.priority) for c in port.sent] == [
        (
            "operate_on_device",
            {"main_ctrl": 0, "cut_knife_ctrl": 0, "cut_knife_height": 50, "max_run_speed": 0.4},
            Priority.USER,
        )
    ]
    port.sent.clear()
    await session.stop()  # the stop repeats the session's speed, never 0
    assert port.sent[1].kwargs["max_run_speed"] == 0.4
    await session.close()


@pytest.mark.parametrize(
    ("option", "travel_speed", "expected"),
    [(None, 0.6, 0.6), (None, 1.2, 1.0), (None, 0.0, 1.0), (0.8, 0.3, 0.8)],
)
async def test_mammotion_drive_session_speed_from_option_or_setting(
    option: float | None, travel_speed: float, expected: float
) -> None:
    extra: dict[str, Any] = {} if option is None else {"manual_drive_speed_mps": option}
    conn, port = ble(**extra)
    await conn.connect()
    port.device().mower_state.travel_speed = travel_speed
    port.sent.clear()
    session = await conn.start_drive_session()
    assert isinstance(session, BleDriveSession)
    assert session.max_run_speed_mps == expected
    assert port.sent[0].kwargs["max_run_speed"] == expected
    await session.close()


def test_mammotion_manual_run_speed_rules() -> None:
    assert manual_run_speed(None, None) == MANUAL_RUN_SPEED_CAP_MPS == 1.0
    assert manual_run_speed(0.5, 0.9) == 0.5
    assert manual_run_speed(None, 0.35) == 0.35
    assert manual_run_speed(2.0, None) == 1.0


async def test_mammotion_drive_session_open_fails_without_a_link() -> None:
    conn, port = ble()
    await conn.connect()
    port.fail["operate_on_device"] = NoTransportAvailableError("BLE gone")
    with pytest.raises(NotConnected):
        await conn.start_drive_session()


async def test_mammotion_blades_stay_off_unless_allowed() -> None:
    conn, port = ble()
    await conn.connect()
    session = await conn.start_drive_session()
    port.sent.clear()
    await session.update(0.5, 0.0, True)
    assert port.names() == ["send_movement"]  # no blade command at all
    assert isinstance(session, BleDriveSession) and not session.blades_on


async def test_mammotion_blades_follow_the_frames_when_allowed() -> None:
    conn, port = ble(allow_manual_blades=True, manual_blade_height_mm=50)
    await conn.connect()
    session = await conn.start_drive_session()
    port.sent.clear()
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
    port.sent.clear()
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
        assert port.names() == ["operate_on_device", "send_movement"]
        await timers.advance(1.5)
        assert not controller.drive_session_active
    assert port.names()[2:4] == ["send_movement", "operate_on_device"]
    assert port.sent[2].kwargs == {"linear_speed": 0, "angular_speed": 0}


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
    bound.BleakClientWithServiceCache = (  # type: ignore[attr-defined]
        usage.ORIGINAL_BLEAK_RETRY_CONNECTOR_CLIENT_WITH_SERVICE_CACHE
    )
    monkeypatch.setitem(sys.modules, bound.__name__, bound)
    try:
        install_bleak_wrappers()
        install_bleak_wrappers()  # idempotent
        assert bleak.BleakClient is habluetooth.HaBleakClientWrapper
        assert bound.BleakClient is habluetooth.HaBleakClientWrapper
        assert bound.BleakScanner is habluetooth.HaBleakScannerWrapper
        assert bound.BleakClientWithServiceCache is usage.HaBleakClientWithServiceCache
    finally:
        usage.uninstall_multiple_bleak_catcher()


async def test_mammotion_ble_drive_session_keeps_reports_coming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drive session (a spot run keeps one open) switches the refresh loop to drive mode:
    the report window is opened and undebounced polls follow while no report arrives."""
    monkeypatch.setattr(mammotion_base, "DRIVE_POLL_S", 0.02)
    conn, port = ble()
    await conn.connect()
    assert conn._mode() == "poll"
    session = await conn.start_drive_session()
    assert conn._mode() == "drive"
    async with asyncio.timeout(5):
        while port.refreshes < 3:  # noqa: ASYNC110 - the loop runs on its own task
            await asyncio.sleep(0.005)
    assert port.report_stream_requests, "the report window was not opened"
    await session.close()
    assert conn._mode() != "drive"  # back to the boost after the session start, then poll
    await conn.disconnect()


async def test_mammotion_ble_drive_poll_skips_while_reports_flow() -> None:
    conn, port = ble()
    await conn.connect()
    session = await conn.start_drive_session()
    await conn._drive_poll()  # the session start already asked; a fresh report just came in
    refreshes = port.refreshes
    conn._report_at = conn._clock_s()
    await conn._drive_poll()
    assert port.refreshes == refreshes, "a fresh report must not trigger a poll"
    conn._report_at = conn._clock_s() - 2 * mammotion_base.DRIVE_POLL_S
    await conn._drive_poll()
    assert port.refreshes == refreshes + 1
    await session.close()
    await conn.disconnect()


async def test_mammotion_ble_drive_report_interval_for_a_calibration() -> None:
    """A drive calibration asks for faster reports (ReportRateCapable): the drive interval drops
    to the request, never below the floor, and returns to the default afterwards."""
    conn, port = ble()
    assert isinstance(conn, ReportRateCapable)
    await conn.connect()
    session = await conn.start_drive_session()
    assert conn._current_refresh_interval_s() == mammotion_base.DRIVE_POLL_S
    conn.set_drive_report_interval(0.5)
    assert conn.drive_poll_s == 0.5 and conn._current_refresh_interval_s() == 0.5
    await conn._drive_poll()
    refreshes = port.refreshes
    conn._report_at = conn._clock_s() - 0.6  # older than 0.9 x 0.5 s, younger than 0.9 s
    await conn._drive_poll()
    assert port.refreshes == refreshes + 1
    conn.set_drive_report_interval(0.05)
    assert conn.drive_poll_s == mammotion_base.MIN_DRIVE_POLL_S
    conn.set_drive_report_interval(None)
    assert conn.drive_poll_s == mammotion_base.DRIVE_POLL_S
    conn._report_at = conn._clock_s() - 0.6
    await conn._drive_poll()
    assert port.refreshes == refreshes + 1, "the default interval does not poll after 0.6 s"
    await session.close()
    await conn.disconnect()


async def test_mammotion_ble_publishes_every_report_while_driving() -> None:
    conn, port = ble()
    await conn.connect()
    seen: list[object] = []
    stream = conn.watch_state()
    session = await conn.start_drive_session()

    async def collect() -> None:
        async for state in stream:
            seen.append(state.observed_at)

    task = asyncio.create_task(collect())
    await asyncio.sleep(0)
    before = len(seen)
    for _ in range(3):
        await port.emit()  # unchanged device, still published while driving
        await asyncio.sleep(0)
    assert len(seen) >= before + 3
    task.cancel()
    await session.close()
    await conn.disconnect()


# --- closing releases the proxy (the mower advertises again for other entries) ---------------


class _ProxyClient(_Client):
    """A PyMammotion client that answers sends and never the waits."""

    async def send_command_with_args(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def send_command_and_wait(self, *_args: Any, **_kwargs: Any) -> Any:
        raise TimeoutError("no answer")


class _ProxyHandle(_Handle):
    device_name = "Luba-VPDRYRUN"
    product_key = ""
    firmware_version = ""
    iot_id = ""
    last_report_data_at = 0.0

    def __init__(self) -> None:
        from mowdeck_control_mammotion.fake import sample_device

        self.snapshot = SimpleNamespace(raw=sample_device())
        self.queue = SimpleNamespace(is_saga_active=False)

    def subscribe_state_changed(self, _listener: Any) -> Any:
        return SimpleNamespace(cancel=lambda: None)

    def subscribe_notification(self, _listener: Any) -> Any:
        return SimpleNamespace(cancel=lambda: None)


class _Proxy:
    """``open_proxy`` and ``open_ble_client`` of the tests: records every proxy close."""

    def __init__(self) -> None:
        self.closes = 0
        self.client = _ProxyClient()
        self.ble_gate: asyncio.Event | None = None
        self.ble_started = asyncio.Event()

    async def open_proxy(self, **kwargs: Any) -> ProxyLink:
        alive = {"api": True}

        async def close() -> None:
            self.closes += 1
            alive["api"] = False

        return ProxyLink(
            ble_device=object(),
            advertised_name="Luba-VPDRYRUN",
            proxy_name="mowdeck-bridge",
            close=close,
            is_alive=lambda: alive["api"],
            host=kwargs["host"],
            port=kwargs["port"],
        )

    async def open_ble_client(self, *, device_name: str, ble_device: object) -> Any:
        self.ble_started.set()
        if self.ble_gate is not None:
            await self.ble_gate.wait()
        return self.client, _ProxyHandle()


def _proxy_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ble_module.MammotionBleProxyConnection, ble_module.BleProxyPort, _Proxy]:
    from mammotion_support import config

    proxy = _Proxy()
    monkeypatch.setattr(ble_module, "open_proxy", proxy.open_proxy)
    monkeypatch.setattr(ble_module, "open_ble_client", proxy.open_ble_client)
    conn = ble_module.create_ble_bridge_connection(config(dict(BLE_OPTIONS)))
    assert isinstance(conn.port, ble_module.BleProxyPort)
    return conn, conn.port, proxy


async def test_mammotion_ble_disconnect_closes_the_proxy_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, port, proxy = _proxy_connection(monkeypatch)
    await conn.connect()
    assert port.proxy_alive
    await conn.disconnect()
    await conn.disconnect()  # idempotent
    assert proxy.closes == 1
    assert proxy.client.stopped
    assert not port.proxy_alive


async def test_mammotion_ble_disconnect_closes_the_proxy_after_a_failed_background_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A background task that had failed must not end the disconnect before the port."""
    conn, _, proxy = _proxy_connection(monkeypatch)
    await conn.connect()

    async def broken() -> None:
        raise RuntimeError("device information round failed")

    failed = asyncio.create_task(broken())
    await asyncio.gather(failed, return_exceptions=True)
    conn._device_info_task = failed  # the state a failed round leaves behind
    await conn.disconnect()
    assert proxy.closes == 1 and proxy.client.stopped


async def test_mammotion_ble_cancelled_connect_closes_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A removal while the Bluetooth link comes up must not leave the proxy open."""
    conn, port, proxy = _proxy_connection(monkeypatch)
    proxy.ble_gate = asyncio.Event()  # the BLE connect hangs
    connecting = asyncio.create_task(conn.connect())
    await proxy.ble_started.wait()  # the proxy is open, the BLE connect waits
    assert port.proxy_alive
    connecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connecting
    assert proxy.closes == 1
    assert not port.proxy_alive
    await conn.disconnect()
    assert proxy.closes == 1


async def test_mammotion_client_port_runs_the_closer_when_stopping_is_cancelled() -> None:
    from mowdeck_control_mammotion.port import ClientPort

    closed: list[str] = []

    class _Stuck(_Client):
        async def stop(self) -> None:
            raise asyncio.CancelledError

    async def opener() -> Any:
        return _Stuck(), _Handle()

    async def on_close() -> None:
        closed.append("proxy")

    port = ClientPort(opener, on_close=on_close)
    await port.open()
    with pytest.raises(asyncio.CancelledError):
        await port.close()
    assert closed == ["proxy"]


async def test_mammotion_ble_tracking_interval_asks_for_reports_and_backs_off() -> None:
    """The theft tracking lease (TrackingRateCapable, ADR 0053): the loop switches to ``track``
    mode with one undebounced request per interval, a mower that stays silent is asked less
    often (never beyond the backoff cap), and releasing restores the normal rhythm."""
    conn, port = ble()
    assert isinstance(conn, TrackingRateCapable)
    await conn.connect()
    assert conn.set_tracking_interval(1.0) == mammotion_base.TRACKING_MIN_INTERVAL_S  # clamped
    assert conn._mode() == "track"
    assert conn._current_refresh_interval_s() == 5.0
    await conn._track_poll()
    assert port.refreshes >= 1
    asked = port.refreshes
    for _ in range(6):  # no report ever arrives: silent polls
        conn._report_at = None
        await conn._track_poll()
    assert port.refreshes == asked + 6, "it keeps asking, only slower"
    assert conn._tracking_silent_polls >= 6
    assert conn._current_refresh_interval_s() == 20.0  # two doublings of 5 s
    conn._tracking_silent_polls = 300
    assert conn._current_refresh_interval_s() == mammotion_base.TRACKING_BACKOFF_MAX_S
    conn._report_at = conn._clock_s() + 1  # a report arrived since the last request
    await conn._track_poll()
    assert conn._tracking_silent_polls == 0
    assert conn.set_tracking_interval(30.0) == 30.0
    assert conn._current_refresh_interval_s() == 30.0
    assert conn.set_tracking_interval(None) is None
    assert conn._mode() != "track"
    await conn.disconnect()


async def test_mammotion_ble_tracking_survives_a_failing_request() -> None:
    conn, port = ble()
    await conn.connect()
    conn.set_tracking_interval(5.0)

    async def broken() -> None:
        raise ConnectionError("cloud said no")

    port.refresh = broken  # type: ignore[method-assign]
    await conn._track_poll()  # logged and counted, never raised
    assert conn._tracking_silent_polls == 1
    await conn.disconnect()
