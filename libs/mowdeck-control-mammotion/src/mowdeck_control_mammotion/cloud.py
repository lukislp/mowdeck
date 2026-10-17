"""Mammotion cloud connection (``ConnectionKind.CLOUD``): PyMammotion's MQTT transport.

Logs in with the account from the options (use a second account with the mower shared to it,
spike 0001 section 3.3), selects the mower by ``device_name`` or ``iot_id`` and talks to it
through the Mammotion MQTT broker (``MQTTTransport``) or, for pre-2025 devices, Aliyun
(``AliyunMQTTTransport``); PyMammotion picks the transport from the device list.

Manual driving is off: PyMammotion's cloud ``RemoteDriveSession`` has never been run against a
mower (its own documentation says so) and the owner's firmware (1.30.29.26) is below the
1.30.31.19 threshold that restores movement over Wi-Fi. Joystick driving needs the BLE bridge.

Live camera (ADR 0032): :meth:`MammotionCloudConnection.get_camera_session` asks the Mammotion
cloud for an Agora stream token (``get_stream_subscription``, which also makes the mower
publish) and the 4G video budget (``get_video_resource``, best effort) and returns them as a
:class:`~mowdeck_contracts.v1.CameraSession`. The browser joins the channel itself; no media
passes through MowDeck. The token is a secret and is never logged. The mower keeps publishing
after the last viewer left; :meth:`MammotionCloudConnection.release_camera_session`
(:class:`~mowdeck_control.CameraSessionRelease`, ADR 0045) tells it to stop (PyMammotion's
``stop_stream``, Agora ``vi_switch=0``).

Fresh state: the Mammotion broker forwards reports only on request, so the connection asks
for a fresh report every ``refresh_seconds`` (option, 10 to 300 s, default 20; every
``refresh_seconds_active``, 2 to 60 s, default 5, while mowing, returning or paused; see
:class:`~mowdeck_control_mammotion.base.MammotionBase`). PyMammotion's own 600 sends per 12 h
quota applies only to firmware below 1.30.25.1 (the move to the Mammotion MQTT broker); the
owner's 1.30.29.26 is past it, and PyMammotion skips the request when a report arrived within
15 s anyway. A server side 429 ban still applies to everyone, so do not go below 10 s.

A transient MQTT loss does not end the connection: PyMammotion reconnects by itself and commands
meanwhile raise :class:`~mowdeck_control.NotConnected`. Only a terminal login failure
(``reauth_required``) makes :attr:`is_connected` False, so the Device-Gateway does not log in
again on every network hiccup (ban risk R1).

Shared session (ADR 0034): the mowers of one Mammotion account share one logged-in client from
a :class:`~mowdeck_control_mammotion.session.CloudSessionPool` (one login per account instead
of one per mower); each connection selects its own device handle and keeps its own state,
camera, refresh loop and device information rounds. Closing a connection releases its share;
the last one stops the client.

Firmware and links (ADR 0038): the cloud connection is the
:class:`~mowdeck_control.FirmwarePort` of a Mammotion mower (the vendor's version check and OTA
request go over the account's HTTPS session) and can look for the mower through a Bluetooth
bridge (:class:`~mowdeck_control.BridgeScanCapable`, ESPHome scan in
:mod:`mowdeck_control_mammotion.esphome`); it declares ``ble_bridge`` in
``capabilities.links_supported``. A registration whose options carry such a link gets a
:class:`~mowdeck_control_mammotion.dual.MammotionDualConnection` from the factory.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime
from functools import partial
from typing import Any, Final, Protocol, cast

from mowdeck_contracts.v1 import (
    LINK_BLE_BRIDGE,
    BridgeScanError,
    BridgeScanErrorCode,
    BridgeScanReply,
    CameraSession,
    ConnectionKind,
    FirmwareInfo,
    utc_now,
)
from mowdeck_control import ConnectionConfig, NotConnected

from mowdeck_control_mammotion import mapping
from mowdeck_control_mammotion.base import MammotionBase
from mowdeck_control_mammotion.esphome import EsphomeUnavailable, scan_proxy
from mowdeck_control_mammotion.options import CloudOptions
from mowdeck_control_mammotion.port import (
    ClientPort,
    MammotionClient,
    MammotionPort,
    MowerDevice,
    NoTransportAvailableError,
    OpenedClient,
    PortClosedError,
    VideoResourceResponse,
    login_cloud_client,
    select_handle,
)
from mowdeck_control_mammotion.session import CloudSessionPool

_log = logging.getLogger("mowdeck_control_mammotion.cloud")

CAMERA_REQUEST_TIMEOUT_S: Final = 10.0
"""Longest wait for the stream token (PyMammotion retries an empty answer once)."""
VIDEO_RESOURCE_TIMEOUT_S: Final = 3.0


class BridgeScanner(Protocol):
    """Scans through a Bluetooth bridge (:func:`~mowdeck_control_mammotion.esphome.scan_proxy`
    or a fake)."""

    async def __call__(
        self,
        *,
        host: str,
        port: int,
        noise_psk: str,
        expected_name: str | None,
        timeout_s: float,
    ) -> BridgeScanReply: ...


class MammotionCloudConnection(MammotionBase):
    """A LUBA (or other Mammotion mower) through the Mammotion cloud."""

    _kind = ConnectionKind.CLOUD

    def __init__(
        self,
        port: MammotionPort,
        config: ConnectionConfig,
        options: CloudOptions,
        *,
        clock: Callable[[], datetime] = utc_now,
        scanner: BridgeScanner | None = None,
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
        self._scanner: BridgeScanner = scanner or scan_proxy

    def _link_label(self) -> str:
        return "cloud" if self._port.link_up else "cloud (no usable transport)"

    def _camera_offered(self) -> bool:
        return True

    def _firmware_offered(self) -> bool:
        return True

    def _links_supported(self) -> list[str]:
        return [LINK_BLE_BRIDGE]

    # --- firmware (FirmwarePort, ADR 0038) ----------------------------------------------------

    async def check_firmware(self) -> FirmwareInfo | None:
        """Ask the Mammotion cloud what is installed and offered (``get_device_ota_firmware``);
        the answer lands in ``MowerState.firmware_info`` and is published."""
        return await self._check_firmware()

    async def start_firmware_update(self, version: str) -> None:
        """Ask the Mammotion cloud to install ``version`` (``start_ota_upgrade``) after the
        safety rules; raises :class:`~mowdeck_control.CommandRejected` otherwise. Returns as
        soon as the request went out; the state follows the update."""
        self._require_connected()
        await self._firmware_update(version)

    # --- bridge scan (BridgeScanCapable, ADR 0038) ------------------------------------------

    async def scan_bridge(
        self,
        *,
        host: str,
        port: int,
        secret: str,
        expected_name: str | None,
        timeout_s: float,
    ) -> BridgeScanReply:
        """Look for the mower through the bridge at ``host``:``port`` (ESPHome native API with
        ``secret`` as the encryption key); never raises for a bridge problem."""
        try:
            return await self._scanner(
                host=host,
                port=port,
                noise_psk=secret,
                expected_name=expected_name,
                timeout_s=timeout_s,
            )
        except EsphomeUnavailable as exc:
            return BridgeScanReply(
                error=BridgeScanError(code=BridgeScanErrorCode.UNSUPPORTED, detail=str(exc)[:500])
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning("bridge scan of %s failed: %s", host, type(exc).__name__)
            return BridgeScanReply(
                error=BridgeScanError(
                    code=BridgeScanErrorCode.UNREACHABLE,
                    detail=f"scan through {host}:{port} failed: {type(exc).__name__}"[:500],
                )
            )

    async def get_camera_session(self) -> CameraSession | None:
        """A fresh Agora session for the mower's camera; None for models without a camera.

        Raises :class:`~mowdeck_control.NotConnected` when the cloud gives no stream token.
        """
        self._require_connected()
        device_name = self._port.device_name
        if not mapping.has_camera(device_name, self._port.product_key()):
            return None
        try:
            async with asyncio.timeout(CAMERA_REQUEST_TIMEOUT_S):
                subscription = await self._port.stream_subscription(
                    all_cameras=self.options.all_cameras
                )
        except (NoTransportAvailableError, PortClosedError) as exc:
            raise NotConnected("the Mammotion cloud session is not open") from exc
        except TimeoutError as exc:
            raise NotConnected("the Mammotion cloud did not send a stream token in time") from exc
        except Exception as exc:
            # The error text may echo the request; only the type is logged.
            _log.warning("stream token request for %s failed: %s", device_name, type(exc).__name__)
            raise NotConnected("the Mammotion cloud refused the stream token request") from exc
        if subscription is None:
            raise NotConnected("the Mammotion cloud sent no stream token")
        session = mapping.camera_session_of(
            subscription, now=self._clock(), video=await self._video_resource()
        )
        _log.info(
            "camera session for %s: %d camera(s), encrypted=%s, expires %s",
            device_name,
            len(session.cameras),
            session.encryption is not None,
            session.expires_at.isoformat(),
        )
        return session

    async def release_camera_session(self, channel: str | None = None) -> None:
        """:class:`~mowdeck_control.CameraSessionRelease`: tell the mower to stop publishing
        its camera stream (PyMammotion's ``stop_stream``, Agora ``vi_switch=0``).

        The stop is per mower, so ``channel`` is not needed. Idempotent: the command is sent
        every time and is harmless without a running stream; models without a camera send
        nothing. Raises :class:`~mowdeck_control.NotConnected` when it could not be sent.
        """
        del channel  # one stream per mower: the stop needs no channel
        self._require_connected()
        device_name = self._port.device_name
        if not mapping.has_camera(device_name, self._port.product_key()):
            return
        try:
            async with asyncio.timeout(CAMERA_REQUEST_TIMEOUT_S):
                await self._port.stop_stream()
        except (NoTransportAvailableError, PortClosedError) as exc:
            raise NotConnected("the Mammotion cloud session is not open") from exc
        except TimeoutError as exc:
            _log.warning("camera stream stop for %s timed out", device_name)
            raise NotConnected("the camera stream stop was not sent in time") from exc
        except Exception as exc:
            _log.warning("camera stream stop for %s failed: %s", device_name, type(exc).__name__)
            raise NotConnected("the camera stream stop failed") from exc
        _log.info("camera stream of %s released", device_name)

    async def _video_resource(self) -> VideoResourceResponse | None:
        try:
            async with asyncio.timeout(VIDEO_RESOURCE_TIMEOUT_S):
                return await self._port.video_resource()
        except Exception as exc:  # the budget is informational only
            _log.info("no video budget for %s: %s", self._port.device_name, type(exc).__name__)
            return None


SETTLE_SECONDS: Final = 3.0
"""Wait after a fresh login so MQTT subscribes and the first report arrives."""

_DEFAULT_POOL: CloudSessionPool | None = None


def default_pool() -> CloudSessionPool:
    """The process wide session pool of the real Mammotion cloud (created on first use)."""
    global _DEFAULT_POOL
    if _DEFAULT_POOL is None:
        _DEFAULT_POOL = CloudSessionPool(login_cloud_client)
    return _DEFAULT_POOL


async def open_pooled_cloud_client(
    pool: CloudSessionPool,
    *,
    account: str,
    password: str,
    device_name: str | None,
    iot_id: str | None,
    settle_seconds: float = SETTLE_SECONDS,
) -> OpenedClient:
    """Borrow the account's shared client from ``pool`` and select the mower.

    A fresh login waits ``settle_seconds`` for MQTT; a reused session does not. The device is
    asked for a report either way. The lease is released again when the mower is not on the
    account or is not a mower.
    """
    lease = await pool.acquire(account, password)
    try:
        client = cast(MammotionClient, lease.client)
        handle = select_handle(client, device_name=device_name, iot_id=iot_id)
        if not isinstance(handle.snapshot.raw, MowerDevice):
            msg = f"{handle.device_name} is not a mower"
            raise LookupError(msg)
        if not lease.reused:
            await asyncio.sleep(settle_seconds)
        with contextlib.suppress(Exception):
            await client.refresh_status(handle.device_name)
    except BaseException:
        await lease.release()
        raise
    return OpenedClient(client, handle, lease.release)


def cloud_port(options: CloudOptions, pool: CloudSessionPool | None = None) -> ClientPort:
    """The real PyMammotion port for ``options`` on the shared session of its account (the
    pool's credential cache directory replaces the per-mower ``session_cache_path``)."""

    return ClientPort(
        partial(
            open_pooled_cloud_client,
            pool or default_pool(),
            account=options.account,
            password=options.password,
            device_name=options.device_name,
            iot_id=options.iot_id,
        )
    )


def split_link_options(options: Any) -> tuple[dict[str, Any], Any]:
    """``(connection options, links)``: the ``links`` entry of a registration's options is not
    a cloud option (the Device-Gateway copies ``MowerRegistration.links`` into it)."""
    plain = dict(options)
    return plain, plain.pop("links", None)


def cloud_connection_factory(
    pool: CloudSessionPool,
) -> Callable[[ConnectionConfig], MammotionCloudConnection]:
    """A ``ConnectionKind.CLOUD`` factory whose connections share sessions from ``pool``; a
    registration with a ``ble_bridge`` link gets a
    :class:`~mowdeck_control_mammotion.dual.MammotionDualConnection`."""

    def create(config: ConnectionConfig) -> MammotionCloudConnection:
        from mowdeck_control_mammotion.dual import MammotionDualConnection, dual_link_options

        plain, _links = split_link_options(config.options)
        options = CloudOptions.parse(plain)
        link = dual_link_options(config.options)
        port = cloud_port(options, pool)
        if link is not None:
            return MammotionDualConnection(port, config, options, link)
        return MammotionCloudConnection(port, config, options)

    return create


def create_cloud_connection(config: ConnectionConfig) -> MammotionCloudConnection:
    """Connection factory for ``ConnectionKind.CLOUD`` (entry point ``mowdeck.connections``);
    mowers of the same Mammotion account share one session (:func:`default_pool`)."""
    return cloud_connection_factory(default_pool())(config)
