"""The Worx cloud as a MowDeck cloud provider (ADR 0066).

:class:`WorxProvider` owns one :class:`~mowdeck_control_worx.pool.AccountPool` per gateway: the
discovery of a linked account and the connections of all its mowers share one session (one
login, one MQTT link) per account, behind the pool's login guard.

With ``ProviderContext.fake`` the pool opens its sessions at
:mod:`~mowdeck_control_worx.fake_cloud` (accounts ``*@fake.mowdeck.test``, password
``fake-cloud-password``, any brand); that module is imported only then, and pyworxcloud only for
the real cloud.
"""

from collections.abc import Mapping

from mowdeck_contracts.v1 import ProviderDescriptor
from mowdeck_control import (
    CloudLoginRefused,
    ConnectionConfig,
    MowerConnection,
    ProviderContext,
    ProviderDevices,
)

from mowdeck_control_worx import mapping
from mowdeck_control_worx.connection import WorxConnection
from mowdeck_control_worx.descriptor import DESCRIPTOR
from mowdeck_control_worx.options import WorxOptions, login_of
from mowdeck_control_worx.pool import AccountPool
from mowdeck_control_worx.session import SessionOpener, WorxLogin, WorxSession


class WorxProvider:
    """:class:`~mowdeck_control.CloudProviderPlugin` on a shared account pool."""

    def __init__(self, pool: AccountPool) -> None:
        self.pool = pool

    @property
    def descriptor(self) -> ProviderDescriptor:
        """:data:`~mowdeck_control_worx.descriptor.DESCRIPTOR`."""
        return DESCRIPTOR

    async def discover(self, credentials: Mapping[str, str]) -> ProviderDevices:
        """Log in (or reuse the account's open session) and list its mowers."""
        try:
            login = login_of(credentials)
        except ValueError as exc:
            raise CloudLoginRefused(str(exc)) from None
        lease = await self.pool.acquire(login)
        try:
            devices = mapping.discovered_devices(lease.session.mowers())
        finally:
            await lease.release()
        return ProviderDevices(devices=devices, reused_session=lease.reused)

    def create_connection(self, config: ConnectionConfig) -> MowerConnection:
        """The connection of one mower (credentials and serial number in the options)."""
        return WorxConnection(self.pool, config, WorxOptions.parse(config.options))

    async def close(self) -> None:
        """Stop every shared session."""
        await self.pool.close()


def real_opener() -> SessionOpener:
    """Sessions at the real cloud, through pyworxcloud (imported here, not at module load)."""

    async def open_real(login: WorxLogin) -> WorxSession:
        from mowdeck_control_worx.pyworx_session import PyworxSession

        return await PyworxSession.open(login)

    return open_real


def create_provider(context: ProviderContext) -> WorxProvider:
    """The plugin for ``context``: the real cloud or the fake one. No session cache: the
    library keeps its tokens in memory, one login per account and gateway start."""
    if context.fake:
        from mowdeck_control_worx.fake_cloud import FakeWorxCloud

        return WorxProvider(AccountPool(FakeWorxCloud().opener()))
    return WorxProvider(AccountPool(real_opener()))
