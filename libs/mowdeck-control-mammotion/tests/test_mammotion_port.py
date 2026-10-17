"""The real PyMammotion port and the ESPHome proxy wiring, with the network calls replaced."""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from mowdeck_control_mammotion import esphome, port
from mowdeck_control_mammotion.fake import sample_device
from mowdeck_control_mammotion.port import (
    ClientPort,
    FirmwareRefused,
    MowerDevice,
    NoTransportAvailableError,
    PortClosedError,
    Priority,
    TransportType,
    vendor_call_succeeded,
)

PASSWORD = "test-only"  # noqa: S105


class _Snapshot:
    def __init__(self, raw: object) -> None:
        self.raw = raw


class _Subscription:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _Queue:
    is_saga_active = False


class _Transport:
    def __init__(self) -> None:
        self.connected = False

    async def connect(self) -> None:
        self.connected = True


class _Handle:
    def __init__(self, name: str = "Luba-VP1", iot_id: str = "iot-1", raw: object = None) -> None:
        self.device_name = name
        self.iot_id = iot_id
        self.product_key = "a1nf9kRBWoH"
        self.firmware_version = "1.30.29.26"
        self.snapshot = _Snapshot(sample_device(name) if raw is None else raw)
        self.has_usable_transport = True
        self.last_report_data_at = 12.5
        self.queue = _Queue()
        self.started = False
        self.transport: _Transport | None = _Transport()
        self.subscriptions: list[_Subscription] = []
        self.listeners: list[Callable[[object], Awaitable[None]]] = []

    def is_transport_connected(self, kind: TransportType) -> bool:
        return kind is TransportType.BLE

    def subscribe_state_changed(
        self, handler: Callable[[object], Awaitable[None]]
    ) -> _Subscription:
        self.listeners.append(handler)
        sub = _Subscription()
        self.subscriptions.append(sub)
        return sub

    async def start(self) -> None:
        self.started = True

    def get_transport(self, _kind: TransportType) -> _Transport | None:
        return self.transport


class _Registry:
    def __init__(self, handles: list[_Handle]) -> None:
        self.all_devices = handles


class _Client:
    """Duck-typed stand-in for ``MammotionClient`` (the calls ``port.py`` makes)."""

    def __init__(self, handles: list[_Handle] | None = None) -> None:
        self.handles = handles if handles is not None else [_Handle()]
        self.device_registry = _Registry(self.handles)
        self.reauth_required: str | None = None
        self.calls: list[tuple[Any, ...]] = []
        self.on_credentials_updated: Callable[[], Awaitable[None]] | None = None
        self.cache: dict[str, Any] = {"mammotion_data": {"token": "t"}}
        self.saga_steps = 0
        self.stopped = False
        self.fail_refresh = False
        self.fail_fresh = False
        self.wait_result: Exception | None = None

    async def login_and_initiate_cloud(self, account: str, password: str) -> None:
        self.calls.append(("login", account))

    async def restore_credentials(
        self, account: str, password: str, cached: dict[str, Any]
    ) -> None:
        self.calls.append(("restore", account, cached))

    def to_cache(self) -> dict[str, Any]:
        return self.cache

    async def stop(self) -> None:
        self.stopped = True

    async def send_command_with_args(self, name: str, key: str, **kwargs: Any) -> None:
        self.calls.append(("send", name, key, kwargs))

    async def send_command_and_wait(self, name: str, key: str, field: str, **kwargs: Any) -> Any:
        self.calls.append(("send_and_wait", name, key, field, kwargs))
        return "reply"

    async def wait_for(
        self,
        name: str,
        predicate: Callable[[object], bool],
        *,
        timeout: float,  # noqa: ASYNC109 - mirrors MammotionClient.wait_for
    ) -> Any:
        if self.wait_result is not None:
            raise self.wait_result
        if not predicate(self.handles[0].snapshot.raw):
            raise TimeoutError
        return self.handles[0].snapshot.raw

    async def refresh_status(self, name: str) -> None:
        self.calls.append(("refresh", name))
        if self.fail_refresh:
            raise NoTransportAvailableError("down")

    async def ensure_fresh_state(self, name: str, *, max_age_s: float) -> None:
        self.calls.append(("fresh", name, max_age_s))
        if self.fail_fresh:
            raise NoTransportAvailableError("down")

    async def start_map_sync(self, name: str) -> None:
        self.calls.append(("map", name))
        self.handles[0].queue.is_saga_active = True

    async def add_ble_only_device(self, **kwargs: Any) -> _Handle:
        self.calls.append(("ble_only", kwargs["device_name"], kwargs["self_managed_scanning"]))
        handle = self.handles[0]
        handle.device_name = kwargs["device_name"]
        return handle


def _port(client: _Client) -> ClientPort:
    async def opener() -> Any:
        return client, client.handles[0]

    return ClientPort(opener)


async def test_mammotion_client_port_delegates_to_pymammotion() -> None:
    client = _Client()
    p = _port(client)
    assert not p.is_open and not p.link_up and not p.has_report and not p.saga_active
    with pytest.raises(PortClosedError):
        p.device()
    with pytest.raises(PortClosedError):
        await p.send("start_job", priority=Priority.USER)
    await p.open()
    await p.open()  # idempotent
    assert p.is_open and p.link_up and p.has_report and p.ble_connected()
    assert p.device_name == "Luba-VP1"
    assert isinstance(p.device(), MowerDevice)
    assert (p.firmware(), p.product_key()) == ("1.30.29.26", "a1nf9kRBWoH")
    await p.send("pause_execute_task", priority=Priority.EMERGENCY)
    reply = await p.send_and_wait(
        "generate_route_information", "bidire_reqconver_path", priority=Priority.USER, timeout_s=4
    )
    assert reply == "reply"
    assert client.calls[0] == (
        "send",
        "Luba-VP1",
        "pause_execute_task",
        {"priority": Priority.EMERGENCY},
    )
    assert client.calls[1][4] == {"send_timeout": 4, "priority": Priority.USER}
    assert await p.wait_for(lambda d: d.report_data.dev.battery_val == 87, 1)
    assert not await p.wait_for(lambda d: d.report_data.dev.battery_val == 1, 1)
    await p.refresh()
    client.fail_refresh = True
    await p.refresh()  # logged only
    await p.ensure_fresh(10)
    assert client.calls[-1] == ("fresh", "Luba-VP1", 10)
    client.fail_fresh = True
    await p.ensure_fresh(10)  # logged at debug only
    client.reauth_required = "token rejected"
    assert not p.is_open
    seen: list[str] = []

    async def listener() -> None:
        seen.append("update")

    unsubscribe = p.subscribe(listener)
    await client.handles[0].listeners[0](object())
    unsubscribe()
    assert seen == ["update"] and client.handles[0].subscriptions[0].cancelled
    await p.close()
    await p.close()
    assert client.stopped and not p.is_open


async def test_mammotion_client_port_rejects_a_non_mower_and_survives_stop_errors() -> None:
    client = _Client([_Handle(raw=object())])

    async def failing_stop() -> None:
        raise RuntimeError("boom")

    client.stop = failing_stop  # type: ignore[method-assign]
    closed: list[bool] = []

    async def on_close() -> None:
        closed.append(True)

    async def opener() -> Any:
        return client, client.handles[0]

    p = ClientPort(opener, on_close=on_close)
    await p.open()
    with pytest.raises(TypeError, match="not a mower"):
        p.device()
    await p.close()  # the stop error is logged
    assert closed == [True]


async def test_mammotion_client_port_map_sync_waits_for_the_saga(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    p = _port(client)
    await p.open()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            client.handles[0].queue.is_saga_active = False

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    await p.sync_map(60)
    assert ("map", "Luba-VP1") in client.calls
    assert sleeps == [0.5, 0.5]
    client.handles[0].queue.is_saga_active = True

    async def never_done(_seconds: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", never_done)
    with pytest.raises(TimeoutError, match="did not finish"):
        await p.sync_map(-1)


def test_mammotion_select_handle_by_name_or_iot_id() -> None:
    a, b = _Handle("Luba-VPA", "iot-a"), _Handle("Luba-VPB", "iot-b")
    client: Any = _Client([a, b])
    found: Any = port.select_handle(client, device_name="Luba-VPB", iot_id=None)
    assert found is b
    found = port.select_handle(client, device_name=None, iot_id="iot-a")
    assert found is a
    with pytest.raises(LookupError, match="Luba-VPA, Luba-VPB"):
        port.select_handle(client, device_name="Luba-X", iot_id=None)
    with pytest.raises(LookupError, match="iot_id nope"):
        port.select_handle(client, device_name=None, iot_id="nope")


async def test_mammotion_login_uses_and_refreshes_the_credential_cache(tmp_path: Path) -> None:
    cache = tmp_path / "state" / "mammotion.json"
    client: Any = _Client()
    await port.login_cloud(client, "second@example.com", "pw", cache)
    assert client.calls == [("login", "second@example.com")]
    assert json.loads(cache.read_text("utf-8")) == {"mammotion_data": {"token": "t"}}
    again: Any = _Client()
    again.cache = {"mammotion_data": {"token": "rotated"}, "obj": sample_device().location.RTK}
    await port.login_cloud(again, "second@example.com", "pw", cache)
    assert again.calls[0][0] == "restore"
    assert again.on_credentials_updated is not None
    await again.on_credentials_updated()
    stored = json.loads(cache.read_text("utf-8"))
    assert stored["mammotion_data"] == {"token": "rotated"}
    assert stored["obj"]["latitude"] != 0  # data classes are stored through to_dict
    cache.write_text("not json", "utf-8")
    third: Any = _Client()
    await port.login_cloud(third, "a", "pw", cache)
    assert third.calls[0][0] == "login"
    fourth: Any = _Client()
    fourth.cache = {}
    await port.login_cloud(fourth, "a", "pw", None)
    assert port._jsonable(3.5) == "3.5"


async def test_mammotion_open_cloud_client(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[_Client] = []

    def factory() -> _Client:
        client = _Client([_Handle("Luba-VP1", "iot-1")])
        created.append(client)
        return client

    monkeypatch.setattr(port, "MammotionClient", factory)
    handle: Any
    _client, handle = await port.open_cloud_client(
        account="a",
        password=PASSWORD,
        device_name=None,
        iot_id="iot-1",
        cache_path=None,
        settle_seconds=0,
    )
    assert handle.device_name == "Luba-VP1"
    assert ("refresh", "Luba-VP1") in created[0].calls
    with pytest.raises(LookupError):
        await port.open_cloud_client(
            account="a", password=PASSWORD, device_name="x", iot_id=None, cache_path=None
        )
    assert created[-1].stopped  # a failed open stops the client again

    def rtk_factory() -> _Client:
        client = _Client([_Handle("RTK1", "iot-2", raw=object())])
        created.append(client)
        return client

    monkeypatch.setattr(port, "MammotionClient", rtk_factory)
    with pytest.raises(LookupError, match="not a mower"):
        await port.open_cloud_client(
            account="a", password=PASSWORD, device_name="RTK1", iot_id=None, cache_path=None
        )


async def test_mammotion_open_ble_client(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[_Client] = []

    def factory() -> _Client:
        client = _Client()
        created.append(client)
        return client

    monkeypatch.setattr(port, "MammotionClient", factory)
    handle: Any
    _client, handle = await port.open_ble_client(device_name="Luba-VPBLE", ble_device=object())
    assert handle.started and handle.transport is not None and handle.transport.connected
    assert created[0].calls == [("ble_only", "Luba-VPBLE", False)]

    def no_transport() -> _Client:
        client = _Client()
        client.handles[0].transport = None
        created.append(client)
        return client

    monkeypatch.setattr(port, "MammotionClient", no_transport)
    with pytest.raises(NoTransportAvailableError):
        await port.open_ble_client(device_name="Luba-VPBLE", ble_device=object())
    assert created[-1].stopped


# --- ESPHome proxy ------------------------------------------------------------------------


class _Adv:
    local_name = "Luba-VPADV"


class _BleDevice:
    def __init__(self, address: str) -> None:
        self.address = address
        self.name = None


class _Scanner:
    def __init__(self, devices: dict[str, tuple[_BleDevice, _Adv]]) -> None:
        self.discovered_devices_and_advertisement_data = devices
        self.torn_down = False

    def async_setup(self) -> Callable[[], None]:
        def teardown() -> None:
            self.torn_down = True

        return teardown


class _Manager:
    def __init__(self) -> None:
        self.registered: list[_Scanner] = []

    def async_register_scanner(self, scanner: _Scanner) -> Callable[[], None]:
        self.registered.append(scanner)

        def unregister() -> None:
            self.registered.remove(scanner)

        return unregister


class _Info:
    name = "mowdeck-bridge"


class _Api:
    instances: ClassVar[list["_Api"]] = []

    def __init__(self, *, address: str, port: int, password: str | None, noise_psk: str) -> None:
        self.args = (address, port, password, noise_psk)
        self.disconnected = False
        self.on_stop: Callable[[bool], Awaitable[None]] | None = None
        _Api.instances.append(self)

    async def connect(self, *, on_stop: Callable[[bool], Awaitable[None]], login: bool) -> None:
        assert login
        self.on_stop = on_stop

    async def device_info(self) -> _Info:
        return _Info()

    async def disconnect(self) -> None:
        self.disconnected = True


class _ClientData:
    def __init__(self, scanner: _Scanner) -> None:
        self.scanner = scanner


def _patch_proxy(monkeypatch: pytest.MonkeyPatch, scanner: _Scanner) -> _Manager:
    manager = _Manager()

    async def shared() -> _Manager:
        return manager

    monkeypatch.setattr(esphome, "_shared_manager", shared)
    monkeypatch.setattr("aioesphomeapi.client.APIClient", _Api)
    monkeypatch.setattr(
        "bleak_esphome.connect_scanner", lambda api, info, available: _ClientData(scanner)
    )
    return manager


async def test_mammotion_esphome_proxy_finds_the_mower(monkeypatch: pytest.MonkeyPatch) -> None:
    mower = _BleDevice("aa:bb:cc:dd:ee:ff")
    scanner = _Scanner({"x": (_BleDevice("11:22:33:44:55:66"), _Adv()), "m": (mower, _Adv())})
    manager = _patch_proxy(monkeypatch, scanner)
    link = await esphome.open_proxy(
        host="bridge.local",
        port=6053,
        noise_psk="psk",
        ble_address="AA:BB:CC:DD:EE:FF",
        scan_timeout_s=1,
    )
    api = _Api.instances[-1]
    assert api.args == ("bridge.local", 6053, None, "psk")
    assert link.ble_device is mower and link.advertised_name == "Luba-VPADV"
    assert link.proxy_name == "mowdeck-bridge" and link.is_alive()
    assert manager.registered == [scanner]
    assert api.on_stop is not None
    await api.on_stop(False)
    assert not link.is_alive()
    await link.close()
    assert api.disconnected and scanner.torn_down and manager.registered == []


async def test_mammotion_esphome_proxy_times_out_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = _Scanner({})
    manager = _patch_proxy(monkeypatch, scanner)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", no_sleep)
    with pytest.raises(TimeoutError, match="not seen by proxy"):
        await esphome.open_proxy(
            host="h",
            port=6053,
            noise_psk="psk",
            ble_address="AA:BB:CC:DD:EE:FF",
            scan_timeout_s=0.01,
        )
    assert _Api.instances[-1].disconnected and scanner.torn_down and manager.registered == []


async def test_mammotion_esphome_close_tolerates_disconnect_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = _Scanner({"m": (_BleDevice("AA:BB:CC:DD:EE:FF"), _Adv())})
    _patch_proxy(monkeypatch, scanner)
    link = await esphome.open_proxy(
        host="h", port=1, noise_psk="psk", ble_address="aa:bb:cc:dd:ee:ff", scan_timeout_s=1
    )

    async def broken() -> None:
        raise OSError("socket gone")

    _Api.instances[-1].disconnect = broken  # type: ignore[method-assign]
    await link.close()  # logged, not raised


async def test_mammotion_esphome_shared_manager_is_created_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[Any] = []

    class _BaseManager:
        def __init__(self, adapters: object, slot_manager: object) -> None:
            created.append(self)

        async def async_setup(self) -> None:
            return None

    installed: list[Any] = []
    monkeypatch.setattr("habluetooth.BluetoothManager", _BaseManager)
    monkeypatch.setattr("habluetooth.set_manager", installed.append)
    monkeypatch.setattr("bluetooth_adapters.get_adapters", lambda: object())
    monkeypatch.setattr(esphome, "_manager", None)
    first = await esphome._shared_manager()
    second = await esphome._shared_manager()
    assert first is second and len(created) == 1 and installed == [first]
    first._discover_service_info(object())  # discovery is a no-op without Home Assistant


class _Response:
    def __init__(self, data: object) -> None:
        self.data = data


class _Http:
    def __init__(self, video: object) -> None:
        self.video = video
        self.asked: list[str] = []

    async def get_video_resource(self, iot_id: str) -> _Response:
        self.asked.append(iot_id)
        return _Response(self.video)


class _CameraClient(_Client):
    def __init__(self, stream: object, video: object, http: bool = True) -> None:
        super().__init__()
        self.stream = stream
        self.mammotion_http = _Http(video) if http else None
        self.all_cameras: list[bool] = []

    async def get_stream_subscription(
        self, name: str, iot_id: str, *, all_cameras: bool = False
    ) -> _Response:
        self.calls.append(("stream", name, iot_id))
        self.all_cameras.append(all_cameras)
        return _Response(self.stream)

    async def stop_stream(self, name: str) -> None:
        self.calls.append(("stop_stream", name))


async def test_mammotion_client_port_fetches_stream_token_and_video_budget() -> None:
    stream = port.StreamSubscriptionResponse(
        appid="a",
        openEncrypt=0,
        cameras=[],
        channelName="c",
        areaCode="G",
        token="t",  # noqa: S106
        uid=1,
    )
    video = port.VideoResourceResponse(
        id="1",
        deviceId="iot-1",
        deviceName="Luba-VP1",
        cycleType=1,
        usageYearMonth="2026-10",
        totalTime=60,
        availableTime=30,
    )
    client = _CameraClient(stream, video)
    p = _port(client)
    await p.open()
    assert await p.stream_subscription() is stream
    assert await p.video_resource() is video
    assert ("stream", "Luba-VP1", "iot-1") in client.calls
    assert await p.stream_subscription(all_cameras=False) is stream
    # Every camera by default (cameraStates 1, 1, ...), the front left one only on request.
    assert client.all_cameras == [True, False]
    empty = _port(_CameraClient(None, "not a model"))
    await empty.open()
    assert await empty.stream_subscription() is None
    assert await empty.video_resource() is None
    ble_only = _port(_CameraClient(stream, video, http=False))
    await ble_only.open()
    assert await ble_only.stream_subscription() is None
    assert await ble_only.video_resource() is None


async def test_mammotion_client_port_stops_the_stream_of_its_device() -> None:
    client = _CameraClient(None, None)
    p = _port(client)
    with pytest.raises(port.PortClosedError):
        await p.stop_stream()
    await p.open()
    await p.stop_stream()
    await p.stop_stream()
    assert [c for c in client.calls if c[0] == "stop_stream"] == [
        ("stop_stream", "Luba-VP1"),
        ("stop_stream", "Luba-VP1"),
    ]


def _with_ext_mod(device: MowerDevice, value: str) -> MowerDevice:
    device.mqtt_properties = SimpleNamespace(  # type: ignore[assignment]
        params=SimpleNamespace(items=SimpleNamespace(extMod=SimpleNamespace(value=value)))
    )
    return device


def test_mammotion_firmware_precedence() -> None:
    device = sample_device()
    device.mower_state.swversion = "3.0.0.0"
    device.device_firmwares.device_version = "1.1.1.1"
    assert port.firmware_of(device, "2.0.0.0") == "1.1.1.1"
    device.device_firmwares.device_version = ""
    assert port.firmware_of(device, "2.0.0.0") == "2.0.0.0"
    assert port.firmware_of(device, "1.0.0.0") == "3.0.0.0"  # placeholder
    assert port.firmware_of(device, "") == "3.0.0.0"
    device.mower_state.swversion = ""
    assert port.firmware_of(device, "1.0.0.0") == ""


def test_mammotion_model_precedence() -> None:
    device = sample_device(sub_model_id="", product_key="")
    assert port.model_of(device, "Luba-VP1") == "Luba VP"  # device type from the name
    assert port.model_of(device, "Luba-VP1", "a1nf9kRBWoH") == "LubaAWD3000723"  # table key
    device.mower_state.model_id = "LUBA-2-AWD-X"
    assert port.model_of(device, "Luba-VP1", "a1nf9kRBWoH") == "LUBA-2-AWD-X"
    _with_ext_mod(device, "LubaAWD1000723")
    assert port.model_of(device, "Luba-VP1") == "LubaAWD1000723"
    device.mower_state.sub_model_id = "HM030070LB2PAWD50OMNI"
    assert port.model_of(device, "Luba-VP1") == "LUBA 2 AWD 5000X"  # exact model wins
    device.mower_state.sub_model_id = "no-such-sub-model"
    assert port.model_of(device, "Luba-VP1") == "LubaAWD1000723"


def test_mammotion_model_config_lookup_order() -> None:
    device = sample_device(sub_model_id="", product_key="")
    assert port.model_config_of(device) is None
    assert port.model_config_of(device, "a1iMygIwxFC") is None  # Luba VP: not in the table
    by_key = port.model_config_of(device, "a1nf9kRBWoH")
    assert by_key is not None and by_key["extMod"] == "LubaAWD3000723"
    device.mower_state.product_key = "a1nf9kRBWoH"
    assert port.model_config_of(device) == by_key
    device.mower_state.sub_model_id = "HM030070LB2PAWD50OMNI"
    by_sub = port.model_config_of(device, "a1nf9kRBWoH")
    assert by_sub is not None and by_sub["extMod"] == "LUBA 2 AWD 5000X"


async def test_mammotion_client_port_firmware_and_model_read_the_device() -> None:
    client = _Client()
    p = _port(client)
    await p.open()
    device = client.handles[0].snapshot.raw
    assert isinstance(device, MowerDevice)
    device.device_firmwares.device_version = "1.30.30.1"
    device.mower_state.sub_model_id = ""
    _with_ext_mod(device, "LubaAWD1000723")
    assert (p.firmware(), p.model()) == ("1.30.30.1", "LubaAWD1000723")
    device.mower_state.sub_model_id = "HM030070LB2PAWD50OMNI"
    assert p.model() == "LUBA 2 AWD 5000X"


@pytest.mark.parametrize(
    ("code", "ok"), [(0, True), (200, True), (None, True), (500, False), (401, False), (1, False)]
)
def test_mammotion_vendor_call_success_codes(code: object, ok: bool) -> None:
    """The cloud answers 0 for success; PyMammotion's own 200 and a missing code count too."""
    assert vendor_call_succeeded(code) is ok


class _OtaHttp:
    def __init__(self, code: int) -> None:
        self.code = code
        self.requests: list[tuple[str, str]] = []

    async def start_ota_upgrade(self, iot_id: str, version: str) -> Any:
        self.requests.append((iot_id, version))
        return SimpleNamespace(code=self.code, msg="", data=None)


@pytest.mark.parametrize("code", [0, 200])
async def test_mammotion_firmware_update_accepts_the_cloud_success_code(code: int) -> None:
    client = _Client()
    http = _OtaHttp(code)
    client.mammotion_http = http  # type: ignore[attr-defined]
    p = _port(client)
    await p.open()
    await p.start_firmware_update("1.30.31.19")
    assert http.requests == [("iot-1", "1.30.31.19")]


async def test_mammotion_firmware_update_refused_on_an_error_code() -> None:
    client = _Client()
    client.mammotion_http = _OtaHttp(500)  # type: ignore[attr-defined]
    p = _port(client)
    await p.open()
    with pytest.raises(FirmwareRefused, match="code 500"):
        await p.start_firmware_update("1.30.31.19")
