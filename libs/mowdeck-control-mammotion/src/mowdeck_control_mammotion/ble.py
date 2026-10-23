"""BLE connection through an ESPHome Bluetooth Proxy (``ConnectionKind.BLE_BRIDGE``).

Needs the extra ``esphome``. The proxy (for example a XIAO ESP32-S3 with the stock ESPHome
``bluetooth_proxy``) is reached at ``host``:``port`` with the API encryption key ``noise_psk``;
the mower is found by its BLE MAC ``ble_address`` and registered in PyMammotion as a BLE only
device (``add_ble_only_device``, no cloud login).

Manual driving is offered over this link (:class:`~mowdeck_control_mammotion.drive.BleDriveSession`
on ``send_movement``). Blades while driving stay off unless the option ``allow_manual_blades``
is true. When the proxy or the BLE link drops, :attr:`is_connected` turns False and commands
raise :class:`~mowdeck_control.NotConnected`, so the Device-Gateway's backoff reconnects with a
new connection.
"""

import logging
from collections.abc import Callable
from datetime import datetime

from mowdeck_contracts.v1 import BASE_STATION_OPTION, ConnectionKind, MowerActivity, utc_now
from mowdeck_control import CommandRejected, ConnectionConfig, DriveSession

from mowdeck_control_mammotion import mapping
from mowdeck_control_mammotion.base import MammotionBase
from mowdeck_control_mammotion.drive import BleDriveSession, manual_run_speed
from mowdeck_control_mammotion.esphome import ProxyLink, open_proxy
from mowdeck_control_mammotion.options import BleProxyOptions
from mowdeck_control_mammotion.port import (
    ClientPort,
    DeviceHandle,
    MammotionClient,
    MammotionPort,
    open_ble_client,
)

_log = logging.getLogger("mowdeck_control_mammotion.ble")


class BleProxyPort(ClientPort):
    """:class:`ClientPort` whose client talks BLE through an ESPHome proxy."""

    def __init__(self, options: BleProxyOptions) -> None:
        super().__init__(self._open_link, on_close=self._close_link)
        self._options = options
        self._link: ProxyLink | None = None

    @property
    def proxy_alive(self) -> bool:
        """True while the ESPHome API connection is up."""
        return self._link is not None and self._link.is_alive()

    @property
    def is_open(self) -> bool:
        return super().is_open and self.proxy_alive and self.ble_connected()

    async def _open_link(self) -> tuple[MammotionClient, DeviceHandle]:
        options = self._options
        self._link = await open_proxy(
            host=options.host,
            port=options.port,
            noise_psk=options.noise_psk,
            ble_address=options.ble_address,
            scan_timeout_s=options.scan_timeout_s,
        )
        try:
            name = options.device_name or self._link.advertised_name
            if not name:
                msg = "the mower did not advertise a name; set the option 'device_name'"
                raise CommandRejected(msg)
            return await open_ble_client(device_name=name, ble_device=self._link.ble_device)
        except BaseException:
            # The client never opened, so nobody would close the proxy: release it here.
            await self._close_link()
            raise

    async def _close_link(self) -> None:
        link, self._link = self._link, None
        if link is not None:
            await link.close()
            _log.debug("proxy link of %s closed", self._options.ble_address)


class MammotionBleProxyConnection(MammotionBase):
    """A Mammotion mower over BLE through an ESPHome Bluetooth Proxy."""

    _kind = ConnectionKind.BLE_BRIDGE

    def __init__(
        self,
        port: MammotionPort,
        config: ConnectionConfig,
        options: BleProxyOptions,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        super().__init__(
            port,
            config,
            map_sync_timeout_s=options.map_sync_timeout_s,
            confirm_timeout_s=options.confirm_timeout_s,
            clock=clock,
            refresh_interval_s=options.refresh_seconds,
            active_refresh_interval_s=options.refresh_seconds_active,
            report_stream_s=options.report_stream_seconds,
        )
        self.options = options
        self._session: BleDriveSession | None = None

    def _manual_drive_offered(self) -> bool:
        return True

    def _manual_blades_allowed(self) -> bool:
        return self.options.allow_manual_blades

    def _link_alive(self) -> bool:
        return self._port.link_up

    def _link_label(self) -> str:
        return "ble_proxy" if self._port.link_up else "ble_proxy (link down)"

    async def start_drive_session(self) -> DriveSession:
        self._require_connected()
        self._require_no_sensor_stop()
        self.boost_refresh()
        if self._session is not None and self._session.is_open:
            await self._session.close()
        self._session = BleDriveSession(
            self._port,
            blades_allowed=self.options.allow_manual_blades,
            blade_height_mm=self.options.manual_blade_height_mm,
            sensor_stop=self._sensor_stop_reason,
            max_run_speed_mps=manual_run_speed(
                self.options.manual_drive_speed_mps, mapping.drive_speed_of(self._port.device())
            ),
        )
        await self._session.begin()
        return self._session

    def _drive_active(self) -> bool:
        """An open drive session switches the refresh loop to its ``drive`` mode (report window
        plus undebounced polls about once a second), so spot runs get a fresh pose."""
        return self._session is not None and self._session.is_open

    def _sensor_stop_reason(self) -> str | None:
        state = self._snapshot()
        if state.activity is MowerActivity.ERROR:
            return f"the mower reports an error ({state.error_code})"
        return None

    async def _before_close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()


def create_ble_bridge_connection(config: ConnectionConfig) -> MammotionBleProxyConnection:
    """Connection factory for ``ConnectionKind.BLE_BRIDGE`` (entry point group
    ``mowdeck.connections``). A linked RTK base station (option ``base_station``, ADR 0047) is
    ignored: Fleet links base stations to cloud mowers only."""
    options = BleProxyOptions.parse(
        {k: v for k, v in config.options.items() if k != BASE_STATION_OPTION}
    )
    return MammotionBleProxyConnection(BleProxyPort(options), config, options)
