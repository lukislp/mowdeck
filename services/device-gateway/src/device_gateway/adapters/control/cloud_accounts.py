"""The Mammotion cloud of the gateway: one shared session per account (ADR 0034).

:class:`CloudBackend` bundles what the gateway needs from ``mowdeck-control-mammotion`` (the
only module of the service that imports that GPL-3.0 package directly; the connections of
the other kinds still come through the ``mowdeck-control`` registry):

* ``pool``: the :class:`~mowdeck_control_mammotion.session.CloudSessionPool`, one logged-in
  PyMammotion client per set of credentials. The cloud connections of all mowers of one linked
  account borrow it (each with its own port, state, refresh loop, camera and device information
  rounds), and the discovery reuses it, so a re-read of the account does not log its connected
  mowers out.
* ``factory``: the ``cloud`` connection factory on that pool.
* ``accounts``: :class:`MammotionCloudAccounts`, the discovery port.

``MOWDECK_FAKE_MAMMOTION_CLOUD`` (development and the browser tests only) swaps all three for
the fake cloud of :mod:`mowdeck_control_mammotion.fake_cloud`.
"""

from dataclasses import dataclass
from pathlib import Path

from mowdeck_contracts.v1 import CloudProvider
from mowdeck_control import ConnectionFactory
from mowdeck_control_mammotion.cloud import cloud_connection_factory
from mowdeck_control_mammotion.discovery import (
    CloudLoginRefused,
    CloudUnavailable,
    DeviceLister,
    discover_devices,
    registry_devices,
)
from mowdeck_control_mammotion.port import login_cloud_client
from mowdeck_control_mammotion.session import CloudSessionPool

from device_gateway.application.ports import (
    CloudDevices,
    CloudLoginError,
    CloudUnavailableError,
)


class MammotionCloudAccounts:
    """:class:`~device_gateway.application.ports.CloudAccounts` on the session pool."""

    def __init__(self, pool: CloudSessionPool, lister: DeviceLister = registry_devices) -> None:
        self._pool = pool
        self._lister = lister

    def supports(self, provider: CloudProvider) -> bool:
        """Only Mammotion so far."""
        return provider is CloudProvider.MAMMOTION

    async def discover(self, provider: CloudProvider, email: str, password: str) -> CloudDevices:
        """Log in (or reuse the open session) and list the devices of the account."""
        del provider
        try:
            found = await discover_devices(self._pool, email, password, lister=self._lister)
        except CloudLoginRefused as exc:
            raise CloudLoginError(str(exc)) from exc
        except CloudUnavailable as exc:
            raise CloudUnavailableError(str(exc)) from exc
        return CloudDevices(devices=found.devices, reused_session=found.reused_session)


@dataclass(frozen=True, slots=True)
class CloudBackend:
    """Session pool, connection factory and discovery of one cloud (real or fake)."""

    pool: CloudSessionPool
    factory: ConnectionFactory
    accounts: MammotionCloudAccounts
    fake: bool = False


def cloud_backend(*, fake: bool = False, cache_dir: Path | None = None) -> CloudBackend:
    """The real Mammotion cloud (a pool of this gateway with the credential caches in
    ``cache_dir``), or the fake one for development. The fake cloud module is imported only
    here, when it is selected, so a production gateway never loads it."""
    if fake:
        from mowdeck_control_mammotion.fake_cloud import (
            fake_cloud_connection_factory,
            fake_devices,
            fake_pool,
        )

        pool = fake_pool()
        return CloudBackend(
            pool=pool,
            factory=fake_cloud_connection_factory(pool),
            accounts=MammotionCloudAccounts(pool, fake_devices),
            fake=True,
        )
    pool = CloudSessionPool(login_cloud_client, cache_dir=cache_dir)
    return CloudBackend(
        pool=pool, factory=cloud_connection_factory(pool), accounts=MammotionCloudAccounts(pool)
    )
