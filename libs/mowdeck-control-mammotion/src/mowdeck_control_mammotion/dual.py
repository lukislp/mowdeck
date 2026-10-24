"""Dual link (ADR 0038): the Mammotion cloud plus a Bluetooth bridge next to the mower.

:class:`MammotionDualConnection` is a
:class:`~mowdeck_control_mammotion.cloud.MammotionCloudConnection` whose registration carries
a ``ble_bridge`` link (``config.options["links"]["ble_bridge"]``, the public link options of
Fleet merged with the withheld ``noise_psk`` by the Device-Gateway).

Link roles (ADR 0038): the cloud carries the state, the data requests, the fresh-state polling
and every command but the drive channel, like the camera. The bridge has two roles:

* ``drive``: manual driving and spot runs (both hold a drive session). The drive frames and
  the fast report polling of the open session go over Bluetooth.
* ``fast_position``: while the mower is mowing or returning (and the link option
  ``fast_position`` is on, the default) the bridge also carries PyMammotion's continuous
  Bluetooth report stream, so positions arrive about once a second. This comes on top of the
  cloud polling, never instead of it.

While docked or idle no role needs Bluetooth: the bridge only holds the ESPHome proxy link and
reports the link status and the mower's RSSI from its advertisements; it does not poll and the
mower keeps advertising (other bridge scans still see it).

:class:`BridgeWatcher` keeps the proxy open (:func:`~mowdeck_control_mammotion.esphome.open_proxy`,
reconnecting with its own backoff, 5 s doubling to 60 s, independent of the cloud session) and
attaches the mower's BLE transport to the cloud device handle only while a role needs it
(:meth:`~mowdeck_control_mammotion.port.MammotionPort.attach_bridge`), removing it again when no
role is left (:meth:`~mowdeck_control_mammotion.port.MammotionPort.detach_bridge`). PyMammotion
would send everything over a connected BLE transport; the port therefore routes cloud first
(:func:`~mowdeck_control_mammotion.port.bridge_route`): only the drive channel and the report
polling of an open drive session use the bridge.

The state reports ``connection`` ``ble_bridge`` while Bluetooth carries traffic, ``cloud``
otherwise, and ``links["ble_bridge"]`` with ``connected`` while the proxy link is held, the
bridge's firmware (``esphome_version``, ``project_version``, ``outdated``) and the mower's RSSI
at the bridge; every link change publishes a state.

What stays the cloud's: ``is_connected`` (a lost bridge does not end the connection, a dead
cloud login does), the camera, the firmware service.

Which link drives (ADR 0049, amending ADR 0038): the bridge when its proxy link is up, else the
vendor's cloud remote drive when the mower offers it
(:meth:`~mowdeck_control_mammotion.cloud.MammotionCloudConnection.cloud_drive_available`). The
choice is made when a drive session opens and never changes while it runs:
:meth:`MammotionDualConnection.start_drive_session` connects Bluetooth on demand while the proxy is
up and raises ``NotConnected`` when Bluetooth does not come up (no fallback then: the
capabilities promised reverse); with the proxy down it opens a cloud session, or raises
``NotConnected`` (bridge not connected) when the cloud cannot drive either, so a spot run or a
calibration ends ``link_lost``. An open Bluetooth session ends when Bluetooth drops: its frames
never fall back to the cloud; the next session may then use the cloud. The capabilities follow
the link a new session would use: ``manual_drive`` (bridge or cloud), ``drive_reverse`` (bridge
only) and ``manual_drive_blades`` (the bridge link's ``allow_manual_blades``, else the cloud
connection's). A bridge scan
(:meth:`MammotionDualConnection.scan_bridge`) for the bridge this connection holds is answered
from the live scanner, so a "re-check" never opens a second API client to the proxy.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any, Final, Protocol

from mowdeck_contracts.v1 import (
    LINK_BLE_BRIDGE,
    BridgeScanReply,
    ConnectionKind,
    LinkStatus,
    MowerActivity,
    ProxyInfo,
    utc_now,
)
from mowdeck_control import ConnectionConfig, DriveSession, NotConnected

from mowdeck_control_mammotion import mapping
from mowdeck_control_mammotion.base import MIN_DRIVE_POLL_S
from mowdeck_control_mammotion.cloud import (
    CLOUD_MIN_DRIVE_POLL_S,
    BridgeScanner,
    MammotionCloudConnection,
)
from mowdeck_control_mammotion.drive import BleDriveSession, manual_run_speed
from mowdeck_control_mammotion.esphome import ProxyLink, open_proxy, proxy_info_of, scan_reply_from
from mowdeck_control_mammotion.options import BleProxyOptions, OptionsError
from mowdeck_control_mammotion.port import MammotionPort

_log = logging.getLogger("mowdeck_control_mammotion.dual")

BRIDGE_NOT_CONNECTED: Final = "the Bluetooth bridge is not connected"
BLE_NOT_UP: Final = "the Bluetooth connection through the bridge did not come up"
BRIDGE_BACKOFF_INITIAL_S: Final = 5.0
BRIDGE_BACKOFF_MAX_S: Final = 60.0
BRIDGE_POLL_S: Final = 2.0
"""How often the watcher checks the proxy link and the roles (attach or detach Bluetooth)."""
BLE_ATTACH_TIMEOUT_S: Final = 45.0
"""Longest wait of :meth:`MammotionDualConnection.start_drive_session` for Bluetooth."""
FAST_POSITION_ACTIVITIES: Final = frozenset({MowerActivity.MOWING, MowerActivity.RETURNING})
"""Activities in which the ``fast_position`` role attaches Bluetooth."""


class ProxyOpener(Protocol):
    """Opens the proxy and finds the mower
    (:func:`~mowdeck_control_mammotion.esphome.open_proxy`)."""

    def __call__(
        self, *, host: str, port: int, noise_psk: str, ble_address: str, scan_timeout_s: float
    ) -> Awaitable[ProxyLink]: ...


def link_options_of(raw: Mapping[str, Any]) -> BleProxyOptions:
    """The bridge link options of a registration (``links["ble_bridge"]``) as
    :class:`BleProxyOptions`; Fleet's ``credentials`` markers are ignored."""
    options = {k: v for k, v in raw.items() if not k.startswith("credentials")}
    return BleProxyOptions.parse(options)


def _never() -> bool:
    return False


class BridgeWatcher:
    """Keeps the Bluetooth bridge link of one mower: holds the proxy, attaches Bluetooth while
    ``wanted()`` says a role needs it, detaches it when not, reconnects the proxy.

    ``on_change(carrying)`` is awaited after every change of the proxy link or of the Bluetooth
    attachment (``carrying``: Bluetooth carries traffic now); the connection publishes a state
    and ends an open drive session when Bluetooth is gone. The watcher never touches the cloud
    session.
    """

    def __init__(
        self,
        port: MammotionPort,
        options: BleProxyOptions,
        *,
        on_change: Callable[[bool], Awaitable[None]],
        wanted: Callable[[], bool] = _never,
        opener: ProxyOpener = open_proxy,
        clock: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        initial_backoff_s: float = BRIDGE_BACKOFF_INITIAL_S,
        max_backoff_s: float = BRIDGE_BACKOFF_MAX_S,
        poll_s: float = BRIDGE_POLL_S,
    ) -> None:
        self._port = port
        self._options = options
        self._on_change = on_change
        self._wanted = wanted
        self._opener = opener
        self._clock = clock
        self._sleep = sleep
        self._initial_backoff_s = initial_backoff_s
        self._max_backoff_s = max_backoff_s
        self._poll_s = poll_s
        self.link: ProxyLink | None = None
        self.proxy: ProxyInfo | None = None
        """What the bridge said about itself when the link opened last."""
        self.since: datetime | None = None
        """When the link opened or was lost last."""
        self.last_error: str | None = None
        """The exception type of the last failed open or attach (never the key)."""
        self.attempts = 0
        self.attached = False
        """True while the mower's BLE transport is attached through the bridge."""
        self._task: asyncio.Task[None] | None = None

    @property
    def proxy_up(self) -> bool:
        """True while the proxy link is open (the bridge holds the link)."""
        return self.link is not None and self.link.is_alive()

    @property
    def connected(self) -> bool:
        """True while Bluetooth through the bridge is attached and connected."""
        return self.attached and self.proxy_up and self._port.ble_connected()

    def holds(self, host: str, port: int) -> bool:
        """True while this watcher holds an open link to the proxy at ``host``:``port``."""
        link = self.link
        return (
            link is not None
            and link.is_alive()
            and link.host.casefold() == host.casefold()
            and (link.port == port or not link.port)
        )

    def start(self, name: str) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name=f"bridge-{name}")

    async def stop(self) -> None:
        """Cancel the task and close an open link (the BLE transport is detached)."""
        task, self._task = self._task, None
        try:
            if task is not None:
                task.cancel()
                await asyncio.wait({task})
                if not task.cancelled() and (error := task.exception()) is not None:
                    _log.debug("bridge watcher had failed: %r", error)
        finally:
            await self._drop(notify=False)

    async def run(self) -> None:
        """Open the proxy, follow the roles while it stays up, reconnect with backoff."""
        delay = self._initial_backoff_s
        options = self._options
        while True:
            self.attempts += 1
            try:
                link = await self._opener(
                    host=options.host,
                    port=options.port,
                    noise_psk=options.noise_psk,
                    ble_address=options.ble_address,
                    scan_timeout_s=options.scan_timeout_s,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                _log.info(
                    "bridge %s not opened (%s), next try in %.0f s",
                    options.host,
                    self.last_error,
                    delay,
                )
                await self._sleep(delay)
                delay = min(delay * 2, self._max_backoff_s)
                continue
            self.link = link
            self.proxy = proxy_info_of(link.device_info, checked_at=self._clock())
            self.since = self._clock()
            self.last_error = None
            delay = self._initial_backoff_s
            _log.info(
                "bridge %s (%s, ESPHome %s%s) holds the link to %s",
                link.proxy_name,
                options.host,
                self.proxy.esphome_version,
                ", outdated" if self.proxy.outdated else "",
                options.ble_address,
            )
            await self._on_change(False)
            await self._follow_roles(link)
            _log.warning("bridge link to %s lost", options.host)
            await self._drop(notify=True)
            await self._sleep(delay)
            delay = min(delay * 2, self._max_backoff_s)

    async def _follow_roles(self, link: ProxyLink) -> None:
        """While the proxy is up: attach Bluetooth when a role needs it (retrying with its own
        backoff), detach it when none does or when it dropped."""
        attach_delay = self._initial_backoff_s
        while link.is_alive():
            wanted = self._wanted()
            if self.attached and (not wanted or not self._port.ble_connected()):
                if wanted:
                    _log.warning("Bluetooth through bridge %s lost", self._options.host)
                await self._detach()
                await self._on_change(False)
            elif wanted and not self.attached:
                try:
                    await self._port.attach_bridge(link)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = type(exc).__name__
                    _log.warning(
                        "BLE through bridge %s not attached (%s), next try in %.0f s",
                        self._options.host,
                        self.last_error,
                        attach_delay,
                    )
                    await self._sleep(attach_delay)
                    attach_delay = min(attach_delay * 2, self._max_backoff_s)
                    continue
                self.attached = True
                self.last_error = None
                attach_delay = self._initial_backoff_s
                _log.info("bridge %s carries Bluetooth to the mower", self._options.host)
                await self._on_change(True)
            await self._sleep(self._poll_s)

    async def _detach(self) -> None:
        self.attached = False
        with contextlib.suppress(Exception):
            await self._port.detach_bridge()

    async def _drop(self, *, notify: bool) -> None:
        link, self.link = self.link, None
        if self.attached:
            await self._detach()
        if link is None:
            return
        self.since = self._clock()
        with contextlib.suppress(Exception):
            await link.close()
        if notify:
            await self._on_change(False)


class MammotionDualConnection(MammotionCloudConnection):
    """A Mammotion mower through the cloud with a Bluetooth bridge as its second link."""

    def __init__(
        self,
        port: MammotionPort,
        config: ConnectionConfig,
        options: Any,
        link: BleProxyOptions,
        *,
        clock: Callable[[], datetime] = utc_now,
        opener: ProxyOpener = open_proxy,
        scanner: BridgeScanner | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_s: float = BRIDGE_POLL_S,
        attach_timeout_s: float = BLE_ATTACH_TIMEOUT_S,
    ) -> None:
        super().__init__(port, config, options, clock=clock, scanner=scanner)
        self.link_options = link
        self._session: BleDriveSession | None = None
        self._drive_requested = False
        self._attach_timeout_s = attach_timeout_s
        self._watcher = BridgeWatcher(
            port,
            link,
            on_change=self._on_bridge_change,
            wanted=self.bridge_wanted,
            opener=opener,
            clock=clock,
            sleep=sleep,
            poll_s=poll_s,
        )
        port.route_bridge(self._ble_drive_active)

    @property
    def watcher(self) -> BridgeWatcher:
        """The bridge watcher (diagnostics and tests)."""
        return self._watcher

    # --- link roles -------------------------------------------------------------------------

    def bridge_roles(self) -> frozenset[str]:
        """The roles that need Bluetooth through the bridge right now: ``drive`` (a drive
        session is open or being opened) and ``fast_position`` (mowing or returning)."""
        roles: set[str] = set()
        if self._drive_requested or self._ble_drive_active():
            roles.add("drive")
        state = self._last_state
        if (
            self.link_options.fast_position
            and state is not None
            and state.activity in FAST_POSITION_ACTIVITIES
        ):
            roles.add("fast_position")
        return frozenset(roles)

    def bridge_wanted(self) -> bool:
        """True when a role needs Bluetooth (the watcher attaches it)."""
        return self._connected and bool(self.bridge_roles())

    # --- lifecycle --------------------------------------------------------------------------

    async def connect(self) -> None:
        await super().connect()
        if self._connected:
            self._watcher.start(self._port.device_name)

    async def _before_close(self) -> None:
        session, self._session = self._session, None
        try:
            if session is not None:
                await session.close()
            await self._close_cloud_session()
        finally:
            await self._watcher.stop()

    async def _on_bridge_change(self, carrying: bool) -> None:
        if not carrying:
            session, self._session = self._session, None
            if session is not None:
                await session.close()
        if self._connected:
            self._publish(force=True)

    # --- hooks --------------------------------------------------------------------------------

    def ble_connected(self) -> bool:
        """True while Bluetooth through the bridge carries traffic (a role is active)."""
        return self._connected and self._watcher.connected

    def _manual_drive_offered(self) -> bool:
        if not self._connected:
            return False
        return self._watcher.proxy_up or self.cloud_drive_available()

    def _manual_blades_allowed(self) -> bool:
        if self._watcher.proxy_up:
            return self.link_options.allow_manual_blades
        return self.options.allow_manual_blades

    def _drive_reverse_offered(self) -> bool:
        return self._watcher.proxy_up

    def _min_drive_poll_s(self) -> float:
        return MIN_DRIVE_POLL_S if self._watcher.proxy_up else CLOUD_MIN_DRIVE_POLL_S

    def _ble_drive_active(self) -> bool:
        return self._session is not None and self._session.is_open

    def _drive_active(self) -> bool:
        return self._ble_drive_active() or super()._drive_active()

    def _mode(self) -> str:
        mode = super()._mode()
        # Bluetooth streams the positions while it is attached; the cloud keeps polling at the
        # active interval instead of holding its own report window.
        return "poll" if mode == "stream" and self.ble_connected() else mode

    def _connection_kind(self) -> ConnectionKind:
        return ConnectionKind.BLE_BRIDGE if self.ble_connected() else ConnectionKind.CLOUD

    def _link_label(self) -> str:
        return LINK_BLE_BRIDGE if self.ble_connected() else super()._link_label()

    def _link_statuses(self) -> dict[str, LinkStatus]:
        held = self._connected and self._watcher.proxy_up
        link = self._watcher.link
        rssi = self._port.ble_rssi() if self.ble_connected() else None
        if rssi is None and held and link is not None:
            rssi = link.rssi()  # from the mower's advertisements at the bridge
        return {
            LINK_BLE_BRIDGE: mapping.link_status_of(
                self._watcher.proxy, connected=held, since=self._watcher.since, rssi=rssi
            )
        }

    # --- manual driving ---------------------------------------------------------------------

    async def start_drive_session(self) -> DriveSession:
        self._require_connected()
        if not self._watcher.proxy_up:
            if not (self.options.cloud_drive and self.cloud_drive_available()):
                raise NotConnected(BRIDGE_NOT_CONNECTED)
            if self._session is not None:
                session, self._session = self._session, None
                await session.close()
            return await self._start_cloud_drive()
        self._require_no_sensor_stop()
        await self._close_cloud_session()
        if self._session is not None and self._session.is_open:
            await self._session.close()
        self._drive_requested = True
        try:
            await self._wait_for_bluetooth()
            self.boost_refresh()
            self._session = BleDriveSession(
                self._port,
                blades_allowed=self.link_options.allow_manual_blades,
                blade_height_mm=self.link_options.manual_blade_height_mm,
                sensor_stop=self._sensor_stop_reason,
                link_up=self.ble_connected,
                max_run_speed_mps=manual_run_speed(
                    self.link_options.manual_drive_speed_mps,
                    mapping.drive_speed_of(self._port.device()),
                ),
            )
            await self._session.begin()
        finally:
            self._drive_requested = False
        return self._session

    async def _wait_for_bluetooth(self) -> None:
        """Wait until the watcher attached Bluetooth for the ``drive`` role."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._attach_timeout_s
        while not self.ble_connected():
            if loop.time() >= deadline or not self._watcher.proxy_up:
                raise NotConnected(BLE_NOT_UP)
            await asyncio.sleep(0.05)

    # --- bridge scan ------------------------------------------------------------------------

    async def scan_bridge(
        self,
        *,
        host: str,
        port: int,
        secret: str,
        expected_name: str | None,
        timeout_s: float,
    ) -> BridgeScanReply:
        """A scan through the bridge this connection holds is answered from its live scanner
        (a re-check); any other bridge is scanned like on a plain cloud connection."""
        link = self._watcher.link
        if link is not None and self._watcher.holds(host, port):
            proxy = proxy_info_of(link.device_info, checked_at=self._clock())
            self._watcher.proxy = proxy
            return scan_reply_from(
                proxy,
                link.seen_devices(),
                expected_name,
                detail=f"{expected_name} is not advertising at {proxy.proxy_name} right now",
            )
        return await super().scan_bridge(
            host=host, port=port, secret=secret, expected_name=expected_name, timeout_s=timeout_s
        )


def dual_link_options(options: Mapping[str, Any]) -> BleProxyOptions | None:
    """The ``ble_bridge`` link of ``options["links"]`` as :class:`BleProxyOptions`, None when
    the registration has no bridge link. Raises :class:`OptionsError` for a broken one."""
    links = options.get("links")
    if not isinstance(links, Mapping):
        return None
    raw = links.get(LINK_BLE_BRIDGE)
    if not isinstance(raw, Mapping):
        return None
    try:
        return link_options_of(raw)
    except OptionsError as exc:
        msg = f"link {LINK_BLE_BRIDGE}: {exc}"
        raise OptionsError(msg) from exc
