"""Build a provider's connection on its fake vendor client, wrap it in the controller, send.

The connection is built the way the Device-Gateway builds it: from the cloud provider plugin
(``ProviderContext(fake=True)``, discovery with the fake credentials, ``create_connection`` with
the device and the credentials in the options), or from the package's own factory. Every command
of the checks goes through :class:`~mowdeck_control.MowerController`, so the checks see what a
user sees: the controller's rules plus the adapter's answer.
"""

import asyncio
import inspect
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandResult,
    CommandStatus,
    DiscoveredDevice,
    MowerCommand,
    PrincipalKind,
    PrincipalRef,
)
from mowdeck_control import (
    CLOUD_PROVIDER_OPTION,
    CloudProviderPlugin,
    ConnectionConfig,
    MowerConnection,
    MowerController,
    ProviderContext,
    ProviderSpec,
)
from mowdeck_control.providers import installed_specs

from mowdeck_provider_testkit.package import CLOUD_PROVIDERS, InstalledPackage
from mowdeck_provider_testkit.subject import ProviderUnderTest

OK: Final = frozenset({CommandStatus.EXECUTED, CommandStatus.ACCEPTED})
"""A command the mower took: confirmed, or sent without a confirmation."""

ISSUER: Final = PrincipalRef(subject=UUID(int=0x6D6F776465636B), kind=PrincipalKind.USER)

SPIED_METHODS: Final = (
    "execute",
    "dispatch",
    "start_drive_session",
    "set_setting",
    "run_maintenance",
    "leave_dock",
    "set_drive_speed",
    "start_firmware_update",
    "start_base_station_firmware_update",
)
"""Methods through which a command reaches the adapter."""


def provider_specs(package: InstalledPackage) -> dict[str, ProviderSpec]:
    """The package's cloud provider specs by id (loaded by ``mowdeck_control``'s own loader)."""
    return installed_specs(package.source(CLOUD_PROVIDERS))


def wrong_credentials(
    credentials: Mapping[str, str], secret_keys: frozenset[str]
) -> dict[str, str]:
    """``credentials`` with every secret changed: what a refused login looks like."""
    return {k: (f"{v}-not-it" if k in secret_keys else v) for k, v in credentials.items()}


def spy_on(connection: object) -> list[str] | None:
    """Record every call that hands a command to ``connection`` (its method names, ``execute``
    and ``dispatch`` with the command kind). The connection's class is swapped for a subclass
    that only adds the recording, so ``isinstance`` checks of the optional protocols still see
    the original methods. None when the class cannot be swapped (an extension type)."""
    calls: list[str] = []
    base = type(connection)

    def recorder(name: str) -> Any:
        original = getattr(base, name)

        if name in {"execute", "dispatch"}:

            async def with_kind(self: Any, command: Any, *args: Any, **kwargs: Any) -> Any:
                calls.append(f"{name}:{command.kind}")
                return await original(self, command, *args, **kwargs)

            return with_kind

        async def plain(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            return await original(self, *args, **kwargs)

        return plain

    namespace: dict[str, Any] = {"__slots__": ()}
    for name in SPIED_METHODS:
        if inspect.iscoroutinefunction(getattr(base, name, None)):
            namespace[name] = recorder(name)
    try:
        spy = type(f"Spied{base.__name__}", (base,), namespace)
        connection.__class__ = spy
    except TypeError:
        return None
    return calls


@dataclass
class MowerUnderTest:
    """One connection on the fake vendor client and its controller (started on demand)."""

    connection: MowerConnection
    config: ConnectionConfig
    subject: ProviderUnderTest
    plugin: CloudProviderPlugin | None = None
    device: DiscoveredDevice | None = None
    calls: list[str] | None = None
    controller: MowerController = field(init=False)
    _started: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.controller = MowerController(self.connection, mower_id=self.config.mower_id)

    async def start(self) -> MowerController:
        """Connect through the controller (capabilities, map, state pump) once."""
        if not self._started:
            await asyncio.wait_for(self.controller.start(), self.subject.command_timeout_s)
            self._started = True
        return self.controller

    async def send(self, command: MowerCommand) -> CommandResult:
        """``command`` through the started controller, bounded by the subject's timeout."""
        controller = await self.start()
        envelope = CommandEnvelope.create(
            mower_id=self.config.mower_id,
            account_id=self.config.account_id,
            issued_by=ISSUER,
            command=command,
        )
        return await asyncio.wait_for(controller.handle(envelope), self.subject.command_timeout_s)

    async def close(self) -> None:
        """Close the controller (it disconnects) and the plugin."""
        try:
            with suppress(Exception):
                await asyncio.wait_for(self.controller.close(), self.subject.command_timeout_s)
        finally:
            if self.plugin is not None:
                with suppress(Exception):
                    await self.plugin.close()


def _config(subject: ProviderUnderTest, options: Mapping[str, Any]) -> ConnectionConfig:
    return ConnectionConfig(
        mower_id=uuid4(), account_id=uuid4(), options={**options, **subject.mower_options}
    )


async def _from_factory(subject: ProviderUnderTest) -> MowerUnderTest:
    assert subject.connection_factory is not None  # noqa: S101 - checked by the caller
    config = _config(subject, {})
    built = subject.connection_factory(config)
    connection = await built if inspect.isawaitable(built) else built
    return MowerUnderTest(connection=connection, config=config, subject=subject)


async def _from_provider(
    subject: ProviderUnderTest, package: InstalledPackage, device_filter: Any
) -> MowerUnderTest:
    specs = provider_specs(package)
    if not specs or subject.fake_credentials is None:
        pytest.skip(
            "no way to build a connection: give ProviderUnderTest.fake_credentials (cloud "
            "provider) or connection_factory"
        )
    provider, spec = sorted(specs.items())[0]
    plugin = spec.create(ProviderContext(fake=True))
    try:
        credentials = plugin.descriptor.normalized(subject.fake_credentials)
        found = await asyncio.wait_for(plugin.discover(credentials), subject.command_timeout_s)
        device = next((d for d in found.devices if device_filter(d)), None)
        if device is None:
            pytest.skip("the fake account lists no device of the requested kind")
        options: dict[str, Any] = {
            **credentials,
            "device_name": device.device_name,
            CLOUD_PROVIDER_OPTION: provider,
        }
        if device.iot_id is not None:
            options["iot_id"] = device.iot_id
        config = _config(subject, options)
        connection = plugin.create_connection(config)
    except BaseException:
        await plugin.close()
        raise
    return MowerUnderTest(
        connection=connection, config=config, subject=subject, plugin=plugin, device=device
    )


@asynccontextmanager
async def open_mower(
    subject: ProviderUnderTest, package: InstalledPackage, *, device_filter: Any = None
) -> AsyncIterator[MowerUnderTest]:
    """A :class:`MowerUnderTest` on the fake client, closed afterwards. ``device_filter``
    replaces ``subject.pick_device`` (the offline-mower check picks another device)."""
    if subject.connection_factory is not None and device_filter is None:
        mower = await _from_factory(subject)
    else:
        mower = await _from_provider(subject, package, device_filter or subject.pick_device)
    mower.calls = spy_on(mower.connection)
    try:
        yield mower
    finally:
        await mower.close()
