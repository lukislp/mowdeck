"""A fake Mammotion cloud for development and the browser tests (ADR 0034).

The Device-Gateway uses it instead of the real cloud when ``MOWDECK_FAKE_MAMMOTION_CLOUD`` is
set (development only). Every account below :data:`FAKE_CLOUD_DOMAIN` exists and takes
:data:`FAKE_CLOUD_PASSWORD`; any other password is refused like a wrong one at Mammotion, any
other address is unknown. Each account holds :data:`FAKE_DEVICES`: two mowers (one online, one
offline) and an RTK base station. A cloud mower registered on such an account connects to a
:class:`~mowdeck_control_mammotion.fake.FakeMammotionPort` (a docked LUBA 2 AWD 5000X with the
sample map) through the same shared session pool as the real cloud, so "two mowers, one login"
holds here too.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from mowdeck_contracts.v1 import DiscoveredDevice
from mowdeck_control import ConnectionConfig

from mowdeck_control_mammotion.cloud import MammotionCloudConnection
from mowdeck_control_mammotion.fake import SAMPLE_PRODUCT_KEY, FakeMammotionPort
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
    """A :class:`FakeMammotionPort` that holds a lease on the fake account's shared session."""

    def __init__(self, pool: CloudSessionPool, options: CloudOptions) -> None:
        super().__init__(device_name=options.device_name or FAKE_DEVICES[0].device_name)
        self._pool = pool
        self._options = options
        self._lease: SessionLease | None = None

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
    """A ``ConnectionKind.CLOUD`` factory on the fake cloud."""

    def create(config: ConnectionConfig) -> MammotionCloudConnection:
        options = CloudOptions.parse(config.options)
        return MammotionCloudConnection(PooledFakePort(pool, options), config, options)

    return create
