"""The Husqvarna cloud as a MowDeck cloud provider (ADR 0065).

:class:`HusqvarnaProvider` owns one :class:`~mowdeck_control_husqvarna.pool.AccountPool` per
gateway: the discovery of a linked account and the connections of all its mowers share one
session (one token, one websocket) per application key, which keeps the account within
Husqvarna's rate limits.

With ``ProviderContext.fake`` the pool opens its sessions against
:mod:`~mowdeck_control_husqvarna.fake_cloud` (application keys ``fake-husqvarna-...``, secret
``fake-cloud-password``); that module is imported only then.
"""

from collections.abc import Mapping
from typing import Final

from mowdeck_contracts.v1 import ProviderDescriptor
from mowdeck_control import (
    CloudLoginRefused,
    ConnectionConfig,
    MowerConnection,
    ProviderContext,
    ProviderDevices,
)

from mowdeck_control_husqvarna import mapping
from mowdeck_control_husqvarna.account import HusqvarnaAccount, open_account
from mowdeck_control_husqvarna.connection import HusqvarnaConnection
from mowdeck_control_husqvarna.descriptor import APP_KEY, APP_SECRET, DESCRIPTOR
from mowdeck_control_husqvarna.options import HusqvarnaOptions
from mowdeck_control_husqvarna.pool import AccountOpener, AccountPool
from mowdeck_control_husqvarna.transport import AiohttpTransport

MISSING_CREDENTIALS: Final = "the application key and the application secret are required"


class HusqvarnaProvider:
    """:class:`~mowdeck_control.CloudProviderPlugin` on a shared account pool."""

    def __init__(self, pool: AccountPool) -> None:
        self.pool = pool

    @property
    def descriptor(self) -> ProviderDescriptor:
        """:data:`~mowdeck_control_husqvarna.descriptor.DESCRIPTOR`."""
        return DESCRIPTOR

    async def discover(self, credentials: Mapping[str, str]) -> ProviderDevices:
        """Log in (or reuse the account's open session) and list its mowers."""
        app_key = credentials.get(APP_KEY, "").strip()
        secret = credentials.get(APP_SECRET, "")
        if not app_key or not secret:
            raise CloudLoginRefused(MISSING_CREDENTIALS)
        lease = await self.pool.acquire(app_key, secret)
        try:
            devices = mapping.discovered_devices(lease.account.mowers)
        finally:
            await lease.release()
        return ProviderDevices(devices=devices, reused_session=lease.reused)

    def create_connection(self, config: ConnectionConfig) -> MowerConnection:
        """The connection of one mower (credentials and mower id in the options)."""
        return HusqvarnaConnection(self.pool, config, HusqvarnaOptions.parse(config.options))

    async def close(self) -> None:
        """Stop every shared session."""
        await self.pool.close()


def real_opener() -> AccountOpener:
    """Sessions against the real Husqvarna cloud (one aiohttp session each)."""

    async def open_real(app_key: str, app_secret: str) -> HusqvarnaAccount:
        return await open_account(HusqvarnaAccount(AiohttpTransport(), app_key, app_secret))

    return open_real


def create_provider(context: ProviderContext) -> HusqvarnaProvider:
    """The plugin for ``context``: the real cloud or the fake one (no session cache: a
    client-credentials token is cheap to get and lives only in memory)."""
    if context.fake:
        from mowdeck_control_husqvarna.fake_cloud import FakeHusqvarnaCloud

        return HusqvarnaProvider(AccountPool(FakeHusqvarnaCloud().opener()))
    return HusqvarnaProvider(AccountPool(real_opener()))
