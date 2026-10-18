"""The PyMammotion surface: the only module of this package that imports ``pymammotion``.

Everything the adapter needs from PyMammotion 0.10.9 goes through :class:`MammotionPort`, a
small async protocol in MowDeck's own terms. :class:`ClientPort` implements it on a
``pymammotion.client.MammotionClient`` and one ``DeviceHandle``; tests and the owner script's
``--dry-run`` use :class:`~mowdeck_control_mammotion.fake.FakeMammotionPort` instead.

The PyMammotion data classes (``MowerDevice``, ``HashList``, enums) are re-exported from here,
so the mapping code reads the real structures and a renamed field breaks the tests.

PyMammotion API names used (file references are relative to the installed package):

* ``client.MammotionClient``: ``login_and_initiate_cloud``, ``restore_credentials``,
  ``to_cache``, ``on_credentials_updated``, ``reauth_required`` (``client_auth.py``);
  ``add_ble_only_device``, ``send_command_with_args``, ``send_command_and_wait``,
  ``start_map_sync``, ``refresh_status``, ``ensure_fresh_state``, ``start_report_stream``,
  ``mower``,
  ``device_registry``, ``stop``,
  ``get_stream_subscription``, ``mammotion_http`` (``client.py``).
* ``http.http.MammotionHTTP.get_video_resource`` and the models ``StreamSubscriptionResponse``,
  ``VideoResourceResponse`` (``http/model/camera_stream.py``); ``utility.agora_token``
  (``decode_agora_token``, claims only, never the token text).
* Firmware (ADR 0038): ``MammotionHTTP.get_device_ota_firmware([iot_id])`` (``POST
  /device-server/v1/devices/version/check``, model ``CheckDeviceVersion`` with
  ``product_version_info_vo``) and ``MammotionHTTP.start_ota_upgrade(iot_id, version)`` (``POST
  /device-server/v1/ota/device/upgrade``); ``MowerDevice.apply_version_check``,
  ``clear_version_info`` and ``update_check`` (progress from the MQTT property ``otaProgress``
  and the BLE ``toapp_upgrade_report``, see ``Device.apply_ota_progress``);
  ``events.ota_progress`` (``OTAProgress`` with ``is_complete`` and ``is_failed``).
* Dual link (ADR 0038): ``MammotionClient.add_ble_to_device(device_name, ble_device,
  account_id, rssi)`` attaches a BLE transport to the registered cloud device,
  ``connect_ble(device_name, account_id)`` opens it, ``DeviceHandle.set_prefer_ble`` prefers it,
  ``disconnect_transport(TransportType.BLE)`` drops it (``client.py``, ``ble_inventory.py``).
* ``device.handle.DeviceHandle``: ``snapshot.raw``, ``subscribe_state_changed``,
  ``has_usable_transport``, ``is_transport_connected``, ``get_transport``, ``queue.is_saga_active``,
  ``last_report_data_at``, ``firmware_version`` (fallback), ``snapshot.raw.device_firmwares``,
  ``mqtt_properties.params.items.extMod``, ``product_key``, ``iot_id``, ``account_id``,
  ``wait_for``.
* ``messaging.mow_path_saga.MowPathSaga`` (the planned route: ``generate_route_information``
  unless ``skip_planning``, the line hash list sub_cmd 3, the ``cover_path_upload`` frames into
  ``HashList.current_mow_path``), enqueued with ``DeviceHandle.enqueue_saga`` like
  ``MammotionClient.start_mow_path_saga`` does, gated like it by
  ``DeviceHandle.mow_path_fetch_enabled``; ``GenerateRouteInformation.from_current_task_settings``.
* ``data.model.coordinates.CoordinateConverter.enu_to_lla`` (the dock position in WGS84, the
  same forward transform as ``MowerDevice.update_report_data``).
* ``messaging.command_queue.Priority`` (``EMERGENCY``, ``USER``).
* ``transport.base``: ``TransportType``, ``NoTransportAvailableError``, ``CommandTimeoutError``,
  ``TransportError``, ``LoginFailedError``.
* Command builder names (``mammotion/commands/messages/navigation.py`` and ``driver.py``):
  ``generate_route_information``, ``start_job``, ``pause_execute_task``,
  ``resume_execute_task``, ``return_to_dock``, ``cancel_job``, ``send_movement``,
  ``operate_on_device``, ``set_blade_height``, ``set_speed``; ``read_write_device``
  (``mammotion_command.py``, ``rw_id=5, context=1, rw=1``: the RTK origin and dock buffer);
  ``set_car_manual_light`` and ``get_car_light`` (``mammotion/commands/messages/media.py``, the
  camera's headlamp; the answer ``get_lamp_rsp`` lands in ``mower_state.lamp_info``,
  ``data.model.device_info.LampInfo``).
"""

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, Protocol, runtime_checkable

from pymammotion.client import MammotionClient
from pymammotion.data.error_codes import bundled_error_codes
from pymammotion.data.error_codes import describe as describe_error
from pymammotion.data.error_codes import get_error_info as error_info
from pymammotion.data.model.coordinates import CoordinateConverter
from pymammotion.data.model.device import MowerDevice, create_device
from pymammotion.data.model.device_capabilities import DeviceConfig
from pymammotion.data.model.device_config import OperationSettings, build_route_information
from pymammotion.data.model.device_info import LampInfo
from pymammotion.data.model.enums import RTKStatus
from pymammotion.data.model.events import (
    OTA_RESULT_FAILED,
    OTA_RESULT_IN_PROGRESS,
    OTA_RESULT_SUCCESS,
    OTAProgress,
)
from pymammotion.data.model.generate_route_information import GenerateRouteInformation
from pymammotion.data.model.hash_list import (
    AreaHashNameList,
    CommDataCouple,
    FrameList,
    HashList,
    MowPathPacket,
    NavGetCommData,
    NavGetHashListData,
    PathType,
    RootHashList,
)
from pymammotion.data.model.hash_list import MowPath as MowPathFrame
from pymammotion.data.model.location import Dock, LocationPoint
from pymammotion.data.model.mowing_modes import CuttingMode, PathAngleSetting
from pymammotion.data.model.report_info import LocationData
from pymammotion.device.handle import DeviceHandle
from pymammotion.http.model.camera_stream import (
    Camera,
    StreamSubscriptionResponse,
    VideoResourceResponse,
)
from pymammotion.http.model.http import CheckDeviceVersion, ProductVersionInfo
from pymammotion.messaging.command_queue import Priority
from pymammotion.messaging.mow_path_saga import MowPathSaga
from pymammotion.proto import DrvUpgradeReport, RptAct, RptInfoType, SystemUpdateBufMsg
from pymammotion.state.device_state import DeviceNotification
from pymammotion.transport.base import (
    AccountInUseError,
    AuthError,
    CommandTimeoutError,
    LoginFailedError,
    NoTransportAvailableError,
    TransportError,
    TransportType,
)
from pymammotion.utility.agora_token import AgoraTokenClaims, decode_agora_token
from pymammotion.utility.constant.device_enums import WorkMode
from pymammotion.utility.device_type import DeviceType

__all__ = [
    "MOW_PATH_SAGA_CEILING_S",
    "OTA_RESULT_FAILED",
    "OTA_RESULT_IN_PROGRESS",
    "OTA_RESULT_SUCCESS",
    "AccountInUseError",
    "AgoraTokenClaims",
    "AreaHashNameList",
    "AuthError",
    "BridgeLink",
    "Camera",
    "CheckDeviceVersion",
    "ClientPort",
    "CommDataCouple",
    "CommandTimeoutError",
    "CoordinateConverter",
    "CuttingMode",
    "DeviceHandle",
    "DeviceType",
    "Dock",
    "DrvUpgradeReport",
    "FirmwareRefused",
    "FrameList",
    "GenerateRouteInformation",
    "HashList",
    "LampInfo",
    "LocationData",
    "LocationPoint",
    "LoginFailedError",
    "MammotionClient",
    "MammotionPort",
    "MowPathFetchError",
    "MowPathFrame",
    "MowPathPacket",
    "MowerDevice",
    "NavGetCommData",
    "NavGetHashListData",
    "NoTransportAvailableError",
    "NotificationListener",
    "OTAProgress",
    "OpenedClient",
    "OperationSettings",
    "PathAngleSetting",
    "PathType",
    "PortClosedError",
    "Priority",
    "ProductVersionInfo",
    "RTKStatus",
    "RootHashList",
    "StateListener",
    "StreamSubscriptionResponse",
    "SystemUpdateBufMsg",
    "TrackedMowPathSaga",
    "TransportError",
    "TransportType",
    "VideoResourceResponse",
    "WorkMode",
    "build_route_information",
    "bundled_error_codes",
    "create_device",
    "decode_agora_token",
    "describe_error",
    "error_info",
    "firmware_of",
    "login_cloud",
    "login_cloud_client",
    "model_config_of",
    "model_of",
    "select_handle",
]

_log = logging.getLogger("mowdeck_control_mammotion.port")

StateListener = Callable[[], Awaitable[None]]
"""Called (without arguments) whenever PyMammotion publishes a new device snapshot."""
NotificationListener = Callable[[str, dict[str, Any] | None], Awaitable[None]]
"""Called with the identifier and value of every ``thing/event`` post (PyMammotion's
``DeviceNotification``): ``device_warning_event``, ``device_warning_code_event``, ..."""

_SAGA_START_GRACE_S = 3.0
_SAGA_POLL_S = 0.5


FIRMWARE_PLACEHOLDER = "1.0.0.0"
"""What PyMammotion's update-check frame holds before the device reported its version."""


def firmware_of(device: object, handle_version: object = "") -> str:
    """Main firmware like Mammotion-HA's ``sw_version``, empty when unknown.

    Order: ``device_firmwares.device_version`` (filled by the device information request),
    the handle's ``firmware_version`` unless empty or the ``1.0.0.0`` placeholder, then
    ``mower_state.swversion``.
    """
    firmwares = getattr(device, "device_firmwares", None)
    candidates = (
        getattr(firmwares, "device_version", ""),
        handle_version,
        getattr(getattr(device, "mower_state", None), "swversion", ""),
    )
    for candidate in candidates:
        version = str(candidate or "").strip()
        if version and version != FIRMWARE_PLACEHOLDER:
            return version
    return ""


_DEVICE_CONFIG = DeviceConfig()


def model_config_of(device: object, product_key: str = "") -> dict[str, Any] | None:
    """The entry of PyMammotion's per-model table (``device_capabilities.py``), None if unknown.

    Looked up like ``MowerDevice.device_limits``: ``mower_state.sub_model_id`` (the exact
    model, answer to ``get_device_product_model``) first, then the product key (``product_key``
    or ``mower_state.product_key``). Unlike ``device_limits`` there is no family default: the
    Luba VP product keys are not in the table and its default is the Yuka's, so an unknown model
    stays unknown. The entry has ``extMod``, ``blade_height_min/max``, ``cutting_speed_min/max``
    (speed while mowing, absent for product-key entries), ``working_speed_min/max`` and
    ``path_spacing_min/max``.
    """
    state = getattr(device, "mower_state", None)
    keys = (
        str(getattr(state, "sub_model_id", "") or "").strip(),
        str(product_key or "").strip(),
        str(getattr(state, "product_key", "") or "").strip(),
    )
    for key in keys:
        if key and (config := _DEVICE_CONFIG.get_device_config(key)) is not None:
            return config
    return None


def model_of(device: object, device_name: str, product_key: str = "") -> str:
    """Model, most specific first: ``extMod`` of the table entry of the sub model (for example
    ``LUBA 2 AWD 5000X``), the ``extMod`` property of the cloud, ``mower_state.model_id``, the
    ``extMod`` of the product key's table entry, else the device type name derived from the
    device name (for example ``Luba VP``)."""
    state = getattr(device, "mower_state", None)
    sub_model = str(getattr(state, "sub_model_id", "") or "").strip()
    if sub_model:
        name = str(_DEVICE_CONFIG.get_external_model(sub_model) or "").strip()
        if name:
            return name
    params = getattr(getattr(device, "mqtt_properties", None), "params", None)
    ext_mod = getattr(params, "items", None)
    value = getattr(getattr(ext_mod, "extMod", None), "value", "")
    if value and str(value).strip():
        return str(value).strip()
    model_id = getattr(state, "model_id", "")
    if model_id and str(model_id).strip():
        return str(model_id).strip()
    config = model_config_of(device, product_key)
    name = str(config.get("extMod") or "").strip() if config else ""
    if name:
        return name
    return DeviceType.value_of_str(device_name, product_key or "").get_model()


class PortClosedError(TransportError):
    """The port has no open client (never opened, or closed)."""


def vendor_call_succeeded(code: object) -> bool:
    """Whether a Mammotion cloud response code means success.

    The cloud API answers ``0`` for success (PyMammotion checks ``code != 0`` everywhere).
    PyMammotion returns ``200`` itself when the answer was not JSON, and the HTTP status (never
    200) when the request failed. A missing code counts as success.
    """
    return code is None or code in (0, 200)


class FirmwareRefused(Exception):  # noqa: N818 - reads like the other outcomes
    """The vendor's firmware service refused the request (the message is safe to show)."""


@runtime_checkable
class BridgeLink(Protocol):
    """What :meth:`MammotionPort.attach_bridge` needs from an open Bluetooth bridge
    (:class:`~mowdeck_control_mammotion.esphome.ProxyLink` satisfies it)."""

    @property
    def ble_device(self) -> Any:
        """The bleak ``BLEDevice`` of the mower as the bridge's scanner saw it."""
        ...

    def rssi(self) -> int | None:
        """The mower's signal strength at the bridge (dBm), if known."""
        ...


class MowPathFetchError(Exception):
    """PyMammotion did not fetch the mow path: the fetch is switched off over MQTT, or the saga
    failed (the message names the reason, never secrets)."""


MOW_PATH_SAGA_CEILING_S = 90.0
"""Hard limit of one route fetch inside PyMammotion (``Saga.total_timeout``, default 300 s): an
abandoned fetch must not hold the device queue (map syncs, reads) for five minutes."""


class TrackedMowPathSaga(MowPathSaga):
    """PyMammotion's ``MowPathSaga`` that reports when it ended (also on failure) and the route
    hash the mower confirmed when it planned.

    ``MammotionClient.start_mow_path_saga`` only calls back on success; a caller that waits for
    the route needs the end either way.
    """

    total_timeout = MOW_PATH_SAGA_CEILING_S

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.finished = asyncio.Event()
        self.error: BaseException | None = None
        self.confirmed_hash: int | None = None

    async def execute(self, broker: Any) -> None:
        try:
            await super().execute(broker)
        except BaseException as exc:
            self.error = exc
            raise
        finally:
            self.finished.set()

    async def _plan_route(self, broker: Any) -> None:
        await super()._plan_route(broker)
        route = self._route_val
        if route is not None:
            self.confirmed_hash = int(getattr(route, "path_hash", 0) or 0)


@runtime_checkable
class MammotionPort(Protocol):
    """What the adapter needs from PyMammotion for one mower."""

    @property
    def device_name(self) -> str:
        """The device name PyMammotion uses as key, for example ``Luba-VP...``."""
        ...

    @property
    def is_open(self) -> bool:
        """True between :meth:`open` and :meth:`close` while the session is usable."""
        ...

    @property
    def link_up(self) -> bool:
        """True when a command sent now would find a usable transport."""
        ...

    @property
    def has_report(self) -> bool:
        """True once the device sent at least one telemetry report."""
        ...

    @property
    def saga_active(self) -> bool:
        """True while a multi-frame transfer (map sync) holds the device queue."""
        ...

    async def open(self) -> None:
        """Log in or connect and select the device. Raises on failure."""
        ...

    async def close(self) -> None:
        """Stop the client and every transport. Idempotent."""
        ...

    def device(self) -> MowerDevice:
        """The current device state (a PyMammotion ``MowerDevice``)."""
        ...

    def firmware(self) -> str:
        """Main firmware version, empty when unknown."""
        ...

    def model(self) -> str:
        """Model name or code (for example ``LubaAWD1000723``), empty when unknown."""
        ...

    def product_key(self) -> str:
        """Cloud product key, empty when unknown (BLE only)."""
        ...

    def subscribe(self, listener: StateListener) -> Callable[[], None]:
        """Call ``listener`` on every state change; returns the unsubscribe function."""
        ...

    def subscribe_notifications(self, listener: NotificationListener) -> Callable[[], None]:
        """Call ``listener(identifier, value)`` on every ``thing/event`` post of the mower
        (warning codes, information, ...); returns the unsubscribe function."""
        ...

    async def send(self, command: str, *, priority: Priority, **kwargs: Any) -> None:
        """Send the named ``MammotionCommand`` builder with ``kwargs``."""
        ...

    async def send_and_wait(
        self,
        command: str,
        expected_field: str,
        *,
        priority: Priority,
        timeout_s: float,
        **kwargs: Any,
    ) -> Any:
        """Send and wait for the response field; raises ``CommandTimeoutError``.

        PyMammotion re-sends the command on every attempt without a reply (broker
        ``send_and_wait``, ``retries=2``), so use it only for idempotent requests (reads, the
        route request), never for a command that changes what the mower does.
        """
        ...

    async def wait_for(self, predicate: Callable[[MowerDevice], bool], timeout_s: float) -> bool:
        """Wait until ``predicate`` holds for the device state; False on timeout."""
        ...

    async def refresh(self) -> None:
        """Ask the device for a fresh report (best effort)."""
        ...

    async def ensure_fresh(self, max_age_s: float) -> None:
        """Ask for a one-shot report when the last one is older than ``max_age_s`` seconds.

        Background priority (a running map sync or the offline flag may skip it), best effort:
        never raises for a failed send.
        """
        ...

    async def start_report_stream(self, duration_ms: int) -> None:
        """Open (or renew) PyMammotion's continuous report window of ``duration_ms``: while the
        mower is in an active work mode it pushes about one report per second until the window
        ends; a call while docked or idle falls back to a one-shot request. Best effort: never
        raises for a failed send."""
        ...

    async def sync_map(self, timeout_s: float) -> None:
        """Fetch the complete map into ``device().map``; raises ``TimeoutError``."""
        ...

    async def fetch_mow_path(
        self, route: GenerateRouteInformation | None, *, plan: bool, timeout_s: float
    ) -> int | None:
        """Run PyMammotion's ``MowPathSaga`` and wait until it ended: with ``plan`` it first
        sends ``route`` (``generate_route_information``) and waits for the mower's confirmation,
        without it the route the mower already planned is fetched (``skip_planning``; ``route``
        may then be None: the running job's settings are used). The route lands in
        ``device().map`` (line hash list and ``current_mow_path``).

        Returns the route hash the mower confirmed when planning (None without ``plan``).
        Raises :class:`MowPathFetchError` (switched off, saga failed) and ``TimeoutError``
        after ``timeout_s`` (the saga itself ends after :data:`MOW_PATH_SAGA_CEILING_S`).
        """
        ...

    async def stream_subscription(
        self, *, all_cameras: bool = True
    ) -> StreamSubscriptionResponse | None:
        """A fresh Agora stream token for the mower (this also makes the mower publish video);
        None when the cloud API is not available (BLE only) or answered without data.

        ``all_cameras`` switches every camera slot on (``cameraStates`` 1, 1 and the rear one
        where the model has it); the mower then publishes slot ``n`` under Agora uid ``n + 1``.
        Without it only the front left camera publishes (uid 1), as in the vendor app."""
        ...

    async def stop_stream(self) -> None:
        """Tell the mower to stop publishing video (PyMammotion's ``stop_stream``, Agora
        ``vi_switch=0``: ``device_agora_join_channel_with_position(enter_state=0)``). The mower
        keeps publishing after the last viewer left until it gets this. Harmless without a
        running stream. Raises on a failed send."""
        ...

    async def video_resource(self) -> VideoResourceResponse | None:
        """The 4G video budget of the mower; None when unknown."""
        ...

    def ble_connected(self) -> bool:
        """True while a BLE transport (through a bridge) is connected."""
        ...

    def ble_rssi(self) -> int | None:
        """The mower's signal strength at the attached bridge (dBm), None without one."""
        ...

    def report_received_at(self) -> float:
        """Monotonic time (``time.monotonic``) of the last telemetry report, 0 without one; it
        also advances for a report that changed nothing."""
        ...

    def route_bridge(self, drive_open: Callable[[], bool] | None) -> None:
        """Dual link (ADR 0038, link roles): route cloud first. With ``drive_open`` set, only
        the drive channel (:data:`BRIDGE_DRIVE_COMMANDS`) and, while ``drive_open()``, the
        report requests use a connected Bluetooth bridge; every other command, read and report
        request goes over the cloud (:func:`bridge_route`). None: PyMammotion's own choice."""
        ...

    async def attach_bridge(self, link: BridgeLink) -> None:
        """Attach the BLE transport of ``link`` to the mower's cloud device and connect it
        (ADR 0038). Raises when the Bluetooth connection fails."""
        ...

    async def detach_bridge(self) -> None:
        """Remove the BLE transport again (no role needs it, or the bridge is gone); everything
        goes over the cloud."""
        ...

    async def check_firmware(self) -> CheckDeviceVersion | None:
        """Ask the vendor cloud for the firmware state of the mower; None when the cloud API is
        not available (BLE only) or answered without data. Raises on a failed request."""
        ...

    async def start_firmware_update(self, version: str) -> None:
        """Ask the vendor cloud to install ``version``; raises :class:`FirmwareRefused`."""
        ...


BRIDGE_DRIVE_COMMANDS: Final = frozenset({"send_movement", "operate_on_device"})
"""What the drive channel sends (frames and blades): on a cloud-first port only these use a
connected Bluetooth bridge."""
REPORT_CHANNELS: Final = (
    RptInfoType.RIT_DEV_STA,
    RptInfoType.RIT_DEV_LOCAL,
    RptInfoType.RIT_WORK,
    RptInfoType.RIT_MAINTAIN,
    RptInfoType.RIT_BASESTATION_INFO,
    RptInfoType.RIT_VIO,
    RptInfoType.RIT_CONNECT,
    RptInfoType.RIT_RTK,
)
"""The report channels of a one-shot report request (PyMammotion's own set)."""


def bridge_route(
    command: str | None, *, cloud_first: bool, ble_connected: bool, drive_open: bool
) -> Literal["ble", "cloud"]:
    """Which link carries ``command`` (None: a report request).

    PyMammotion always picks a connected BLE transport. A cloud-first port (ADR 0038, link
    roles) keeps everything on the cloud but the drive channel and, while a drive session is
    open, its fast report polling.
    """
    if not ble_connected:
        return "cloud"
    if not cloud_first:
        return "ble"
    if command is None:
        return "ble" if drive_open else "cloud"
    return "ble" if command in BRIDGE_DRIVE_COMMANDS else "cloud"


def select_handle(
    client: MammotionClient, *, device_name: str | None, iot_id: str | None
) -> DeviceHandle:
    """Pick the mower by ``device_name`` (exact) or ``iot_id`` among the client's devices."""
    handles = list(client.device_registry.all_devices)
    for handle in handles:
        if device_name is not None and handle.device_name == device_name:
            return handle
        if iot_id is not None and handle.iot_id == iot_id:
            return handle
    names = ", ".join(sorted(h.device_name for h in handles)) or "none"
    wanted = device_name if device_name is not None else f"iot_id {iot_id}"
    msg = f"device {wanted} is not on this account (devices: {names})"
    raise LookupError(msg)


async def login_cloud(
    client: MammotionClient, account: str, password: str, cache_path: Path | None
) -> None:
    """Log in, preferring the PyMammotion credential cache at ``cache_path``.

    The cache avoids a password grant on every reconnect (ban risk R1 of spike 0001). It holds
    live tokens: keep it on a private volume.
    """

    async def save_cache() -> None:
        if cache_path is not None:
            _write_cache(cache_path, client.to_cache())

    client.on_credentials_updated = save_cache
    cached = _read_cache(cache_path) if cache_path is not None else None
    if cached:
        await client.restore_credentials(account, password, cached)
    else:
        await client.login_and_initiate_cloud(account, password)
    await save_cache()


def _read_cache(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _log.warning("ignoring unreadable Mammotion credential cache %s", path)
        return None
    return data if isinstance(data, dict) else None


def _write_cache(path: Path, cache: dict[str, Any]) -> None:
    if not cache:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, default=_jsonable), encoding="utf-8")


def _jsonable(value: Any) -> Any:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return str(value)


@dataclass(frozen=True, slots=True)
class OpenedClient:
    """What an opener of :class:`ClientPort` returns for a shared client.

    ``release`` gives the client back to its pool instead of stopping it when the port closes
    (the cloud session of a linked account is shared by its mowers, ADR 0034).
    """

    client: MammotionClient
    handle: DeviceHandle
    release: Callable[[], Awaitable[None]]


Opener = Callable[[], Awaitable[tuple[MammotionClient, DeviceHandle] | OpenedClient]]
"""Creates or borrows the client and returns the handle of the mower."""


class ClientPort:
    """:class:`MammotionPort` on a ``MammotionClient`` and one ``DeviceHandle``.

    ``opener`` creates or logs in the client and returns the handle of the mower; it is the
    only part that differs between the cloud and the BLE proxy path. An opener that returns an
    :class:`OpenedClient` borrows a shared client: :meth:`close` releases it instead of stopping
    it, so the other mowers of the account keep their session.
    """

    def __init__(
        self,
        opener: Opener,
        *,
        on_close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._opener = opener
        self._on_close = on_close
        self._client: MammotionClient | None = None
        self._handle: DeviceHandle | None = None
        self._release: Callable[[], Awaitable[None]] | None = None
        self._bridge: BridgeLink | None = None
        self._drive_open: Callable[[], bool] | None = None

    # --- state ----------------------------------------------------------------------------

    @property
    def device_name(self) -> str:
        return self._require_handle().device_name

    @property
    def is_open(self) -> bool:
        if self._client is None or self._handle is None:
            return False
        return self._client.reauth_required is None

    @property
    def link_up(self) -> bool:
        return self._handle is not None and self._handle.has_usable_transport

    @property
    def has_report(self) -> bool:
        return self._handle is not None and self._handle.last_report_data_at > 0

    @property
    def saga_active(self) -> bool:
        return self._handle is not None and self._handle.queue.is_saga_active

    def ble_connected(self) -> bool:
        """True while the handle's BLE transport is connected."""
        return self._handle is not None and self._handle.is_transport_connected(TransportType.BLE)

    def ble_rssi(self) -> int | None:
        return self._bridge.rssi() if self._bridge is not None else None

    def report_received_at(self) -> float:
        handle = self._handle
        return float(handle.last_report_data_at) if handle is not None else 0.0

    def route_bridge(self, drive_open: Callable[[], bool] | None) -> None:
        self._drive_open = drive_open

    def _pinned_to_cloud(self, command: str | None) -> bool:
        """True when PyMammotion would pick the bridge but the cloud must carry ``command``."""
        ble = self.ble_connected()
        route = bridge_route(
            command,
            cloud_first=self._drive_open is not None,
            ble_connected=ble,
            drive_open=self._drive_open is not None and self._drive_open(),
        )
        return ble and route == "cloud"

    async def _cloud_report(self, max_age_s: float) -> None:
        """A one-shot report request over the cloud when the last report is older than
        ``max_age_s`` (best effort, never raises)."""
        handle = self._require_handle()
        if handle.report_data_age <= max_age_s:
            return
        payload = handle.commands.request_iot_sys(
            rpt_act=RptAct.RPT_START,
            rpt_info_type=list(REPORT_CHANNELS),
            timeout=10_000,
            count=1,
        )
        try:
            await handle.send_cloud(payload)
        except Exception:
            _log.debug("cloud report request for %s failed", self.device_name, exc_info=True)

    def device(self) -> MowerDevice:
        raw = self._require_handle().snapshot.raw
        if not isinstance(raw, MowerDevice):
            msg = f"{self.device_name} is not a mower"
            raise TypeError(msg)
        return raw

    def firmware(self) -> str:
        handle = self._require_handle()
        return firmware_of(handle.snapshot.raw, handle.firmware_version)

    def model(self) -> str:
        handle = self._require_handle()
        return model_of(handle.snapshot.raw, handle.device_name, handle.product_key)

    def product_key(self) -> str:
        return str(self._require_handle().product_key or "")

    # --- lifecycle ------------------------------------------------------------------------

    async def open(self) -> None:
        if self._client is not None:
            return
        opened = await self._opener()
        if isinstance(opened, OpenedClient):
            self._client, self._handle, self._release = opened.client, opened.handle, opened.release
        else:
            self._client, self._handle = opened
            self._release = None

    async def close(self) -> None:
        client, self._client, self._handle = self._client, None, None
        release, self._release = self._release, None
        self._bridge = None
        try:
            if release is not None:
                try:
                    await release()
                except Exception:
                    _log.exception("releasing the shared PyMammotion client failed")
            elif client is not None:
                try:
                    await client.stop()
                except Exception:
                    _log.exception("stopping the PyMammotion client failed")
        finally:
            # Always, also when stopping the client was cancelled: the closer releases what the
            # opener set up beside the client (the ESPHome proxy of a Bluetooth bridge).
            if self._on_close is not None:
                await self._on_close()

    def subscribe(self, listener: StateListener) -> Callable[[], None]:
        handle = self._require_handle()

        async def on_snapshot(_snapshot: object) -> None:
            await listener()

        subscription = handle.subscribe_state_changed(on_snapshot)
        return subscription.cancel

    def subscribe_notifications(self, listener: NotificationListener) -> Callable[[], None]:
        handle = self._require_handle()

        async def on_notification(notification: DeviceNotification) -> None:
            await listener(notification.identifier, notification.value)

        subscription = handle.subscribe_notification(on_notification)
        return subscription.cancel

    # --- commands -------------------------------------------------------------------------

    async def send(self, command: str, *, priority: Priority, **kwargs: Any) -> None:
        client = self._require_client()
        if self._pinned_to_cloud(command):
            handle = self._require_handle()
            payload = getattr(handle.commands, command)(**kwargs)
            await handle.send_cloud(payload, user_initiated=priority.is_direct)
            return
        await client.send_command_with_args(self.device_name, command, priority=priority, **kwargs)

    async def send_and_wait(
        self,
        command: str,
        expected_field: str,
        *,
        priority: Priority,
        timeout_s: float,
        **kwargs: Any,
    ) -> Any:
        client = self._require_client()
        if self._pinned_to_cloud(command):
            handle = self._require_handle()
            payload = getattr(handle.commands, command)(**kwargs)

            async def send_over_cloud() -> None:
                await handle.send_cloud(payload, user_initiated=priority.is_direct)

            return await handle.broker.send_and_wait(
                send_fn=send_over_cloud, expected_field=expected_field, send_timeout=timeout_s
            )
        return await client.send_command_and_wait(
            self.device_name,
            command,
            expected_field,
            send_timeout=timeout_s,
            priority=priority,
            **kwargs,
        )

    async def wait_for(self, predicate: Callable[[MowerDevice], bool], timeout_s: float) -> bool:
        client = self._require_client()

        def holds(device: object) -> bool:
            return isinstance(device, MowerDevice) and predicate(device)

        try:
            await client.wait_for(self.device_name, holds, timeout=timeout_s)
        except TimeoutError:
            return False
        return True

    async def refresh(self) -> None:
        client = self._require_client()
        if self._pinned_to_cloud(None):
            await self._cloud_report(0.0)
            return
        try:
            await client.refresh_status(self.device_name)
        except Exception:
            _log.warning("status refresh for %s failed", self.device_name, exc_info=True)

    async def ensure_fresh(self, max_age_s: float) -> None:
        client = self._require_client()
        if self._pinned_to_cloud(None):
            await self._cloud_report(max_age_s)
            return
        try:
            # wait=False: a BACKGROUND queued count=1 report request, debounced by PyMammotion
            # (no send when a report arrived within the last 15 s or a BLE stream is live).
            await client.ensure_fresh_state(self.device_name, max_age_s=max_age_s)
        except Exception:
            _log.debug("fresh state request for %s failed", self.device_name, exc_info=True)

    async def start_report_stream(self, duration_ms: int) -> None:
        client = self._require_client()
        if self._pinned_to_cloud(None):
            # Bluetooth streams the positions while it is attached; the cloud keeps polling.
            await self._cloud_report(1.0)
            return
        try:
            # Active work mode: RPT_START (count=0, about 1 Hz) plus a stop timer, repeated calls
            # reset the window; docked or idle: PyMammotion falls back to a one-shot poll.
            await client.start_report_stream(self.device_name, duration_ms=duration_ms)
        except Exception:
            _log.debug("report stream request for %s failed", self.device_name, exc_info=True)

    async def sync_map(self, timeout_s: float) -> None:
        client = self._require_client()
        await client.start_map_sync(self.device_name)
        loop = asyncio.get_running_loop()
        started_by = loop.time() + _SAGA_START_GRACE_S
        deadline = loop.time() + timeout_s
        # PyMammotion exposes the saga only as a flag, so it is polled (as in spike step 3).
        while loop.time() < started_by and not self.saga_active:  # noqa: ASYNC110
            await asyncio.sleep(0.05)
        while self.saga_active:
            if loop.time() > deadline:
                msg = f"map sync of {self.device_name} did not finish within {timeout_s:.0f} s"
                raise TimeoutError(msg)
            await asyncio.sleep(_SAGA_POLL_S)

    async def fetch_mow_path(
        self, route: GenerateRouteInformation | None, *, plan: bool, timeout_s: float
    ) -> int | None:
        self._require_client()
        handle = self._require_handle()
        ble = handle.is_transport_connected(TransportType.BLE)
        if not handle.mow_path_fetch_enabled and not ble:
            msg = "the mow path fetch over the cloud is switched off in PyMammotion"
            raise MowPathFetchError(msg)
        if route is None:
            if plan:
                msg = "planning needs a route"
                raise ValueError(msg)
            route = GenerateRouteInformation.from_current_task_settings(self.device().work)
        saga = TrackedMowPathSaga(
            command_builder=handle.commands,
            send_command=handle.send_raw,
            get_map=lambda: self.device().map,
            zone_hashs=list(route.one_hashs),
            route_info=route,
            skip_planning=not plan,
            device_name=handle.device_name,
            sync_type=2 if ble else 3,  # as MammotionClient.start_mow_path_saga
        )
        await handle.enqueue_saga(saga)
        try:
            async with asyncio.timeout(timeout_s):
                await saga.finished.wait()
        except TimeoutError:
            msg = f"the mow path of {self.device_name} did not arrive within {timeout_s:.0f} s"
            raise TimeoutError(msg) from None
        if saga.error is not None:
            msg = f"the mow path fetch failed: {type(saga.error).__name__}"
            raise MowPathFetchError(msg) from saga.error
        if saga.failed:
            _log.warning("mow path of %s arrived incomplete", self.device_name)
        return saga.confirmed_hash if plan else None

    async def stream_subscription(
        self, *, all_cameras: bool = True
    ) -> StreamSubscriptionResponse | None:
        client = self._require_client()
        handle = self._require_handle()
        if client.mammotion_http is None or not handle.iot_id:
            return None
        response = await client.get_stream_subscription(
            handle.device_name, handle.iot_id, all_cameras=all_cameras
        )
        data = getattr(response, "data", None)
        return data if isinstance(data, StreamSubscriptionResponse) else None

    async def stop_stream(self) -> None:
        client = self._require_client()
        handle = self._require_handle()
        await client.stop_stream(handle.device_name)

    async def video_resource(self) -> VideoResourceResponse | None:
        client = self._require_client()
        handle = self._require_handle()
        http = client.mammotion_http
        if http is None or not handle.iot_id:
            return None
        response = await http.get_video_resource(handle.iot_id)
        data = getattr(response, "data", None)
        return data if isinstance(data, VideoResourceResponse) else None

    # --- dual link (ADR 0038) -------------------------------------------------------------

    async def attach_bridge(self, link: BridgeLink) -> None:
        client = self._require_client()
        handle = self._require_handle()
        account_id = getattr(handle, "account_id", None)
        await client.add_ble_to_device(handle.device_name, link.ble_device, account_id, link.rssi())
        await client.connect_ble(handle.device_name, account_id)
        if not handle.is_transport_connected(TransportType.BLE):
            msg = f"the Bluetooth link to {handle.device_name} did not come up"
            raise NoTransportAvailableError(msg)
        # No preference: with it PyMammotion reconnects Bluetooth on every send after a detach.
        # The routing is cloud first instead (bridge_route).
        handle.set_prefer_ble(value=False)
        self._bridge = link
        _log.info("BLE through the bridge attached to %s", handle.device_name)

    async def detach_bridge(self) -> None:
        self._bridge = None
        handle = self._handle
        if handle is None:
            return
        handle.set_prefer_ble(value=False)
        try:
            # Removed, not only disconnected: nothing reconnects it until a role asks again.
            await handle.remove_transport(TransportType.BLE)
        except Exception:  # the bridge is gone already; the cloud goes on
            _log.debug("dropping the BLE transport of %s failed", handle.device_name, exc_info=True)

    # --- firmware (ADR 0038) --------------------------------------------------------------

    async def check_firmware(self) -> CheckDeviceVersion | None:
        client = self._require_client()
        handle = self._require_handle()
        http = client.mammotion_http
        if http is None or not handle.iot_id:
            return None
        response = await http.get_device_ota_firmware([handle.iot_id])
        data = getattr(response, "data", None) or []
        checks = [c for c in data if isinstance(c, CheckDeviceVersion)]
        check = next((c for c in checks if c.device_id == handle.iot_id), None) or (
            checks[0] if checks else None
        )
        if check is None:
            code = getattr(response, "code", None)
            if not vendor_call_succeeded(code):
                msg = f"the firmware check failed with code {code}"
                raise FirmwareRefused(msg)
            return None
        self.device().apply_version_check(check)
        return check

    async def start_firmware_update(self, version: str) -> None:
        client = self._require_client()
        handle = self._require_handle()
        http = client.mammotion_http
        if http is None or not handle.iot_id:
            raise FirmwareRefused("firmware updates need the cloud link")
        self.device().clear_version_info()  # the version is fetched again after the update
        response = await http.start_ota_upgrade(handle.iot_id, version)
        code = getattr(response, "code", None)
        if not vendor_call_succeeded(code):
            msg = f"the firmware service refused the update (code {code})"
            raise FirmwareRefused(msg)
        _log.info("firmware update of %s to %s requested", handle.device_name, version)

    # --- helpers --------------------------------------------------------------------------

    def _require_client(self) -> MammotionClient:
        if self._client is None:
            raise PortClosedError("the PyMammotion client is not open")
        return self._client

    def _require_handle(self) -> DeviceHandle:
        if self._handle is None:
            raise PortClosedError("the PyMammotion client is not open")
        return self._handle


async def login_cloud_client(
    account: str, password: str, cache_path: Path | None
) -> MammotionClient:
    """A new ``MammotionClient`` logged in to the Mammotion cloud with every account device.

    The login function of the shared session pool (:mod:`.session`); stops the client again
    when the login fails.
    """
    client = MammotionClient()
    try:
        await login_cloud(client, account, password, cache_path)
    except BaseException:
        await client.stop()
        raise
    return client


async def open_cloud_client(
    *,
    account: str,
    password: str,
    device_name: str | None,
    iot_id: str | None,
    cache_path: Path | None,
    settle_seconds: float = 3.0,
) -> tuple[MammotionClient, DeviceHandle]:
    """Log in to the Mammotion cloud (MQTT transports) and select the mower."""
    client = MammotionClient()
    try:
        await login_cloud(client, account, password, cache_path)
        handle = select_handle(client, device_name=device_name, iot_id=iot_id)
        if not isinstance(handle.snapshot.raw, MowerDevice):
            msg = f"{handle.device_name} is not a mower"
            raise LookupError(msg)
        await asyncio.sleep(settle_seconds)  # let MQTT subscribe and the first report arrive
        with contextlib.suppress(Exception):
            await client.refresh_status(handle.device_name)
    except BaseException:
        await client.stop()
        raise
    return client, handle


async def open_ble_client(
    *, device_name: str, ble_device: Any, connect_timeout: float = 30.0
) -> tuple[MammotionClient, DeviceHandle]:
    """Register a BLE only device (no cloud login) on a bleak ``BLEDevice`` and connect it.

    ``ble_device`` comes from the ESPHome proxy scanner (see :mod:`.esphome`).
    """
    client = MammotionClient()
    try:
        handle = await client.add_ble_only_device(
            device_id=device_name,
            device_name=device_name,
            initial_device=MowerDevice(name=device_name),
            ble_device=ble_device,
            self_managed_scanning=False,
        )
        await handle.start()
        transport = handle.get_transport(TransportType.BLE)
        if transport is None:
            msg = "the device handle has no BLE transport"
            raise NoTransportAvailableError(msg)
        started = time.perf_counter()
        async with asyncio.timeout(connect_timeout):
            await transport.connect()
        _log.info("BLE connected to %s in %.2f s", device_name, time.perf_counter() - started)
    except BaseException:
        await client.stop()
        raise
    return client, handle
