"""Shared implementation of the Mammotion connections on a :class:`MammotionPort`.

:class:`MammotionBase` implements :class:`~mowdeck_control.MowerConnection` once: state mapping
and push updates, capabilities, the map, and the translation of every job command. The cloud and
the BLE proxy connection only differ in how the port is opened and whether manual driving is
offered.

Commands (Mammotion-HA order for ``StartMow``, spike 0001 section 3.3):

=================  =========================================================================
``StartMow``       end a paused job (``cancel_job``), ``generate_route_information`` and wait
                   for its reply ``bidire_reqconver_path``, then ``start_job`` sent once
                   (no reply awaited), confirmed by the state mapping to ``mowing``
``PauseMow``       ``pause_execute_task``, confirmed by the work mode ``MODE_PAUSE``
``ResumeMow``      ``resume_execute_task``, confirmed by ``MODE_WORKING``
``ReturnToDock``   ``return_to_dock``, confirmed by returning, charging or docked; with
                   ``end_job`` first ``cancel_job`` and a short wait until the work mode left
                   the job modes (working, paused), so the job does not resume later
``EmergencyStop``  ``Priority.EMERGENCY``: ``pause_execute_task`` (halts an autonomous job,
                   wheels and blades, in place), ``send_movement(0, 0)`` (halts manual
                   motion) and ``operate_on_device`` with blades off; every step is tried
                   even when an earlier one fails
``SetBladeHeight`` ``set_blade_height`` (millimetres)
``SetSpeed``       ``set_speed`` (metres per second)
``SetLight``       the camera's headlamp: ``set_car_manual_light(manual_ctrl=on)``, then the
                   read ``get_car_light(ids=1126)`` (PyMammotion's ``mower_api.py``); ``executed``
                   when the answer shows the wanted state, else ``accepted``; allowed during
                   an error (AI assist lights up a stuck mower)
``FirmwareUpdate`` cloud only (ADR 0038): ``clear_version_info`` and the vendor's
                   ``start_ota_upgrade`` (:meth:`MammotionPort.start_firmware_update`),
                   confirmed by ``update_check.isupgrading`` or the work mode
                   ``MODE_UPDATING``; the preconditions are the controller's rules
                   (:func:`mowdeck_control.safety.firmware_update_problem`), applied here once
                   more for direct callers
=================  =========================================================================

Firmware (ADR 0038): the state carries ``firmware_info`` once the vendor was asked
(:meth:`MammotionBase.check_firmware`, cloud only) or the mower reports a running update. A
:class:`~mowdeck_control_mammotion.mapping.FirmwareTracker` follows the update through the
device updates (``update_check`` from the MQTT property ``otaProgress`` and the BLE
``toapp_upgrade_report``): it ends ``idle`` on a report with a new version or a completed
frame, ``failed`` on a failed frame (with the mower's message) and ``unknown`` after
``FIRMWARE_UPDATE_MAX_S``. When it ended, the device information rounds run again and the
vendor is asked once more after :data:`FIRMWARE_RECHECK_S`.

Confirmation: every confirmed command is sent first and then confirmed through the reported
state (:class:`~mowdeck_control.TwoPhaseExecution`: :meth:`MammotionBase.dispatch` sends,
the returned ``confirm`` only waits). A missing confirmation within ``confirm_timeout_s`` is
``accepted`` ("command sent; ..."), never ``failed``: the mower may well be doing it.

Never re-send a command that changes what the mower does: PyMammotion's
``send_command_and_wait`` (``DeviceMessageBroker.send_and_wait``, ``retries=2``) calls the send
again on every attempt without a reply. With ``start_job`` and the reply field
``zone_start_precent_t``, which never arrived from the LUBA 2, that re-sent the start 30 s
later and restarted a mower the owner had just paused in the app (incident 2026-10-10). So
:meth:`MammotionPort.send_and_wait` is only for idempotent requests: reads and the route request
``generate_route_information`` (planning the same route twice is harmless). Job commands use
:meth:`MammotionPort.send` with ``Priority.USER``, which PyMammotion sends once (it only repeats
a send the cloud answered with a gateway timeout, see ``execute_command``).

Why pause and not ``cancel_job`` for the emergency stop: pausing is the command the app uses to
halt a running job, it stops in place and keeps the job so a person decides what happens next;
what ``cancel_job`` does with a mower that is out on the lawn (stop or drive home) is not
documented in PyMammotion. Spike step 4 verifies the pause on hardware.

Mow path (:class:`~mowdeck_control.MowPathSource`, :class:`~mowdeck_control.PathPreviewCapable`):
once the work mode shows a job (working, paused, returning) and the report names a route
(``work.path_hash`` above 1) that the adapter does not hold yet, a background task runs
PyMammotion's ``MowPathSaga`` with ``skip_planning`` (:meth:`MammotionPort.fetch_mow_path`): the
route the mower already planned is fetched, never planned again. It never blocks a command,
waits at most ``PATH_FETCH_TIMEOUT_S``, and a failure is logged and tried again after
``PATH_RETRY_S`` (``PATH_ATTEMPTS`` per route); after a reconnect during a job the same rule
fetches it. :meth:`MammotionBase.current_path` adds the progress of every report
(``ub_path_hash``, ``now_index``, ``path_direction``) and returns None once the job ended.
:meth:`MammotionBase.preview_path` plans a start's route without ``start_job``, as the vendor app
does before a start: the saga sends ``generate_route_information`` itself.

Fresh state: the Mammotion broker forwards reports only when someone asks for them, so with
``refresh_interval_s > 0`` a background task sleeps that long and then calls
:meth:`MammotionPort.ensure_fresh` (a one-shot report when the last one is older than half the
interval; PyMammotion debounces and queues it with background priority). Every device update
publishes the state when it changed, and an unchanged state again once
``UNCHANGED_REPUBLISH_S`` passed since the last publish, so ``observed_at`` tells honestly when
the mower last reported, not when its state last changed.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from mowdeck_contracts.v1 import (
    CameraSession,
    ConnectionKind,
    EmergencyStop,
    FirmwareInfo,
    FirmwareUpdate,
    LinkStatus,
    MowerActivity,
    MowerCapabilities,
    MowerMap,
    MowerState,
    MowPath,
    PauseMow,
    ResumeMow,
    ReturnToDock,
    SetBladeHeight,
    SetLight,
    SetSpeed,
    StartMow,
    utc_now,
)
from mowdeck_control import (
    CommandRejected,
    ConnectionConfig,
    Dispatch,
    DriveSession,
    JobCommand,
    NotConnected,
    Outcome,
    PathUnavailable,
    SensorStop,
    StateBroadcaster,
    StateSubscription,
    UnsupportedPattern,
    firmware_update_problem,
)

from mowdeck_control_mammotion import mapping
from mowdeck_control_mammotion.port import (
    CommandTimeoutError,
    FirmwareRefused,
    GenerateRouteInformation,
    MammotionPort,
    MowerDevice,
    MowPathFetchError,
    NoTransportAvailableError,
    PortClosedError,
    Priority,
    TransportError,
    WorkMode,
    build_route_information,
)

_log = logging.getLogger("mowdeck_control_mammotion")

DEFAULT_BLADE_HEIGHT_MM: Final = 60
DEFAULT_SPEED_MPS: Final = 0.3
ROUTE_REQUEST_TIMEOUT_S: Final = 10.0
"""Per-attempt wait for the route reply (PyMammotion retries once); it holds the queue."""
MAP_MAX_AGE_S: Final = 300.0
UNCHANGED_REPUBLISH_S: Final = 5.0
"""An unchanged state is published again on a device update after this many seconds."""
_PAUSED_JOB_CLEAR_S: Final = 15.0
_JOB_MODES: Final = frozenset(
    int(m)
    for m in (
        WorkMode.MODE_PAUSE,
        WorkMode.MODE_WORKING,
        WorkMode.MODE_CHARGING_PAUSE,
        WorkMode.MODE_CORRIDOR_PAUSE,
    )
)
"""Work modes of a running or paused job; ``ReturnToDock(end_job=True)`` waits until they end."""
DEVICE_INFO_RETRY_S: Final = 30.0
"""Wait before the missing device information is requested again."""
DEVICE_INFO_ATTEMPTS: Final = 4
"""Requests of the device information at most: at connect and up to three retries."""
_FIRMWARE_COMMANDS: Final = ("get_device_version_info", "get_device_base_info")
"""Answers fill ``device_firmwares`` (the firmware version)."""
_PRODUCT_MODEL_COMMAND: Final = "get_device_product_model"
"""Answer ``device_product_type_info`` fills ``mower_state.model_id`` and ``sub_model_id``:
the exact model, which selects the blade height and speed limits."""
_ALL_DEVICE_INFO: Final = (*_FIRMWARE_COMMANDS, _PRODUCT_MODEL_COMMAND)
DOCK_BUFFER_COMMAND: Final = "read_write_device"
DOCK_BUFFER_ARGS: Final[dict[str, int]] = {"rw_id": 5, "context": 1, "rw": 1}
"""``read_write_device(rw_id=5, context=1, rw=1)``: the mower answers with ``system_update_buf``
whose first value is 1, which PyMammotion's ``MowerDevice.buffer`` turns into the RTK origin
(``location.RTK`` with its yaw) and the dock pose (``location.dock``). Mammotion-HA sends exactly
this (``coordinator.py`` ``async_rtk_dock_location``, the "resync RTK dock" button) whenever the
RTK or dock latitude is still 0; PyMammotion's ``mower_api.py`` has the same call."""
DOCK_WAIT_S: Final = 15.0
"""How long a map read waits for the dock buffer when the dock or the origin is unknown."""
LIGHT_COMMAND: Final = "set_car_manual_light"
"""Switches the headlamp: ``manual_ctrl=True`` sends ``SetHeadlamp`` with ``set_ids`` 1125
(on), False with 1127 (off), ``lamp_power_ctrl=2`` (``mammotion/commands/messages/media.py``)."""
LIGHT_READ_COMMAND: Final = "get_car_light"
LIGHT_READ_ARGS: Final[dict[str, Any]] = {"ids": 1126}
"""``get_car_light(ids=1126)``: the manual light setting, as PyMammotion's
``mower_api.async_set_manual_light`` and Mammotion-HA read it back."""
LIGHT_REPLY: Final = "get_lamp_rsp"
"""The answer's field in ``SocMul`` (oneof ``SubMul``; the broker matches the leaf name);
PyMammotion's reducer sets ``mower_state.lamp_info.manual_light`` from it."""
LIGHT_READ_TIMEOUT_S: Final = 5.0
"""Wait per attempt for the answer to a light read (PyMammotion sends twice)."""
LIGHT_CONFIRM_S: Final = 3.0
"""How long a light switch waits for the device state to show the answered read."""
PATH_FETCH_TIMEOUT_S: Final = 90.0
"""Longest wait for the route of a running job (PyMammotion's saga ends itself by then too)."""
PATH_PREVIEW_TIMEOUT_S: Final = 90.0
"""Longest wait for a planned preview route."""
PATH_RETRY_S: Final = 30.0
"""Wait before the route of a running job is fetched again after a failed attempt."""
PATH_ATTEMPTS: Final = 3
"""Fetches of the route of one job at most."""
FIRMWARE_RECHECK_S: Final = 60.0
"""Wait after an update ended before the vendor is asked again (the cloud lags the mower)."""
FIRMWARE_CONFIRM_S: Final = 120.0
"""Longest wait for the mower to report a requested update as running."""


class MammotionBase:
    """:class:`~mowdeck_control.MowerConnection` on a :class:`MammotionPort`.

    Subclasses set :attr:`kind` and decide about manual driving in
    :meth:`start_drive_session` and :meth:`_manual_drive_offered`.
    """

    _kind: ConnectionKind = ConnectionKind.CLOUD

    def __init__(
        self,
        port: MammotionPort,
        config: ConnectionConfig,
        *,
        map_sync_timeout_s: float = 300.0,
        confirm_timeout_s: float = 30.0,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] | None = None,
        refresh_interval_s: float = 0.0,
        active_refresh_interval_s: float | None = None,
        report_stream_s: float = 0.0,
        device_info_retry_s: float = DEVICE_INFO_RETRY_S,
    ) -> None:
        if refresh_interval_s < 0:
            msg = "refresh_interval_s must be 0 (off) or positive"
            raise ValueError(msg)
        if active_refresh_interval_s is not None and active_refresh_interval_s <= 0:
            msg = "active_refresh_interval_s must be positive"
            raise ValueError(msg)
        self._port = port
        self._refresh_interval_s = refresh_interval_s
        self._active_refresh_interval_s = active_refresh_interval_s
        if report_stream_s < 0:
            msg = "report_stream_s must be 0 (off) or positive"
            raise ValueError(msg)
        self._report_stream_s = report_stream_s
        self._boost_until = 0.0
        self._refresh_wake = asyncio.Event()
        self._refresher: asyncio.Task[None] | None = None
        self._device_info_retry_s = device_info_retry_s
        self._device_info_task: asyncio.Task[None] | None = None
        self._published_at: float | None = None
        self._publish_clock: Callable[[], float] = time.monotonic
        self._config = config
        self._map_sync_timeout_s = map_sync_timeout_s
        self._confirm_timeout_s = confirm_timeout_s
        self._clock = clock
        self._monotonic = monotonic or (lambda: asyncio.get_running_loop().time())
        self._broadcaster = StateBroadcaster()
        self._connected = False
        self._unsubscribe: Callable[[], None] | None = None
        self._last_state: MowerState | None = None
        self._map: MowerMap | None = None
        self._map_synced_at: float | None = None
        self._blade_height_mm: int | None = None
        self._speed_mps: float | None = None
        self._light_reported = False
        self._lock = asyncio.Lock()
        self._planned: mapping.PlannedPath | None = None
        self._path_task: asyncio.Task[None] | None = None
        self._path_tries: tuple[str, int, float] | None = None
        """Route hash, attempts and loop time of the last fetch of a job's route."""
        self._route: GenerateRouteInformation | None = None
        """The route of the last start (used as the job settings of a later fetch)."""
        self._preview_lock = asyncio.Lock()
        self._firmware = mapping.FirmwareTracker()
        self._firmware_recheck: asyncio.Task[None] | None = None

    # --- MowerConnection: lifecycle -------------------------------------------------------

    @property
    def kind(self) -> ConnectionKind:
        return self._kind

    @property
    def is_connected(self) -> bool:
        return self._connected and self._port.is_open and self._link_alive()

    @property
    def port(self) -> MammotionPort:
        """The PyMammotion port (for scripts and diagnostics)."""
        return self._port

    async def connect(self) -> None:
        async with self._lock:
            if self._connected:
                return
            try:
                await self._port.open()
            except Exception:
                await self._port.close()
                raise
            device_name = self._port.device_name
            if not mapping.is_mower(device_name, self._port.product_key()):
                await self._port.close()
                msg = f"{device_name} is not a mower"
                raise CommandRejected(msg)
            self._connected = True
            self._light_reported = False
            self._unsubscribe = self._port.subscribe(self._on_device_update)
            self._publish(force=True)
            if self._refresh_interval_s > 0:
                self._refresher = asyncio.create_task(
                    self._refresh_loop(), name=f"refresh-{device_name}"
                )
            self._device_info_task = asyncio.create_task(
                self._request_device_info(), name=f"device-info-{device_name}"
            )
            _log.info("mammotion %s connected over %s", device_name, self.kind)

    async def disconnect(self) -> None:
        async with self._lock:
            if not self._connected:
                return
            self._connected = False
            await self._stop_refresh()
            await self._stop_device_info()
            await self._stop_path_fetch()
            await self._stop_firmware_recheck()
            if self._unsubscribe is not None:
                self._unsubscribe()
                self._unsubscribe = None
            await self._before_close()
            await self._port.close()
            self._broadcaster.close()

    @property
    def refresh_interval_s(self) -> float:
        """Seconds between fresh state requests (0: off)."""
        return self._refresh_interval_s

    @property
    def active_refresh_interval_s(self) -> float | None:
        """Seconds between fresh state requests while the mower is active (None: same as
        :attr:`refresh_interval_s`)."""
        return self._active_refresh_interval_s

    def _drive_active(self) -> bool:
        """Whether a manual drive session is running (only the BLE link has one)."""
        return False

    def _is_active(self) -> bool:
        state = self._last_state
        if self._drive_active():
            return True
        return state is not None and state.activity in ACTIVE_ACTIVITIES

    @property
    def report_stream_s(self) -> float:
        """Length of the continuous report window kept open while active (0: off)."""
        return self._report_stream_s

    def _clock_s(self) -> float:
        return asyncio.get_running_loop().time()

    def boost_refresh(self) -> None:
        """A job command went out (or a drive session started): ask for reports at once and
        keep doing so for :data:`BOOST_MIN_S` (at least one stream window) even though the
        state still says docked, so the first ``mowing`` state arrives within seconds instead
        of after the idle interval plus PyMammotion's 15 s debounce. No-op without a refresh
        loop (BLE bridge)."""
        if self._refresh_interval_s <= 0 or self._refresher is None:
            return
        self._boost_until = self._clock_s() + max(BOOST_MIN_S, self._report_stream_s)
        self._refresh_wake.set()

    def _boosted(self) -> bool:
        return self._clock_s() < self._boost_until

    def _mode(self) -> str:
        """``stream`` (active, window renewed), ``boost`` (command sent, not yet active: stream
        call plus fast polls) or ``poll`` (one-shots at the activity's interval)."""
        if self._report_stream_s > 0 and self._is_active():
            return "stream"
        if self._boosted() and not self._is_active():
            return "boost"
        return "poll"

    def _current_refresh_interval_s(self) -> float:
        """The interval for the mower's current activity: half the report stream window while
        streaming, the fast boost poll after a command, else the active one (never longer than
        the idle one) while mowing, returning, paused off the dock or driving, else the idle
        one."""
        mode = self._mode()
        if mode == "stream":
            return self._report_stream_s / 2
        if mode == "boost":
            return min(BOOST_POLL_S, self._refresh_interval_s)
        if self._active_refresh_interval_s is not None and self._is_active():
            return min(self._active_refresh_interval_s, self._refresh_interval_s)
        return self._refresh_interval_s

    async def _refresh_loop(self) -> None:
        """Ask the mower for a fresh report while connected, at the interval of its activity.

        While active (and ``report_stream_s`` > 0) the loop renews PyMammotion's continuous
        report window every half window (about one report per second from the mower); when the
        mower becomes inactive the window is left to expire and the idle one-shot requests
        resume. After a job command (:meth:`boost_refresh`) it opens the window at once and, as
        PyMammotion only streams once its own state is active, also asks for an undebounced
        report (:meth:`MammotionPort.refresh`) every :data:`BOOST_POLL_S`. The interval is
        re-read on every wake-up, and a change of the activity or a boost wakes the loop, so a
        start shortens the wait that is already running (counted from the last request; a
        stream or boost starts at once) and a return to the dock lengthens it.
        """
        loop = asyncio.get_running_loop()
        last = loop.time()
        previous_mode = "poll"
        while self._connected:
            self._refresh_wake.clear()
            mode = self._mode()
            interval = self._current_refresh_interval_s()
            remaining = last + interval - loop.time()
            if mode != "poll" and mode != previous_mode:
                remaining = 0  # open the window / start polling as soon as it applies
            previous_mode = mode
            if remaining > 0:
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(remaining):
                        await self._refresh_wake.wait()
                    continue  # activity changed: work out the wait again
            if not self._connected:
                return
            last = loop.time()
            try:
                if mode == "poll":
                    # Half the interval: the answer to the previous request lands a moment after
                    # it was sent, so its age at the next tick is a little under ``interval``.
                    await self._port.ensure_fresh(interval / 2)
                    continue
                if self._report_stream_s > 0:
                    await self._port.start_report_stream(
                        max(1, round(self._report_stream_s * 1000))
                    )
                if mode == "boost":
                    await self._port.refresh()  # user path: no debounce, no queue
            except Exception:  # the loop never dies on a failed request
                _log.debug("fresh state request failed", exc_info=True)

    async def _request_device_info(self) -> None:
        """Ask the mower for its firmware, exact model, RTK origin, dock and light state, again
        while missing.

        Like Mammotion-HA (``_fetch_missing_device_info`` and ``async_rtk_dock_location``): the
        cloud only forwards these answers when requested. The first round at connect sends every
        command (over BLE only the dock buffer request); every later round only those whose data
        is still missing (firmware: the version commands; model: ``get_device_product_model``;
        RTK origin or dock: :data:`DOCK_BUFFER_COMMAND`). Mowers with a light (both links) also
        read the headlamp (:data:`LIGHT_READ_COMMAND` with :data:`LIGHT_READ_ARGS`) until an
        answer arrived. At most :data:`DEVICE_INFO_ATTEMPTS`
        rounds, ``device_info_retry_s`` apart, all with background priority. Failures are
        ignored (the next round or report may still fill them).
        """
        for attempt in range(DEVICE_INFO_ATTEMPTS):
            if attempt:
                await asyncio.sleep(self._device_info_retry_s)
                if not self._connected:
                    return
            commands = self._device_info_requests(first=not attempt)
            read_light = self._light_unknown()
            if not commands and not read_light:
                return
            for command, kwargs in commands:
                try:
                    await self._port.send(command, priority=Priority.BACKGROUND, **kwargs)
                except Exception:
                    _log.debug("device information request %s failed", command, exc_info=True)
            if read_light:
                await self._read_light(Priority.BACKGROUND)

    def _light_unknown(self) -> bool:
        """True for a mower with a light whose state has not been reported yet."""
        if self._light_reported:
            return False
        if not mapping.has_light(self._port.device_name, self._port.product_key()):
            return False
        try:
            return not mapping.light_reported(self._port.device())
        except Exception:  # a closed port: nothing more to ask
            return False

    async def _read_light(self, priority: Priority) -> bool:
        """Read the headlamp and wait for the answer; True when it arrived (best effort)."""
        try:
            await self._port.send_and_wait(
                LIGHT_READ_COMMAND,
                LIGHT_REPLY,
                priority=priority,
                timeout_s=LIGHT_READ_TIMEOUT_S,
                **LIGHT_READ_ARGS,
            )
        except Exception:
            _log.debug("light read of %s failed", self._port.device_name, exc_info=True)
            return False
        self._light_reported = True
        if self._connected:
            self._publish()
        return True

    def _device_info_requests(self, *, first: bool) -> list[tuple[str, dict[str, Any]]]:
        """The device information commands of one round with their arguments."""
        cloud = self.kind is ConnectionKind.CLOUD
        if first:
            names = list(_ALL_DEVICE_INFO) if cloud else []
            return [(n, {}) for n in names] + [(DOCK_BUFFER_COMMAND, dict(DOCK_BUFFER_ARGS))]
        requests: list[tuple[str, dict[str, Any]]] = []
        if cloud:
            requests.extend((n, {}) for n in self._missing_device_info())
        try:
            device = self._port.device()
        except Exception:  # a closed port: nothing more to ask
            return requests
        if not mapping.dock_known(device):
            requests.append((DOCK_BUFFER_COMMAND, dict(DOCK_BUFFER_ARGS)))
        return requests

    def _missing_device_info(self) -> list[str]:
        """The firmware and model commands whose answer is still missing."""
        missing: list[str] = []
        if not self._port.firmware():
            missing.extend(_FIRMWARE_COMMANDS)
        try:
            state = self._port.device().mower_state
        except Exception:  # a closed port: nothing more to ask
            return missing
        if not (state.sub_model_id or state.model_id):
            missing.append(_PRODUCT_MODEL_COMMAND)
        return missing

    async def _stop_device_info(self) -> None:
        task, self._device_info_task = self._device_info_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _stop_path_fetch(self) -> None:
        task, self._path_task = self._path_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _stop_firmware_recheck(self) -> None:
        task, self._firmware_recheck = self._firmware_recheck, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _stop_refresh(self) -> None:
        task, self._refresher = self._refresher, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # --- MowerConnection: state -----------------------------------------------------------

    async def capabilities(self) -> MowerCapabilities:
        self._require_connected()
        return mapping.capabilities_of(
            self._port.device(),
            device_name=self._port.device_name,
            product_key=self._port.product_key(),
            manual_drive=self._manual_drive_offered(),
            manual_drive_blades=self._manual_blades_allowed(),
            camera=self._camera_offered(),
            model=self._port.model(),
            links_supported=self._links_supported(),
            firmware_update=self._firmware_offered(),
        )

    async def get_state(self) -> MowerState:
        self._require_connected()
        return self._snapshot()

    def watch_state(self) -> StateSubscription:
        self._require_connected()
        return self._broadcaster.subscribe(initial=self._snapshot())

    async def get_map(self) -> MowerMap:
        """The map; synced from the mower on first use and again after ``MAP_MAX_AGE_S``."""
        self._require_connected()
        now = self._monotonic()
        fresh = self._map_synced_at is not None and now - self._map_synced_at < MAP_MAX_AGE_S
        if not (fresh and mapping.map_complete(self._port.device())):
            try:
                await self._port.sync_map(self._map_sync_timeout_s)
            except TimeoutError:
                _log.warning("map sync of %s timed out", self._port.device_name)
            except (NoTransportAvailableError, PortClosedError) as exc:
                raise NotConnected(str(exc)) from exc
            self._map_synced_at = self._monotonic()
        await self._await_dock()
        self._map = mapping.map_of(self._port.device())
        return self._map

    def current_map(self) -> MowerMap | None:
        """:class:`~mowdeck_control.MapRebuildable`: the map rebuilt from the device data
        PyMammotion holds (no sync, no command), so the dock and the RTK reference that arrive
        with the dock buffer after :meth:`get_map` reach the map snapshot. None before the first
        :meth:`get_map` or when not connected."""
        if self._map is None or not self.is_connected:
            return self._map
        try:
            self._map = mapping.map_of(self._port.device())
        except Exception:
            _log.debug("map rebuild of %s failed", self._config.mower_id, exc_info=True)
        return self._map

    async def _await_dock(self) -> None:
        """Ask for the dock buffer and wait up to :data:`DOCK_WAIT_S` while the dock or the RTK
        origin is unknown, so the map (and the snapshot built from it) carries both.

        The connect-time request may still be queued behind the map sync; this one goes out
        right after it. Best effort: a failed send or no answer leaves the map without a dock.
        """
        if mapping.dock_known(self._port.device()):
            return
        try:
            await self._port.send(
                DOCK_BUFFER_COMMAND, priority=Priority.BACKGROUND, **DOCK_BUFFER_ARGS
            )
            await self._port.wait_for(mapping.dock_known, DOCK_WAIT_S)
        except Exception:
            _log.debug("dock buffer request of %s failed", self._port.device_name, exc_info=True)

    # --- MowerConnection: commands --------------------------------------------------------

    async def execute(self, command: JobCommand) -> Outcome:
        return await (await self.dispatch(command)).settle()

    async def dispatch(self, command: JobCommand) -> Dispatch:
        """:class:`~mowdeck_control.TwoPhaseExecution`: send ``command``; a confirmed command
        returns a ``confirm`` that only waits for the state (see the module docstring)."""
        self._require_connected()
        if isinstance(command, EmergencyStop):
            return Dispatch.done(await self._emergency_stop())
        if not isinstance(command, SetLight):
            self._require_no_sensor_stop()
        try:
            match command:
                case StartMow():
                    return await self._start_mow(command)
                case PauseMow():
                    await self._send("pause_execute_task")
                    return self._awaiting("pause_execute_task", {WorkMode.MODE_PAUSE}, "paused")
                case ResumeMow():
                    await self._send("resume_execute_task")
                    return self._awaiting("resume_execute_task", {WorkMode.MODE_WORKING}, "mowing")
                case ReturnToDock():
                    if command.end_job:
                        await self._end_job()
                    await self._send("return_to_dock")
                    return self._awaiting(
                        "return_to_dock",
                        {WorkMode.MODE_RETURNING, WorkMode.MODE_CHARGING},
                        "returning",
                        dock=True,
                    )
                case SetBladeHeight():
                    await self._send("set_blade_height", height=command.mm)
                    self._blade_height_mm = command.mm
                    return Dispatch.done(Outcome.accepted(f"blade height {command.mm} mm sent"))
                case SetSpeed():
                    await self._send("set_speed", speed=float(command.mps))
                    self._speed_mps = command.mps
                    return Dispatch.done(Outcome.accepted(f"speed {command.mps} m/s sent"))
                case SetLight():
                    return Dispatch.done(await self._set_light(on=command.on))
                case FirmwareUpdate():
                    return await self._firmware_update(command.version)
        except CommandTimeoutError:
            # Only the route request waits for a reply; ``start_job`` was not sent.
            return Dispatch.done(Outcome.failed("the mower did not answer in time"))
        except (NoTransportAvailableError, PortClosedError) as exc:
            raise NotConnected("no usable link to the mower") from exc
        except TransportError as exc:
            return Dispatch.done(Outcome.failed(f"transport error: {type(exc).__name__}"))
        msg = f"unsupported command {type(command).__name__}"  # pragma: no cover - exhaustive
        raise CommandRejected(msg)  # pragma: no cover

    async def start_drive_session(self) -> DriveSession:
        self._require_connected()
        raise CommandRejected("manual drive is not available over cloud")

    async def get_camera_session(self) -> CameraSession | None:
        """:class:`~mowdeck_control.CameraCapable`: None here; the cloud connection overrides
        it (the video goes through Agora, which only the cloud API reaches)."""
        self._require_connected()
        return None

    # --- hooks for subclasses -------------------------------------------------------------

    def _manual_drive_offered(self) -> bool:
        return False

    def _camera_offered(self) -> bool:
        return False

    def _manual_blades_allowed(self) -> bool:
        return False

    def _firmware_offered(self) -> bool:
        """Whether the vendor's firmware service is reachable (the cloud connections)."""
        return False

    def _links_supported(self) -> list[str]:
        """Additional link kinds this connection can open (``capabilities.links_supported``)."""
        return []

    def _link_statuses(self) -> dict[str, LinkStatus]:
        """The state of the additional links for ``MowerState.links``."""
        return {}

    def _connection_kind(self) -> ConnectionKind:
        """The kind the state reports (the link in use right now)."""
        return self._kind

    def _link_alive(self) -> bool:
        return True

    def _link_label(self) -> str | None:
        return None

    async def _before_close(self) -> None:
        return None

    # --- firmware (ADR 0038) --------------------------------------------------------------

    @property
    def firmware_info(self) -> FirmwareInfo | None:
        """The firmware state the snapshot reports (None before a check or an update)."""
        return self._firmware.info

    async def _check_firmware(self) -> FirmwareInfo | None:
        """Ask the vendor (:meth:`MammotionPort.check_firmware`) and store the answer."""
        self._require_connected()
        if not self._firmware_offered():
            return None
        try:
            check = await self._port.check_firmware()
        except (NoTransportAvailableError, PortClosedError) as exc:
            raise NotConnected("the Mammotion cloud session is not open") from exc
        except Exception as exc:
            _log.warning(
                "firmware check of %s failed: %s", self._port.device_name, type(exc).__name__
            )
            raise NotConnected("the Mammotion cloud did not answer the firmware check") from exc
        info = self._firmware.checked(check, installed=self._port.firmware(), now=self._clock())
        _log.info(
            "firmware of %s: installed %s, %s%s",
            self._port.device_name,
            info.installed,
            info.update_state,
            f" {info.available}" if info.available else "",
        )
        if self._connected:
            self._publish(force=True)
        return info

    async def _firmware_update(self, version: str) -> Dispatch:
        """Send the update request (``FirmwareUpdate``) after the controller's rules."""
        if not self._firmware_offered():
            raise CommandRejected("firmware updates need the cloud link")
        problem = firmware_update_problem(
            version, self._snapshot(), await self.capabilities(), drive_session_open=False
        )
        if problem is not None:
            raise CommandRejected(problem)
        try:
            await self._port.start_firmware_update(version)
        except FirmwareRefused as exc:
            raise CommandRejected(str(exc)) from exc
        self._firmware.started(version, installed=self._port.firmware(), loop_time=self._clock_s())
        self._publish(force=True)
        self.boost_refresh()

        async def confirm() -> Outcome:
            def running(device: MowerDevice) -> bool:
                return (
                    bool(device.update_check.isupgrading)
                    or mapping.work_mode(device) is WorkMode.MODE_UPDATING
                )

            if await self._port.wait_for(running, min(self._confirm_timeout_s, FIRMWARE_CONFIRM_S)):
                return Outcome.executed(f"firmware update to {version} is running")
            return Outcome.accepted(
                f"firmware update to {version} requested; the mower has not reported it yet"
            )

        return Dispatch.awaiting(f"firmware update to {version} requested", confirm)

    def _track_firmware(self, device: MowerDevice) -> None:
        """Fold the device update into the firmware tracker; an ended update republishes the
        device information and asks the vendor again after :data:`FIRMWARE_RECHECK_S`."""
        ended = self._firmware.observe(
            device, installed=self._port.firmware(), loop_time=self._clock_s(), now=self._clock()
        )
        if ended in ("finished", "failed", "unknown"):
            _log.info("firmware update of %s ended: %s", self._port.device_name, ended)
            if (
                self._connected
                and self._device_info_task is not None
                and self._device_info_task.done()
            ):
                self._device_info_task = asyncio.create_task(
                    self._request_device_info(), name=f"device-info-{self._port.device_name}"
                )
            if self._connected and self._firmware_offered() and self._firmware_recheck is None:
                self._firmware_recheck = asyncio.create_task(
                    self._recheck_firmware_later(),
                    name=f"firmware-recheck-{self._port.device_name}",
                )

    async def _recheck_firmware_later(self) -> None:
        try:
            await asyncio.sleep(FIRMWARE_RECHECK_S)
            if self._connected:
                await self._check_firmware()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.debug("firmware re-check of %s failed", self._port.device_name, exc_info=True)
        finally:
            self._firmware_recheck = None

    # --- commands -------------------------------------------------------------------------

    async def _send(
        self, command: str, *, priority: Priority = Priority.USER, **kwargs: Any
    ) -> None:
        await self._port.send(command, priority=priority, **kwargs)
        if command in BOOST_COMMANDS:
            self.boost_refresh()

    def _awaiting(
        self, sent: str, modes: set[WorkMode], label: str, *, dock: bool = False
    ) -> Dispatch:
        """``sent`` went out; the confirmation waits for one of ``modes`` (see :meth:`_confirm`)."""
        return Dispatch.awaiting(f"{sent} sent", lambda: self._confirm(modes, label, dock=dock))

    async def _confirm(self, modes: set[WorkMode], label: str, *, dock: bool = False) -> Outcome:
        wanted = {int(mode) for mode in modes}

        def reached(device: MowerDevice) -> bool:
            if device.report_data.dev.sys_status in wanted:
                return True
            return dock and mapping.activity_of(device) is MowerActivity.DOCKED

        return await self._confirm_by(reached, label)

    async def _confirm_mowing(self) -> Outcome:
        """``StartMow`` is confirmed by the activity the state mapping calls ``mowing`` (every
        work mode it maps to mowing), so the result agrees with the published state."""

        def mowing(device: MowerDevice) -> bool:
            return mapping.activity_of(device) is MowerActivity.MOWING

        return await self._confirm_by(mowing, "mowing")

    async def _confirm_by(self, reached: Callable[[MowerDevice], bool], label: str) -> Outcome:
        """Wait for ``reached``; only observes the state, never sends (safe to cancel)."""
        if await self._port.wait_for(reached, self._confirm_timeout_s):
            return Outcome.executed(f"mower is {label}")
        return Outcome.accepted(
            f"command sent; the mower did not report {label} within {self._confirm_timeout_s:.0f} s"
        )

    async def _start_mow(self, command: StartMow) -> Dispatch:
        if command.plan_ref is not None:
            raise CommandRejected("stored plans are not supported by the Mammotion adapter")
        if command.spot_plan is not None:
            # The controller already sends the native job instead; this guards direct callers.
            native = command.spot_plan.native
            if native is None:
                raise CommandRejected("custom paths not supported")
            command = StartMow(area_ids=list(native.area_ids), pattern=dict(native.pattern))
        device = self._port.device()
        activity = mapping.activity_of(device, has_report=self._port.has_report)
        if activity in (MowerActivity.MOWING, MowerActivity.RETURNING):
            msg = f"the mower is {activity.value}; pause or end the job first"
            raise CommandRejected(msg)
        plan, route = await self._route_for(command, device)
        if activity is MowerActivity.PAUSED:
            _log.info("ending the paused job of %s before a new start", self._port.device_name)
            await self._send("cancel_job")
            await self._port.wait_for(
                lambda d: d.report_data.dev.sys_status != int(WorkMode.MODE_PAUSE),
                _PAUSED_JOB_CLEAR_S,
            )
        # The route request is idempotent: PyMammotion may re-send it while no reply arrives.
        # Its wait holds the command queue, so it is short (the mower answers within seconds);
        # a later pause or return must not expire behind it.
        await self._port.send_and_wait(
            "generate_route_information",
            "bidire_reqconver_path",
            priority=Priority.USER,
            timeout_s=min(self._confirm_timeout_s, ROUTE_REQUEST_TIMEOUT_S),
            generate_route_information=route,
        )
        # ``start_job`` exactly once: no reply to wait for, no automatic resend.
        await self._send("start_job")
        self._route = route
        self._path_tries = None  # a new job: its route gets fresh attempts
        areas = ", ".join(command.area_ids)
        return Dispatch.awaiting(
            f"start_job sent ({areas} with {plan.job.pattern})", self._confirm_mowing
        )

    async def _route_for(
        self, command: StartMow, device: MowerDevice
    ) -> tuple[mapping.RoutePlan, GenerateRouteInformation]:
        """The route settings of an area start; raises ``CommandRejected`` for unknown areas or
        settings the mower cannot run."""
        known = set(device.map.area)
        unknown = [a for a in command.area_ids if not a.isdigit() or int(a) not in known]
        if unknown:
            msg = f"unknown area ids: {', '.join(unknown)}"
            raise CommandRejected(msg)
        capabilities = await self.capabilities()
        spacing = mapping.path_spacing_of(device, self._port.product_key())
        try:
            plan = mapping.route_plan(
                command,
                capabilities,
                blade_height_mm=self._default_blade_height(device),
                speed_mps=self._speed_mps or DEFAULT_SPEED_MPS,
                path_spacing_cm=spacing,
            )
        except (ValueError, UnsupportedPattern) as exc:
            raise CommandRejected(str(exc)) from exc
        return plan, build_route_information(self._port.device_name, device, plan.settings)

    # --- mow path -------------------------------------------------------------------------

    def current_path(self) -> MowPath | None:
        """:class:`~mowdeck_control.MowPathSource`: the route of the running job with the
        progress of the latest report; None without a job, before the route arrived or while the
        report names another route."""
        planned = self._planned
        if planned is None or not self.is_connected:
            return None
        try:
            device = self._port.device()
        except Exception:
            return None
        if str(device.report_data.work.path_hash) != planned.path.path_hash:
            return None
        progress = mapping.path_progress_of(device, planned)
        return planned.path.model_copy(update={"progress": progress})

    async def preview_path(
        self, area_ids: Sequence[str], pattern: Mapping[str, Any] | None
    ) -> MowPath:
        """:class:`~mowdeck_control.PathPreviewCapable`: let the mower plan the route of a start
        without starting it (``MowPathSaga`` with planning; no ``start_job``).

        Refused while a job runs or for areas or settings the mower cannot run; raises
        :class:`~mowdeck_control.PathUnavailable` when no route arrived within
        ``PATH_PREVIEW_TIMEOUT_S``. One preview at a time: a second one waits for the first.
        """
        self._require_connected()
        self._require_no_sensor_stop()
        try:
            command = StartMow(area_ids=list(area_ids), pattern=dict(pattern) if pattern else None)
        except ValueError as exc:
            raise CommandRejected("choose at least one area") from exc
        device = self._port.device()
        activity = mapping.activity_of(device, has_report=self._port.has_report)
        if activity in ACTIVE_ACTIVITIES or mapping.path_job_running(device):
            msg = f"the mower is {activity.value}; a route is planned before a start"
            raise CommandRejected(msg)
        _plan, route = await self._route_for(command, device)
        async with self._preview_lock:
            try:
                confirmed = await self._port.fetch_mow_path(
                    route, plan=True, timeout_s=PATH_PREVIEW_TIMEOUT_S
                )
            except TimeoutError as exc:
                raise PathUnavailable(
                    "the mower did not hand back its route in time", timed_out=True
                ) from exc
            except (NoTransportAvailableError, PortClosedError) as exc:
                raise NotConnected("no usable link to the mower") from exc
            except MowPathFetchError as exc:
                raise PathUnavailable(str(exc)) from exc
            except TransportError as exc:
                raise PathUnavailable(f"transport error: {type(exc).__name__}") from exc
            planned = mapping.mow_path_of(
                self._port.device(), planned_at=self._clock(), complete_only=False
            )
        if planned is None:
            raise PathUnavailable("the mower planned no route for these areas")
        if confirmed and str(confirmed) != planned.path.path_hash:
            _log.warning(
                "route of %s confirmed as %s but its lines hash to %s",
                self._port.device_name,
                confirmed,
                planned.path.path_hash,
            )
        _log.info(
            "route preview of %s: %d lines, %d points",
            self._port.device_name,
            len(planned.path.polylines),
            planned.path.points_total,
        )
        return planned.path

    def _track_path(self) -> None:
        """Fetch the route of a running job in the background when the report names one the
        adapter does not hold (see the module docstring); forget it once the job ended."""
        try:
            device = self._port.device()
        except Exception:
            return
        if not mapping.path_job_running(device):
            if self._port.has_report:
                self._planned = None
            return
        path_hash = device.report_data.work.path_hash
        if path_hash <= 1:
            return
        wanted = str(path_hash)
        if self._planned is not None and self._planned.path.path_hash == wanted:
            return
        if self._path_task is not None and not self._path_task.done():
            return
        now = self._clock_s()
        tries = self._path_tries
        count = 1
        if tries is not None and tries[0] == wanted:
            if tries[1] >= PATH_ATTEMPTS or now - tries[2] < PATH_RETRY_S:
                return
            count = tries[1] + 1
        self._path_tries = (wanted, count, now)
        self._path_task = asyncio.create_task(
            self._fetch_path(wanted), name=f"mow-path-{self._port.device_name}"
        )

    async def _fetch_path(self, wanted: str) -> None:
        """One background fetch of the running job's route (``skip_planning``)."""
        name = self._port.device_name
        try:
            await self._port.fetch_mow_path(self._route, plan=False, timeout_s=PATH_FETCH_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning("mow path of %s not fetched: %s", name, exc)
            return
        try:
            planned = mapping.mow_path_of(self._port.device(), planned_at=self._clock())
        except Exception:
            _log.exception("cannot map the mow path of %s", name)
            return
        if planned is None or planned.path.path_hash != wanted:
            _log.info("mow path of %s incomplete or of another route", name)
            return
        self._planned = planned
        _log.info(
            "mow path of %s: %d lines, %d points",
            name,
            len(planned.path.polylines),
            planned.path.points_total,
        )
        if self._connected:
            self._publish(force=True)

    async def _end_job(self) -> None:
        """``cancel_job`` for a running or paused job, then wait until the mower left the job
        modes (a missing change within ``_PAUSED_JOB_CLEAR_S`` is not an error: the return
        follows anyway)."""
        _log.info("ending the job of %s before returning", self._port.device_name)
        await self._send("cancel_job")
        await self._port.wait_for(
            lambda d: d.report_data.dev.sys_status not in _JOB_MODES, _PAUSED_JOB_CLEAR_S
        )

    async def _set_light(self, *, on: bool) -> Outcome:
        if not mapping.has_light(self._port.device_name, self._port.product_key()):
            raise CommandRejected("this mower has no switchable light")
        await self._send(LIGHT_COMMAND, manual_ctrl=on)
        label = "on" if on else "off"

        def shows_wanted(device: MowerDevice) -> bool:
            return mapping.light_on_of(device, reported=True) is on

        if await self._read_light(Priority.USER) and await self._port.wait_for(
            shows_wanted, LIGHT_CONFIRM_S
        ):
            return Outcome.executed(f"light is {label}")
        return Outcome.accepted(f"light {label} sent; the mower did not confirm it")

    def _default_blade_height(self, device: MowerDevice) -> int:
        if self._blade_height_mm is not None:
            return self._blade_height_mm
        return mapping.blade_height_of(device) or DEFAULT_BLADE_HEIGHT_MM

    async def _emergency_stop(self) -> Outcome:
        steps: list[tuple[str, dict[str, Any]]] = [
            ("pause_execute_task", {}),
            ("send_movement", {"linear_speed": 0, "angular_speed": 0}),
            (
                "operate_on_device",
                {
                    "main_ctrl": 0,
                    "cut_knife_ctrl": 0,
                    "cut_knife_height": self._default_blade_height(self._port.device()),
                    "max_run_speed": 0.0,
                },
            ),
        ]
        sent: list[str] = []
        errors: list[str] = []
        for name, kwargs in steps:
            try:
                await self._send(name, priority=Priority.EMERGENCY, **kwargs)
                sent.append(name)
            except Exception as exc:  # every step is tried, whatever failed before
                _log.exception("emergency stop step %s failed", name)
                errors.append(f"{name}: {type(exc).__name__}")
        if not sent:
            if not self._port.link_up:
                raise NotConnected("no usable link to the mower for the emergency stop")
            return Outcome.failed("emergency stop not sent: " + "; ".join(errors))
        reason = "stop sent with emergency priority: " + ", ".join(sent)
        if errors:
            reason += " (failed: " + "; ".join(errors) + ")"
        return Outcome.accepted(reason)

    # --- state ----------------------------------------------------------------------------

    def _snapshot(self) -> MowerState:
        return mapping.state_of(
            self._port.device(),
            mower_id=self._config.mower_id,
            account_id=self._config.account_id,
            connection=self._connection_kind(),
            device_name=self._port.device_name,
            firmware=self._port.firmware(),
            observed_at=self._clock(),
            has_report=self._port.has_report,
            link=self._link_label(),
            model=self._port.model(),
            light_reported=self._light_reported,
            speed_setpoint_mps=self._speed_mps or DEFAULT_SPEED_MPS,
            links=self._link_statuses(),
            firmware_info=self._firmware.info,
        )

    async def _on_device_update(self) -> None:
        if self._connected:
            self._publish()

    def _publish(self, *, force: bool = False) -> None:
        try:
            self._track_firmware(self._port.device())
            state = self._snapshot()
        except Exception:
            _log.exception("cannot map the state of %s", self._config.mower_id)
            return
        previous = self._last_state
        self._last_state = state
        self._track_path()
        if (previous is None) or previous.activity is not state.activity:
            self._refresh_wake.set()  # the refresh interval depends on the activity
        now = self._publish_clock()
        stale = self._published_at is None or now - self._published_at >= UNCHANGED_REPUBLISH_S
        if force or previous is None or stale or _changed(previous, state):
            self._broadcaster.publish(state)
            self._published_at = now

    def _require_connected(self) -> None:
        if not self.is_connected:
            raise NotConnected("the mower is not connected")

    def _require_no_sensor_stop(self) -> None:
        state = self._snapshot()
        if state.activity is MowerActivity.ERROR:
            msg = f"the mower reports an error ({state.error_code}); clear it at the mower"
            raise SensorStop(msg)


BOOST_COMMANDS: Final = frozenset(
    {"start_job", "pause_execute_task", "resume_execute_task", "return_to_dock"}
)
"""Sent commands after which the refresh loop boosts (:meth:`MammotionBase.boost_refresh`)."""
BOOST_POLL_S: Final = 3.0
"""Seconds between undebounced report requests while boosted."""
BOOST_MIN_S: Final = 30.0
"""Shortest boost, seconds (it lasts at least one report stream window)."""
ACTIVE_ACTIVITIES: Final = frozenset(
    {MowerActivity.MOWING, MowerActivity.RETURNING, MowerActivity.PAUSED}
)
"""Activities that use the active refresh interval (a docked pause reports as docked)."""


def _changed(previous: MowerState, current: MowerState) -> bool:
    keep = {"observed_at"}
    return previous.model_dump(exclude=keep) != current.model_dump(exclude=keep)
