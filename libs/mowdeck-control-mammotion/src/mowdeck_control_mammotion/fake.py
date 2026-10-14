"""An in-memory :class:`~mowdeck_control_mammotion.port.MammotionPort` for tests and dry runs.

:class:`FakeMammotionPort` holds a real PyMammotion ``MowerDevice`` (built by
:func:`sample_device`), records every command and, like a cooperative mower, moves the work mode
on the commands it understands (start, pause, resume, dock, cancel), so confirmations and the
StartMow sequence can be exercised without hardware. The owner script's ``--dry-run`` uses it.
"""

import asyncio
import contextlib
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from mowdeck_control_mammotion.port import (
    AreaHashNameList,
    CommandTimeoutError,
    CommDataCouple,
    FrameList,
    LampInfo,
    LocationData,
    MowerDevice,
    NavGetCommData,
    NoTransportAvailableError,
    PathType,
    PortClosedError,
    Priority,
    StateListener,
    StreamSubscriptionResponse,
    SystemUpdateBufMsg,
    VideoResourceResponse,
    WorkMode,
    model_of,
)

SAMPLE_DEVICE_NAME: Final = "Luba-VPDRYRUN"
SAMPLE_PRODUCT_KEY: Final = "a1iMygIwxFC"  # a Luba VP (LUBA 2 AWD X) product key
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
        self.wait_real = False
        """When True, :meth:`wait_for` waits up to ``timeout_s`` for a state change
        (:meth:`emit`) that satisfies the predicate, like PyMammotion; otherwise it answers at
        once from the current state."""
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

    async def send(self, command: str, *, priority: Priority, **kwargs: Any) -> None:
        self._check(command)
        self.sent.append(SentCommand(command, priority, dict(kwargs)))
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
        self.sent.append(SentCommand(command, priority, dict(kwargs), expected_field))
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
        await self.emit()

    async def ensure_fresh(self, max_age_s: float) -> None:
        self.fresh_requests.append(max_age_s)
        if self._open:
            await self.emit()

    async def sync_map(self, timeout_s: float) -> None:
        self.map_syncs += 1
        if self.map_sync_error is not None:
            raise self.map_sync_error
        if self.on_map_sync is not None:
            self.on_map_sync(self._device)

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

    # --- test helpers ---------------------------------------------------------------------

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
    "FRONT_HASH",
    "NOGO_HASH",
    "OBSTACLE_HASH",
    "PATH_HASH",
    "SAMPLE_DEVICE_NAME",
    "SAMPLE_PRODUCT_KEY",
    "SAMPLE_SUB_MODEL_ID",
    "CommandTimeoutError",
    "FakeMammotionPort",
    "SentCommand",
    "dock_buffer",
    "frames",
    "sample_device",
]
