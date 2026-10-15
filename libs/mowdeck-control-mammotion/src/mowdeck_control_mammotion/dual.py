"""Dual link (ADR 0038): the Mammotion cloud plus a Bluetooth bridge next to the mower.

:class:`MammotionDualConnection` is a
:class:`~mowdeck_control_mammotion.cloud.MammotionCloudConnection` whose registration carries
a ``ble_bridge`` link (``config.options["links"]["ble_bridge"]``, the public link options of
Fleet merged with the withheld ``noise_psk`` by the Device-Gateway).
It owns a :class:`BridgeWatcher` task that opens the ESPHome proxy
(:func:`~mowdeck_control_mammotion.esphome.open_proxy`), attaches the mower's BLE transport to
the cloud device handle (:meth:`~mowdeck_control_mammotion.port.MammotionPort.attach_bridge`:
PyMammotion's ``add_ble_to_device`` plus ``connect_ble`` and ``prefer_ble``) and reconnects with
its own backoff (5 s doubling to 60 s), independent of the cloud session.

Preference and fallback are PyMammotion's own: a command goes over BLE while it is connected,
else over MQTT (and the handle keeps using BLE while MQTT reconnects). The state reports
``connection`` ``ble_bridge`` while the bridge carries the link, ``cloud`` otherwise, and
``links["ble_bridge"]`` with the bridge's firmware (``esphome_version``, ``project_version``,
``outdated``) and the mower's RSSI at the bridge; every link change publishes a state.

What stays the cloud's: ``is_connected`` (a lost bridge does not end the connection, a dead
cloud login does; both links down means ``NotConnected`` on commands), the camera, the firmware
service. What needs the bridge: manual driving (``capabilities.manual_drive`` follows the
bridge, :meth:`MammotionDualConnection.start_drive_session` refuses without it, and an open
session ends when the bridge drops: a frame never falls back to the cloud). A bridge scan
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
from mowdeck_control import CommandRejected, ConnectionConfig, DriveSession

from mowdeck_control_mammotion import mapping
from mowdeck_control_mammotion.cloud import BridgeScanner, MammotionCloudConnection
from mowdeck_control_mammotion.drive import BleDriveSession
from mowdeck_control_mammotion.esphome import ProxyLink, open_proxy, proxy_info_of, scan_reply_from
from mowdeck_control_mammotion.options import BleProxyOptions, OptionsError
from mowdeck_control_mammotion.port import MammotionPort

_log = logging.getLogger("mowdeck_control_mammotion.dual")

BRIDGE_NOT_CONNECTED: Final = "the Bluetooth bridge is not connected"
BRIDGE_BACKOFF_INITIAL_S: Final = 5.0
BRIDGE_BACKOFF_MAX_S: Final = 60.0
BRIDGE_POLL_S: Final = 2.0
"""How often an open bridge link is checked for a drop (and the RSSI refreshed)."""


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


class BridgeWatcher:
    """Keeps the Bluetooth bridge link of one mower open: connect, attach, watch, reconnect.

    ``on_change(connected)`` is awaited after every attach and loss (the connection publishes
    a state and ends an open drive session). The watcher never touches the cloud session.
    """

    def __init__(
        self,
        port: MammotionPort,
        options: BleProxyOptions,
        *,
        on_change: Callable[[bool], Awaitable[None]],
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
        """The exception type of the last failed open (never the key)."""
        self.attempts = 0
        self._task: asyncio.Task[None] | None = None

    @property
    def connected(self) -> bool:
        """True while the proxy link is open and the BLE transport connected."""
        return self.link is not None and self.link.is_alive() and self._port.ble_connected()

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
        """Open, attach and watch the bridge until cancelled; reconnect with backoff."""
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
            try:
                await self._port.attach_bridge(link)
            except asyncio.CancelledError:
                await link.close()
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                _log.warning(
                    "BLE through bridge %s not attached (%s), next try in %.0f s",
                    options.host,
                    self.last_error,
                    delay,
                )
                await link.close()
                await self._sleep(delay)
                delay = min(delay * 2, self._max_backoff_s)
                continue
            self.link = link
            self.proxy = proxy_info_of(link.device_info, checked_at=self._clock())
            self.since = self._clock()
            self.last_error = None
            delay = self._initial_backoff_s
            _log.info(
                "bridge %s (%s, ESPHome %s%s) carries %s",
                link.proxy_name,
                options.host,
                self.proxy.esphome_version,
                ", outdated" if self.proxy.outdated else "",
                options.ble_address,
            )
            await self._on_change(True)
            while self.connected:
                await self._sleep(self._poll_s)
            _log.warning("bridge link to %s lost", options.host)
            await self._drop(notify=True)
            await self._sleep(delay)
            delay = min(delay * 2, self._max_backoff_s)

    async def _drop(self, *, notify: bool) -> None:
        link, self.link = self.link, None
        if link is None:
            return
        self.since = self._clock()
        with contextlib.suppress(Exception):
            await self._port.detach_bridge()
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
    ) -> None:
        super().__init__(port, config, options, clock=clock, scanner=scanner)
        self.link_options = link
        self._session: BleDriveSession | None = None
        self._watcher = BridgeWatcher(
            port,
            link,
            on_change=self._on_bridge_change,
            opener=opener,
            clock=clock,
            sleep=sleep,
            poll_s=poll_s,
        )

    @property
    def watcher(self) -> BridgeWatcher:
        """The bridge watcher (diagnostics and tests)."""
        return self._watcher

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
        finally:
            await self._watcher.stop()

    async def _on_bridge_change(self, connected: bool) -> None:
        if not connected:
            session, self._session = self._session, None
            if session is not None:
                await session.close()
        if self._connected:
            self._publish(force=True)

    # --- hooks --------------------------------------------------------------------------------

    def ble_connected(self) -> bool:
        """True while the bridge carries the mower's BLE link."""
        return self._connected and self._watcher.connected

    def _manual_drive_offered(self) -> bool:
        return self.ble_connected()

    def _manual_blades_allowed(self) -> bool:
        return self.link_options.allow_manual_blades

    def _drive_active(self) -> bool:
        return self._session is not None and self._session.is_open

    def _connection_kind(self) -> ConnectionKind:
        return ConnectionKind.BLE_BRIDGE if self.ble_connected() else ConnectionKind.CLOUD

    def _link_label(self) -> str:
        return LINK_BLE_BRIDGE if self.ble_connected() else super()._link_label()

    def _link_statuses(self) -> dict[str, LinkStatus]:
        connected = self.ble_connected()
        return {
            LINK_BLE_BRIDGE: mapping.link_status_of(
                self._watcher.proxy,
                connected=connected,
                since=self._watcher.since,
                rssi=self._port.ble_rssi() if connected else None,
            )
        }

    # --- manual driving ---------------------------------------------------------------------

    async def start_drive_session(self) -> DriveSession:
        self._require_connected()
        if not self.ble_connected():
            raise CommandRejected(BRIDGE_NOT_CONNECTED)
        self._require_no_sensor_stop()
        self.boost_refresh()
        if self._session is not None and self._session.is_open:
            await self._session.close()
        self._session = BleDriveSession(
            self._port,
            blades_allowed=self.link_options.allow_manual_blades,
            blade_height_mm=self.link_options.manual_blade_height_mm,
            sensor_stop=self._sensor_stop_reason,
            link_up=self.ble_connected,
        )
        return self._session

    def _sensor_stop_reason(self) -> str | None:
        state = self._snapshot()
        if state.activity is MowerActivity.ERROR:
            return f"the mower reports an error ({state.error_code})"
        return None

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
