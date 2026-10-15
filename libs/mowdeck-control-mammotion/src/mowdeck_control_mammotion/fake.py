"""An in-memory :class:`~mowdeck_control_mammotion.port.MammotionPort` for tests and dry runs.

:class:`FakeMammotionPort` holds a real PyMammotion ``MowerDevice`` (built by
:func:`sample_device`), records every command and, like a cooperative mower, moves the work mode
on the commands it understands (start, pause, resume, dock, cancel), so confirmations and the
StartMow sequence can be exercised without hardware. The owner script's ``--dry-run`` uses it.

The fake mower also plans a route: :data:`SAMPLE_PATH_LINES` (two border laps and stripes on the
front lawn). ``start_job`` sets the report's ``work.path_hash`` to it, and
:meth:`FakeMammotionPort.fetch_mow_path` writes its frames into ``device().map`` like
PyMammotion's ``MowPathSaga`` (:func:`install_mow_path`).
"""

import asyncio
import contextlib
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from mowdeck_control_mammotion.port import (
    OTA_RESULT_IN_PROGRESS,
    OTA_RESULT_SUCCESS,
    AreaHashNameList,
    BridgeLink,
    CheckDeviceVersion,
    CommandTimeoutError,
    CommDataCouple,
    DrvUpgradeReport,
    FrameList,
    GenerateRouteInformation,
    LampInfo,
    LocationData,
    MowerDevice,
    MowPathFrame,
    MowPathPacket,
    NavGetCommData,
    NavGetHashListData,
    NoTransportAvailableError,
    PathType,
    PortClosedError,
    Priority,
    ProductVersionInfo,
    RootHashList,
    StateListener,
    StreamSubscriptionResponse,
    SystemUpdateBufMsg,
    VideoResourceResponse,
    WorkMode,
    bridge_route,
    model_of,
)

SAMPLE_DEVICE_NAME: Final = "Luba-VPDRYRUN"
SAMPLE_PRODUCT_KEY: Final = "a1iMygIwxFC"  # a Luba VP (LUBA 2 AWD X) product key
SAMPLE_FIRMWARE: Final = "1.30.29.26"
SAMPLE_NEWER_FIRMWARE: Final = "1.30.31.19"
"""The release that restores movement over Wi-Fi: what the fake vendor offers."""
SAMPLE_IOT_ID: Final = "fakeiot0001"
SAMPLE_SUB_MODEL_ID: Final = "HM030070LB2PAWD50OMNI"
"""Sub model of a LUBA 2 AWD 5000X in PyMammotion's table: blade 25 to 70 mm, mowing speed
0.2 to 0.6 m/s. The Luba VP product key itself is not in the table."""
FRONT_HASH: Final = 1_000_000_000_000_000_001
BACK_HASH: Final = 1_000_000_000_000_000_002
NOGO_HASH: Final = 1_000_000_000_000_000_003
OBSTACLE_HASH: Final = 1_000_000_000_000_000_004
PATH_HASH: Final = 1_000_000_000_000_000_005
RTK_LAT_DEG: Final = 48.137154
RTK_LON_DEG: Final = 11.576124
RTK_YAW_DEG: Final = 12.5
DOCK_X: Final = -1.0
DOCK_Y: Final = 1.0
DOCK_ROTATION_DEG: Final = 0

PathLine = tuple[int, int, int, list[tuple[float, float]]]
"""One line of a planned route: ``(line_hash, path_type, zone_hash, points)``; ``path_type`` 0 is
the fill, 2 a border lap."""
EDGE_LINE_HASH: Final = 2_000_000_000_000_000_001
STRIPE_LINE_HASH: Final = 2_000_000_000_000_000_002
SAMPLE_PATH_LINES: Final[tuple[PathLine, ...]] = (
    (EDGE_LINE_HASH, 2, FRONT_HASH, [(0.2, 0.2), (11.8, 0.2), (11.8, 7.8), (0.2, 7.8), (0.2, 0.4)]),
    (
        STRIPE_LINE_HASH,
        0,
        FRONT_HASH,
        [(0.5, 0.6), (11.5, 0.6), (11.5, 1.1), (0.5, 1.1), (0.5, 1.6), (11.5, 1.6)],
    ),
)
"""The route the fake mower plans for a start: one border lap and three stripes (11 points)."""
_LINE_SUB_CMD: Final = 3


def install_mow_path(
    device: MowerDevice,
    lines: tuple[PathLine, ...] | list[PathLine],
    *,
    transaction_id: int = 1_700_000_000_000,
    packets_per_line: int = 1,
) -> None:
    """Store ``lines`` like PyMammotion's ``MowPathSaga``: the line hash list (sub_cmd 3, in
    route order) replaces the previous one, and one complete ``cover_path_upload`` transaction
    (one frame per line, ``packets_per_line`` packets each) lands in ``map.current_mow_path``."""
    hash_list = device.map
    hash_list.root_hash_lists = [r for r in hash_list.root_hash_lists if r.sub_cmd != _LINE_SUB_CMD]
    hash_list.root_hash_lists.append(
        RootHashList(
            total_frame=1,
            sub_cmd=_LINE_SUB_CMD,
            data=[
                NavGetHashListData(
                    sub_cmd=_LINE_SUB_CMD,
                    total_frame=1,
                    current_frame=1,
                    data_couple=[line[0] for line in lines],
                )
            ],
        )
    )
    frames: dict[int, MowPathFrame] = {}
    for index, (line_hash, path_type, zone_hash, points) in enumerate(lines, start=1):
        size = max(1, math.ceil(len(points) / packets_per_line))
        chunks = [points[i : i + size] for i in range(0, len(points), size)] or [[]]
        frames[index] = MowPathFrame(
            total_frame=len(lines),
            current_frame=index,
            transaction_id=transaction_id,
            path_packets=[
                MowPathPacket(
                    path_hash=line_hash,
                    path_type=path_type,
                    path_total=len(chunks),
                    path_cur=cur,
                    zone_hash=zone_hash,
                    data_couple=[CommDataCouple(x=x, y=y) for x, y in chunk],
                )
                for cur, chunk in enumerate(chunks, start=1)
            ],
        )
    hash_list.current_mow_path[transaction_id] = frames


def route_hash(lines: tuple[PathLine, ...] | list[PathLine]) -> int:
    """The ``work.path_hash`` a mower reports for ``lines`` (PyMammotion's
    ``HashList.computed_path_hash`` of the line list)."""
    scratch = MowerDevice(name="route")
    install_mow_path(scratch, lines)
    return int(scratch.map.computed_path_hash)


def dock_buffer(
    *,
    rtk_lat_deg: float = RTK_LAT_DEG,
    rtk_lon_deg: float = RTK_LON_DEG,
    rtk_yaw_deg: float = RTK_YAW_DEG,
    dock_x: float = DOCK_X,
    dock_y: float = DOCK_Y,
    rotation_deg: int = DOCK_ROTATION_DEG,
) -> SystemUpdateBufMsg:
    """The ``system_update_buf`` frame (type 1) a mower sends in answer to
    ``read_write_device(rw_id=5, context=1, rw=1)``, laid out as ``MowerDevice.buffer`` reads it:
    ``[3]`` dock rotation, ``[5]``/``[6]`` RTK latitude/longitude (radians times 1e8), ``[7]``/
    ``[8]`` dock x/y (metres times 1e4), ``[13]`` RTK yaw (radians times 1e8)."""
    data = [0] * 14
    data[0] = 1
    data[3] = rotation_deg
    data[5] = round(math.radians(rtk_lat_deg) * 1e8)
    data[6] = round(math.radians(rtk_lon_deg) * 1e8)
    data[7] = round(dock_x * 1e4)
    data[8] = round(dock_y * 1e4)
    data[13] = round(math.radians(rtk_yaw_deg) * 1e8)
    return SystemUpdateBufMsg(update_buf_data=data)


def frames(
    kind: PathType, zone_hash: int, points: list[tuple[float, float]], parts: int = 1
) -> FrameList:
    """A complete :class:`FrameList` of ``points``, split into ``parts`` frames."""
    size = math.ceil(len(points) / parts)
    data = [
        NavGetCommData(
            type=int(kind),
            hash=zone_hash,
            total_frame=parts,
            current_frame=index + 1,
            data_couple=[
                CommDataCouple(x=x, y=y) for x, y in points[index * size : (index + 1) * size]
            ],
        )
        for index in range(parts)
    ]
    return FrameList(total_frame=parts, data=data)


def sample_device(
    name: str = SAMPLE_DEVICE_NAME,
    *,
    product_key: str = SAMPLE_PRODUCT_KEY,
    sub_model_id: str = SAMPLE_SUB_MODEL_ID,
    with_dock: bool = True,
) -> MowerDevice:
    """A docked LUBA 2 AWD 5000X with network RTK and a small map.

    ``sub_model_id=""`` gives a mower whose exact model is not known yet (no limits).
    ``with_dock=False`` gives a mower before the dock buffer (:func:`dock_buffer`) arrived: no
    dock, and the RTK origin as PyMammotion's fallback from ``mqtt_rtk_info`` sets it (yaw 0).
    """
    device = MowerDevice(name=name)
    device.mower_state.product_key = product_key
    device.mower_state.sub_model_id = sub_model_id
    device.mower_state.swversion = "1.30.29.26"
    device.mower_state.travel_speed = 0.4
    dev = device.report_data.dev
    dev.sys_status = int(WorkMode.MODE_READY)
    dev.charge_state = 1
    dev.battery_val = 87
    rtk = device.report_data.rtk
    rtk.status = 4  # RTKStatus.FIX
    rtk.gps_stars = 31
    rtk.age = 1
    rtk.mqtt_rtk_info.rtk_switch = "RTK_USED_NRTK"
    device.report_data.work.knife_height = 55
    device.report_data.connect.used_net = "NET_USED_TYPE_MNET"
    device.report_data.locations = [LocationData(bol_hash=424242)]
    location = device.location
    location.RTK.latitude = math.radians(RTK_LAT_DEG)
    location.RTK.longitude = math.radians(RTK_LON_DEG)
    location.device.latitude = RTK_LAT_DEG + 0.00001
    location.device.longitude = RTK_LON_DEG - 0.00001
    location.orientation = 90
    if with_dock:
        device.buffer(dock_buffer())
    hash_list = device.map
    hash_list.area[FRONT_HASH] = frames(
        PathType.AREA, FRONT_HASH, [(0, 0), (12, 0), (12, 8), (0, 8)], parts=2
    )
    hash_list.area[BACK_HASH] = frames(
        PathType.AREA, BACK_HASH, [(0, 11), (10, 11), (10, 17), (0, 17)]
    )
    hash_list.no_go_zone[NOGO_HASH] = frames(
        PathType.NO_GO_ZONE, NOGO_HASH, [(9, 5.5), (12, 5.5), (12, 8), (9, 8)]
    )
    hash_list.obstacle[OBSTACLE_HASH] = frames(
        PathType.OBSTACLE, OBSTACLE_HASH, [(3, 3), (4, 3), (4, 4), (3, 4)]
    )
    hash_list.path[PATH_HASH] = frames(PathType.PATH, PATH_HASH, [(1, 8), (1, 11)])
    hash_list.area_name = [
        AreaHashNameList(name="Front lawn", hash=FRONT_HASH),
        AreaHashNameList(name="Back lawn", hash=BACK_HASH),
    ]
    return device


def firmware_offer(
    *,
    current: str = SAMPLE_FIRMWARE,
    available: str | None = SAMPLE_NEWER_FIRMWARE,
    release_note: str = "Restores manual driving over Wi-Fi and fixes RTK dropouts.",
    iot_id: str = SAMPLE_IOT_ID,
    isupgrading: bool = False,
    progress: int = 0,
) -> CheckDeviceVersion:
    """A ``CheckDeviceVersion`` as the vendor's version check answers it: ``upgradeable`` with
    ``product_version_info_vo`` when ``available`` is set, else up to date."""
    return CheckDeviceVersion(
        device_id=iot_id,
        device_name=SAMPLE_DEVICE_NAME,
        current_version=current,
        upgradeable=available is not None,
        isupgrading=isupgrading,
        progress=progress,
        product_version_info_vo=ProductVersionInfo(
            release_note=release_note, release_version=available
        )
        if available
        else None,
    )


_MODE_AFTER: Final[dict[str, WorkMode]] = {
    "start_job": WorkMode.MODE_WORKING,
    "pause_execute_task": WorkMode.MODE_PAUSE,
    "resume_execute_task": WorkMode.MODE_WORKING,
    "return_to_dock": WorkMode.MODE_RETURNING,
    "cancel_job": WorkMode.MODE_READY,
}


@dataclass(slots=True)
class SentCommand:
    """One command the fake received."""

    name: str
    priority: Priority
    kwargs: dict[str, Any] = field(default_factory=dict)
    expected_field: str | None = None
    """The reply field of a :meth:`FakeMammotionPort.send_and_wait`; None for a plain send."""
    link: str = "cloud"
    """The link that carried it (``cloud`` or ``ble``, :func:`~.port.bridge_route`)."""

    @property
    def via(self) -> str:
        """``send`` (sent once, no reply awaited) or ``send_and_wait`` (PyMammotion re-sends it
        while no reply arrives)."""
        return "send" if self.expected_field is None else "send_and_wait"


class FakeMammotionPort:
    """:class:`~mowdeck_control_mammotion.port.MammotionPort` without PyMammotion's network code."""

    def __init__(
        self,
        device: MowerDevice | None = None,
        *,
        device_name: str = SAMPLE_DEVICE_NAME,
        product_key: str = SAMPLE_PRODUCT_KEY,
        firmware: str = "1.30.29.26",
        model: str | None = None,
        cooperative: bool = True,
        product_model: tuple[str, str] | None = None,
    ) -> None:
        self._device = device or sample_device(device_name, product_key=product_key)
        self._device_name = device_name
        self._product_key = product_key
        self._firmware = firmware
        self._model = model
        self.cooperative = cooperative
        """When True, commands change the work mode like a real mower would."""
        self.product_model = product_model
        self.dock_answer: SystemUpdateBufMsg | None = dock_buffer()
        """The ``system_update_buf`` the mower sends on the dock buffer request
        (``read_write_device`` with ``rw_id=5``); None: no answer."""
        self.light_answer: bool | None = False
        """The headlamp state the mower reports on a read (``get_car_light(ids=1126)``, answer
        ``get_lamp_rsp``), stored like PyMammotion's reducer in ``mower_state.lamp_info``; None: no
        answer (the read times out). A write (``operate=0``) changes it when the mower answers
        (``light_follows_writes``)."""
        self.light_follows_writes = True
        self.lamp_bright = 80
        """``lamp_bright`` of an answer while the lamp is on."""
        """``(model_id, sub_model_id)`` that ``get_device_product_model`` stores on the device,
        like PyMammotion's reducer on the ``device_product_type_info`` answer (None: no
        answer)."""
        self.opened = 0
        self.closed = 0
        self.open_error: Exception | None = None
        self.link = True
        self.report = True
        self.reauth_required = False
        self.sent: list[SentCommand] = []
        self.fail: dict[str, Exception] = {}
        """Command name to the exception its send raises."""
        self.map_syncs = 0
        self.fresh_requests: list[float] = []
        """``max_age_s`` of every :meth:`ensure_fresh` call, in order."""
        self.refreshes = 0
        """Number of undebounced :meth:`refresh` calls."""
        self.report_stream_requests: list[int] = []
        """``duration_ms`` of every :meth:`start_report_stream` call, in order."""
        self.map_sync_error: Exception | None = None
        self.on_map_sync: Callable[[MowerDevice], None] | None = None
        self.stream: StreamSubscriptionResponse | None = None
        """What :meth:`stream_subscription` returns (None: the cloud sent no token)."""
        self.stream_error: Exception | None = None
        self.stream_requests = 0
        self.stream_all_cameras: list[bool] = []
        """``all_cameras`` of every :meth:`stream_subscription` call, in order."""
        self.video: VideoResourceResponse | None = None
        self.video_error: Exception | None = None
        self.path_lines: tuple[PathLine, ...] | None = SAMPLE_PATH_LINES
        """The route the mower plans: ``start_job`` reports its hash in ``work.path_hash`` and
        :meth:`fetch_mow_path` stores its frames; None: the mower plans nothing (the fetch ends
        without a route)."""
        self.path_error: Exception | None = None
        """What :meth:`fetch_mow_path` raises (for example ``MowPathFetchError``)."""
        self.path_gate: asyncio.Event | None = None
        """When set, :meth:`fetch_mow_path` waits for this event (up to its timeout)."""
        self.path_fetches: list[tuple[bool, GenerateRouteInformation | None]] = []
        """``(plan, route)`` of every :meth:`fetch_mow_path` call, in order."""
        self.wait_real = False
        """When True, :meth:`wait_for` waits up to ``timeout_s`` for a state change
        (:meth:`emit`) that satisfies the predicate, like PyMammotion; otherwise it answers at
        once from the current state."""
        self.ble = False
        """Whether the BLE transport through a bridge is connected (ADR 0038)."""
        self.bridge: BridgeLink | None = None
        """The bridge link attached last (:meth:`attach_bridge`)."""
        self.attach_error: Exception | None = None
        """What :meth:`attach_bridge` raises (the Bluetooth connection failed)."""
        self.rssi: int | None = -61
        """The RSSI reported while the bridge is attached (``None``: unknown)."""
        self.attached = 0
        self.detached = 0
        self.report_links: list[str] = []
        """The link of every report request (``ensure_fresh``, ``refresh``, report stream)."""
        self.report_at = 0.0
        """What :meth:`report_received_at` answers (:meth:`emit` sets it)."""
        self.drive_open: Callable[[], bool] | None = None
        """Set by :meth:`route_bridge` (cloud-first routing on)."""
        self.firmware_check: CheckDeviceVersion | None = None
        """What :meth:`check_firmware` answers (None: the cloud API is not available)."""
        self.firmware_check_error: Exception | None = None
        self.firmware_checks = 0
        self.ota_started: list[str] = []
        """Versions :meth:`start_firmware_update` was asked for, in order."""
        self.ota_error: Exception | None = None
        self.firmware_update_seconds: float | None = None
        """When set, a started update progresses by itself (real seconds) to completion with the
        new version, like the fake cloud's mowers; None: tests deliver the progress."""
        self._ota_task: asyncio.Task[None] | None = None
        self._emitted = asyncio.Event()
        self._listeners: list[StateListener] = []
        self._open = False

    # --- MammotionPort --------------------------------------------------------------------

    @property
    def device_name(self) -> str:
        return self._device_name

    @property
    def is_open(self) -> bool:
        return self._open and not self.reauth_required

    @property
    def link_up(self) -> bool:
        return self._open and self.link

    @property
    def has_report(self) -> bool:
        return self.report

    @property
    def saga_active(self) -> bool:
        return False

    async def open(self) -> None:
        self.opened += 1
        if self.open_error is not None:
            raise self.open_error
        self._open = True

    async def close(self) -> None:
        self.closed += 1
        self._open = False
        self.ble = False
        task, self._ota_task = self._ota_task, None
        if task is not None:
            task.cancel()

    def device(self) -> MowerDevice:
        if not self._open:
            raise PortClosedError("fake port is closed")
        return self._device

    def firmware(self) -> str:
        return self._firmware

    def model(self) -> str:
        """The given model, else derived from the device like ``ClientPort.model``."""
        if self._model is not None:
            return self._model
        return model_of(self._device, self._device_name, self._product_key)

    def set_firmware(self, firmware: str) -> None:
        """Change the reported firmware (for tests)."""
        self._firmware = firmware

    def product_key(self) -> str:
        return self._product_key

    def subscribe(self, listener: StateListener) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def _route(self, command: str | None) -> str:
        return bridge_route(
            command,
            cloud_first=self.drive_open is not None,
            ble_connected=self.ble_connected(),
            drive_open=self.drive_open is not None and self.drive_open(),
        )

    def report_received_at(self) -> float:
        return self.report_at

    def route_bridge(self, drive_open: Callable[[], bool] | None) -> None:
        self.drive_open = drive_open

    async def send(self, command: str, *, priority: Priority, **kwargs: Any) -> None:
        self._check(command)
        self.sent.append(SentCommand(command, priority, dict(kwargs), link=self._route(command)))
        await self._apply(command, kwargs)

    async def send_and_wait(
        self,
        command: str,
        expected_field: str,
        *,
        priority: Priority,
        timeout_s: float,
        **kwargs: Any,
    ) -> Any:
        self._check(command)
        self.sent.append(
            SentCommand(command, priority, dict(kwargs), expected_field, self._route(command))
        )
        if command == "get_car_light" and kwargs.get("ids") == 1126:
            if self.light_answer is None:
                raise CommandTimeoutError(expected_field, 2)
            await self.deliver_light(on=self.light_answer)
            return {expected_field: {}}
        await self._apply(command, kwargs)
        return {expected_field: {}}

    async def wait_for(self, predicate: Callable[[MowerDevice], bool], timeout_s: float) -> bool:
        if not self.wait_real or predicate(self._device):
            return predicate(self._device)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while not predicate(self._device):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            self._emitted.clear()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(remaining):
                    await self._emitted.wait()
        return True

    async def refresh(self) -> None:
        self.refreshes += 1
        self.report_links.append(self._route(None))
        await self.emit()

    async def ensure_fresh(self, max_age_s: float) -> None:
        self.fresh_requests.append(max_age_s)
        self.report_links.append(self._route(None))
        if self._open:
            await self.emit()

    async def start_report_stream(self, duration_ms: int) -> None:
        self.report_stream_requests.append(duration_ms)
        self.report_links.append(self._route(None))
        if self._open:
            await self.emit()

    async def sync_map(self, timeout_s: float) -> None:
        self.map_syncs += 1
        if self.map_sync_error is not None:
            raise self.map_sync_error
        if self.on_map_sync is not None:
            self.on_map_sync(self._device)

    async def fetch_mow_path(
        self, route: GenerateRouteInformation | None, *, plan: bool, timeout_s: float
    ) -> int | None:
        self.path_fetches.append((plan, route))
        if not self._open or not self.link:
            raise NoTransportAvailableError("fake link is down")
        if self.path_gate is not None:
            try:
                async with asyncio.timeout(timeout_s):
                    await self.path_gate.wait()
            except TimeoutError:
                msg = f"the mow path did not arrive within {timeout_s:.0f} s"
                raise TimeoutError(msg) from None
        if self.path_error is not None:
            raise self.path_error
        if self.path_lines is None:
            return 0 if plan else None
        install_mow_path(self._device, self.path_lines)
        await self.emit()
        return route_hash(self.path_lines) if plan else None

    async def stream_subscription(
        self, *, all_cameras: bool = True
    ) -> StreamSubscriptionResponse | None:
        self.stream_requests += 1
        self.stream_all_cameras.append(all_cameras)
        if not self._open:
            raise PortClosedError("fake port is closed")
        if self.stream_error is not None:
            raise self.stream_error
        return self.stream

    async def video_resource(self) -> VideoResourceResponse | None:
        if self.video_error is not None:
            raise self.video_error
        return self.video

    def ble_connected(self) -> bool:
        return self._open and self.ble

    def ble_rssi(self) -> int | None:
        return self.rssi if self.ble_connected() else None

    async def attach_bridge(self, link: BridgeLink) -> None:
        if not self._open:
            raise PortClosedError("fake port is closed")
        self.attached += 1
        if self.attach_error is not None:
            raise self.attach_error
        self.bridge = link
        self.ble = True
        await self.emit()

    async def detach_bridge(self) -> None:
        self.detached += 1
        self.bridge = None
        if self.ble:
            self.ble = False
            if self._open:
                await self.emit()

    async def check_firmware(self) -> CheckDeviceVersion | None:
        if not self._open:
            raise PortClosedError("fake port is closed")
        self.firmware_checks += 1
        if self.firmware_check_error is not None:
            raise self.firmware_check_error
        if self.firmware_check is not None:
            self._device.apply_version_check(self.firmware_check)
        return self.firmware_check

    async def start_firmware_update(self, version: str) -> None:
        if not self._open:
            raise PortClosedError("fake port is closed")
        if self.ota_error is not None:
            raise self.ota_error
        self._device.clear_version_info()
        self.ota_started.append(version)
        if self.firmware_update_seconds is not None:
            self._ota_task = asyncio.create_task(
                self._run_ota(version, self.firmware_update_seconds)
            )

    async def _run_ota(self, version: str, seconds: float) -> None:
        """Progress like a mower pushing ``toapp_upgrade_report`` frames, then the new version."""
        steps = 5
        for step in range(1, steps + 1):
            await asyncio.sleep(seconds / (steps + 1))
            await self.deliver_ota_progress(step * 100 // (steps + 1), version=version)
        await asyncio.sleep(seconds / (steps + 1))
        await self.deliver_ota_progress(100, result=OTA_RESULT_SUCCESS, version=version)

    # --- test helpers ---------------------------------------------------------------------

    async def deliver_ota_progress(
        self,
        progress: int,
        *,
        result: int = OTA_RESULT_IN_PROGRESS,
        version: str = "",
        message: str = "",
        otaid: str = "fake-ota-1",
    ) -> None:
        """Fold an upgrade report into the device like PyMammotion's reducer does for the BLE
        ``toapp_upgrade_report`` (``Device.apply_ota_progress``): a completion (``result`` 0)
        also makes the device report ``version`` as its firmware; then notify the listeners."""
        report = DrvUpgradeReport(
            devname="MainCtrl",
            otaid=otaid,
            version=version,
            progress=progress,
            result=result,
            message=message,
        )
        self._device.apply_ota_progress(report)
        if result == OTA_RESULT_SUCCESS and version:
            self._firmware = version
            self._device.mower_state.swversion = version
        await self.emit()

    def set_work_mode_updating(self, updating: bool) -> None:
        """Put the device into (or out of) ``MODE_UPDATING`` without notifying listeners."""
        self.set_mode(WorkMode.MODE_UPDATING if updating else WorkMode.MODE_READY)

    @property
    def listener_count(self) -> int:
        """Number of active state listeners."""
        return len(self._listeners)

    def names(self) -> list[str]:
        """Names of the commands sent so far, in order."""
        return [c.name for c in self.sent]

    def set_mode(self, mode: WorkMode) -> None:
        """Set the device's work mode (without notifying listeners)."""
        self._device.report_data.dev.sys_status = int(mode)

    async def deliver_light(self, *, on: bool) -> None:
        """Store a headlamp answer like PyMammotion's reducer (``get_lamp_rsp`` with ids 1126:
        ``mower_state.lamp_info.manual_light`` and ``lamp_bright``, see
        :func:`~mowdeck_control_mammotion.mapping.light_on_of`) and notify the listeners."""
        self._device.mower_state.lamp_info = LampInfo(
            lamp_bright=self.lamp_bright if on else 0,
            manual_light=on,
            night_light=self._device.mower_state.lamp_info.night_light,
        )
        await self.emit()

    def light_writes(self) -> list[bool]:
        """The ``manual_ctrl`` value of every headlamp write (``set_car_manual_light``), in
        order."""
        return [
            bool(c.kwargs["manual_ctrl"]) for c in self.sent if c.name == "set_car_manual_light"
        ]

    async def deliver_dock_buffer(self, buffer: SystemUpdateBufMsg | None = None) -> None:
        """Apply a dock buffer (default: :func:`dock_buffer`) like PyMammotion's reducer and
        notify the listeners, as when the mower's ``system_update_buf`` arrives."""
        self._device.buffer(buffer or dock_buffer())
        await self.emit()

    async def emit(self) -> None:
        """Notify every listener, as PyMammotion does after a new report."""
        self.report_at = time.monotonic()
        self._emitted.set()
        for listener in list(self._listeners):
            await listener()

    def _check(self, command: str) -> None:
        if not self._open or not self.link:
            raise NoTransportAvailableError("fake link is down")
        error = self.fail.get(command)
        if error is not None:
            raise error

    async def _apply(self, command: str, kwargs: dict[str, Any] | None = None) -> None:
        if command == "read_write_device" and (kwargs or {}).get("rw_id") == 5:
            if self.dock_answer is not None:
                await self.deliver_dock_buffer(self.dock_answer)
            return
        if command == "set_car_manual_light":
            if self.light_answer is not None and self.light_follows_writes:
                self.light_answer = bool((kwargs or {}).get("manual_ctrl"))
            return
        if command == "get_device_product_model" and self.product_model is not None:
            state = self._device.mower_state
            state.model_id, state.sub_model_id = self.product_model
            await self.emit()
            return
        mode = _MODE_AFTER.get(command)
        if command == "start_job" and self.cooperative and self.path_lines is not None:
            self._device.report_data.work.path_hash = route_hash(self.path_lines)
            self._device.report_data.work.ub_path_hash = self.path_lines[0][0]
        if self.cooperative and mode is not None:
            self.set_mode(mode)
            if mode is not WorkMode.MODE_READY:
                self._device.report_data.dev.charge_state = 0
            await self.emit()


__all__ = [
    "BACK_HASH",
    "DOCK_ROTATION_DEG",
    "DOCK_X",
    "DOCK_Y",
    "EDGE_LINE_HASH",
    "FRONT_HASH",
    "NOGO_HASH",
    "OBSTACLE_HASH",
    "PATH_HASH",
    "SAMPLE_DEVICE_NAME",
    "SAMPLE_FIRMWARE",
    "SAMPLE_IOT_ID",
    "SAMPLE_NEWER_FIRMWARE",
    "SAMPLE_PATH_LINES",
    "SAMPLE_PRODUCT_KEY",
    "SAMPLE_SUB_MODEL_ID",
    "STRIPE_LINE_HASH",
    "CommandTimeoutError",
    "FakeMammotionPort",
    "PathLine",
    "SentCommand",
    "dock_buffer",
    "firmware_offer",
    "frames",
    "install_mow_path",
    "route_hash",
    "sample_device",
]
