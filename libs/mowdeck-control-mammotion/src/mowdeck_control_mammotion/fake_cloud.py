"""A fake Mammotion cloud for development and the browser tests (ADR 0034).

The Device-Gateway uses it instead of the real cloud when ``MOWDECK_FAKE_MAMMOTION_CLOUD`` is
set (development only). Every account below :data:`FAKE_CLOUD_DOMAIN` exists and takes
:data:`FAKE_CLOUD_PASSWORD`; any other password is refused like a wrong one at Mammotion, any
other address is unknown. Each account holds :data:`FAKE_DEVICES`: two mowers (one online, one
offline) and an RTK base station. A cloud mower registered on such an account connects to a
:class:`~mowdeck_control_mammotion.fake.FakeMammotionPort` (a docked LUBA 2 AWD 5000X with the
sample map) through the same shared session pool as the real cloud, so "two mowers, one login"
holds here too.

Links and firmware (ADR 0038, for the browser tests): the fake vendor offers every mower the
firmware :data:`~mowdeck_control_mammotion.fake.SAMPLE_NEWER_FIRMWARE` (``upgradeable``), and
a started update progresses to completion by itself within :data:`FAKE_OTA_SECONDS`. A fake
bridge scan (:func:`fake_scan_proxy`) answers any host with the account's online mower at
-62 dBm (a host containing ``unreachable`` is ``unreachable``, a key that is not 32 base64
bytes ``auth_failed``); a mower registered with a ``ble_bridge`` link gets a dual connection
whose bridge "connects" at once (:func:`fake_open_proxy`).
"""

import base64
import binascii
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from mowdeck_contracts.v1 import (
    BridgeScanError,
    BridgeScanErrorCode,
    BridgeScanReply,
    DiscoveredDevice,
    ScannedDevice,
)
from mowdeck_control import ConnectionConfig

from mowdeck_control_mammotion.cloud import MammotionCloudConnection, split_link_options
from mowdeck_control_mammotion.dual import MammotionDualConnection, dual_link_options
from mowdeck_control_mammotion.esphome import (
    BRIDGE_PROJECT_NAME,
    BRIDGE_TEMPLATE_VERSION,
    ProxyLink,
    proxy_info_of,
    scan_reply_from,
)
from mowdeck_control_mammotion.fake import SAMPLE_PRODUCT_KEY, FakeMammotionPort, firmware_offer
from mowdeck_control_mammotion.options import CloudOptions
from mowdeck_control_mammotion.port import AuthError
from mowdeck_control_mammotion.session import CloudSessionPool, SessionLease

FAKE_CLOUD_DOMAIN: Final = "fake.mowdeck.test"
"""E-mail domain of the fake accounts, e.g. ``garden@fake.mowdeck.test``."""
FAKE_CLOUD_PASSWORD: Final = "fake-cloud-password"  # noqa: S105 - a public test value
"""The password every fake account takes."""

FAKE_DEVICES: Final = (
    DiscoveredDevice(
        device_name="Luba-FAKE0001",
        iot_id="fakeiot0001",
        product_key=SAMPLE_PRODUCT_KEY,
        model="LUBA 2 AWD 5000X",
        online=True,
        is_mower=True,
    ),
    DiscoveredDevice(
        device_name="Luba-FAKE0002",
        iot_id="fakeiot0002",
        product_key=SAMPLE_PRODUCT_KEY,
        model="LUBA 2 AWD 5000X",
        online=False,
        is_mower=True,
    ),
    DiscoveredDevice(
        device_name="RTK-FAKE0001",
        iot_id="fakeiot0003",
        product_key=None,
        model="RTK",
        online=True,
        is_mower=False,
    ),
)


FAKE_OTA_SECONDS: Final = 8.0
"""Real seconds a fake firmware update takes from the request to the new version."""
FAKE_BRIDGE_NAME: Final = "mowdeck-bridge"
FAKE_BRIDGE_ESPHOME: Final = "2026.1.0"
FAKE_BRIDGE_MAC: Final = "AA:BB:CC:DD:EE:01"
FAKE_MOWER_ADDRESS: Final = "FC:23:45:67:89:01"
"""The Bluetooth address the fake bridge sees ``Luba-FAKE0001`` under."""
FAKE_MOWER_RSSI: Final = -62


@dataclass(slots=True)
class FakeDeviceInfo:
    """What the fake bridge answers to ESPHome's ``device_info``."""

    name: str = FAKE_BRIDGE_NAME
    esphome_version: str = FAKE_BRIDGE_ESPHOME
    compilation_time: str = "Jan 1 2026, 00:00:00"
    project_name: str = BRIDGE_PROJECT_NAME
    project_version: str = BRIDGE_TEMPLATE_VERSION
    bluetooth_mac_address: str = FAKE_BRIDGE_MAC
    mac_address: str = FAKE_BRIDGE_MAC


def fake_seen_devices() -> list[ScannedDevice]:
    """The mower advertisements the fake bridge sees: the account's online mower."""
    return [
        ScannedDevice(
            name=FAKE_DEVICES[0].device_name, address=FAKE_MOWER_ADDRESS, rssi=FAKE_MOWER_RSSI
        )
    ]


def _key_ok(noise_psk: str) -> bool:
    try:
        return len(base64.b64decode(noise_psk, validate=True)) == 32
    except (binascii.Error, ValueError):
        return False


async def fake_scan_proxy(
    *, host: str, port: int, noise_psk: str, expected_name: str | None, timeout_s: float
) -> BridgeScanReply:
    """The fake bridge scan (the ``scanner`` of the fake cloud's connections)."""
    del timeout_s
    if "unreachable" in host.casefold():
        return BridgeScanReply(
            error=BridgeScanError(
                code=BridgeScanErrorCode.UNREACHABLE, detail=f"proxy {host}:{port}: no route"
            )
        )
    if not _key_ok(noise_psk):
        return BridgeScanReply(
            error=BridgeScanError(
                code=BridgeScanErrorCode.AUTH_FAILED, detail=f"proxy {host}:{port}: key refused"
            )
        )
    return scan_reply_from(
        proxy_info_of(FakeDeviceInfo()),
        fake_seen_devices(),
        expected_name,
        detail=f"{expected_name} did not advertise at {FAKE_BRIDGE_NAME}",
    )


@dataclass(slots=True)
class _FakeLinkState:
    alive: bool = True


async def fake_open_proxy(
    *, host: str, port: int, noise_psk: str, ble_address: str, scan_timeout_s: float
) -> ProxyLink:
    """The fake proxy opener: the bridge is reachable and sees the mower at once."""
    del scan_timeout_s
    if "unreachable" in host.casefold():
        raise ConnectionError("no route to the fake bridge")
    if not _key_ok(noise_psk):
        raise PermissionError("the fake bridge refused the key")
    state = _FakeLinkState()

    async def close() -> None:
        state.alive = False

    return ProxyLink(
        ble_device=None,
        advertised_name=FAKE_DEVICES[0].device_name,
        proxy_name=FAKE_BRIDGE_NAME,
        close=close,
        is_alive=lambda: state.alive,
        device_info=FakeDeviceInfo(),
        scanner=None,
        host=host,
        port=port,
        address=ble_address.upper(),
        _rssi=FAKE_MOWER_RSSI,
    )


class FakeLoginError(AuthError):
    """The fake cloud refused the credentials."""


class FakeCloudClient:
    """The logged-in "client" of a fake account (what the pool holds)."""

    def __init__(self, account: str) -> None:
        self.account = account
        self.devices = list(FAKE_DEVICES)
        self.stopped = False
        self.reauth_required: str | None = None

    async def stop(self) -> None:
        """Stop the fake session."""
        self.stopped = True


def is_fake_account(account: str) -> bool:
    """True for an e-mail below :data:`FAKE_CLOUD_DOMAIN`."""
    return account.strip().casefold().endswith(f"@{FAKE_CLOUD_DOMAIN}")


async def fake_login(account: str, password: str, cache_path: Path | None) -> FakeCloudClient:
    """The login function of the fake pool."""
    del cache_path
    if not is_fake_account(account) or password != FAKE_CLOUD_PASSWORD:
        raise FakeLoginError("login refused")
    return FakeCloudClient(account)


def fake_devices(client: Any) -> list[DiscoveredDevice]:
    """The device lister of the fake cloud."""
    if not isinstance(client, FakeCloudClient):
        msg = "not a fake cloud session"
        raise TypeError(msg)
    return list(client.devices)


def fake_pool(*, linger_seconds: float = 120.0) -> CloudSessionPool:
    """A session pool on the fake cloud."""
    return CloudSessionPool(fake_login, linger_seconds=linger_seconds)


class PooledFakePort(FakeMammotionPort):
    """A :class:`FakeMammotionPort` that holds a lease on the fake account's shared session;
    the fake vendor offers it a firmware update that installs by itself when started."""

    def __init__(self, pool: CloudSessionPool, options: CloudOptions) -> None:
        name = options.device_name or FAKE_DEVICES[0].device_name
        super().__init__(device_name=name)
        self._pool = pool
        self._options = options
        self._lease: SessionLease | None = None
        listed = next((d for d in FAKE_DEVICES if d.device_name == name), None)
        self.firmware_check = firmware_offer(iot_id=(listed.iot_id if listed else None) or "")
        self.firmware_update_seconds = FAKE_OTA_SECONDS
        self.rssi = FAKE_MOWER_RSSI

    async def open(self) -> None:
        if self._lease is None:
            lease = await self._pool.acquire(self._options.account, self._options.password)
            listed = {d.device_name: d for d in fake_devices(lease.client)}
            found = listed.get(self.device_name)
            if found is None or not found.is_mower:
                await lease.release()
                msg = f"device {self.device_name} is not a mower on this account"
                raise LookupError(msg)
            self._lease = lease
        await super().open()

    async def close(self) -> None:
        await super().close()
        lease, self._lease = self._lease, None
        if lease is not None:
            await lease.release()


def fake_cloud_connection_factory(
    pool: CloudSessionPool,
) -> Callable[[ConnectionConfig], MammotionCloudConnection]:
    """A ``ConnectionKind.CLOUD`` factory on the fake cloud (with the fake bridge for a
    registration that carries a ``ble_bridge`` link)."""

    def create(config: ConnectionConfig) -> MammotionCloudConnection:
        plain, _links = split_link_options(config.options)
        options = CloudOptions.parse(plain)
        link = dual_link_options(config.options)
        port = PooledFakePort(pool, options)
        if link is not None:
            return MammotionDualConnection(
                port, config, options, link, opener=fake_open_proxy, scanner=fake_scan_proxy
            )
        return MammotionCloudConnection(port, config, options, scanner=fake_scan_proxy)

    return create
