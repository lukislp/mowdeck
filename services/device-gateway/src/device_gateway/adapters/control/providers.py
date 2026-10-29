"""The cloud providers of the gateway (ADR 0066): plugins from ``mowdeck.cloud_providers``.

Every vendor whose mowers are reached through a cloud registers a ``ProviderSpec`` in its own
adapter package; the gateway never imports a vendor package by name. :func:`cloud_providers`
loads the enabled ones (``ProviderSpec.enabled_by_default`` plus
``MOWDECK_CLOUD_PROVIDERS_ENABLE`` minus ``MOWDECK_CLOUD_PROVIDERS_DISABLE``) with the fake
clouds when ``MOWDECK_FAKE_CLOUDS`` is set (development and the browser tests only).

:class:`PluginCloudAccounts` is the discovery port on those plugins; the connection provider
routes ``cloud`` registrations to them (:mod:`device_gateway.adapters.control.connections`).
"""

from collections.abc import Mapping
from pathlib import Path

from mowdeck_contracts.v1 import ProviderDescriptor
from mowdeck_control import (
    CloudLoginRefused,
    CloudUnavailable,
    LoadedProviders,
    ProviderContext,
    load_providers,
)
from mowdeck_service_kit import get_logger

from device_gateway.application.ports import (
    CloudDevices,
    CloudLoginError,
    CloudUnavailableError,
)

_log = get_logger(__name__)


def cloud_providers(
    *,
    fake: bool = False,
    cache_dir: Path | None = None,
    enable: tuple[str, ...] = (),
    disable: tuple[str, ...] = (),
) -> LoadedProviders:
    """Create the plugins of the enabled, installed providers."""
    loaded = load_providers(
        ProviderContext(fake=fake, cache_dir=cache_dir), enable=enable, disable=disable
    )
    for descriptor in loaded.descriptors():
        _log.info(
            "cloud provider enabled",
            provider=descriptor.provider,
            tested_on_hardware=descriptor.tested_on_hardware,
            fake=fake,
        )
    return loaded


class PluginCloudAccounts:
    """:class:`~device_gateway.application.ports.CloudAccounts` on the loaded plugins."""

    def __init__(self, providers: LoadedProviders) -> None:
        self._providers = providers

    def descriptors(self) -> list[ProviderDescriptor]:
        """The enabled providers, sorted by display name."""
        return self._providers.descriptors()

    def descriptor(self, provider: str) -> ProviderDescriptor | None:
        """The descriptor of an enabled provider."""
        plugin = self._providers.plugins.get(provider)
        return plugin.descriptor if plugin is not None else None

    async def discover(self, provider: str, credentials: Mapping[str, str]) -> CloudDevices:
        """Log in through the plugin and list the devices."""
        plugin = self._providers.plugins.get(provider)
        if plugin is None:
            msg = f"the provider {provider} is not enabled"
            raise CloudUnavailableError(msg)
        try:
            found = await plugin.discover(credentials)
        except CloudLoginRefused as exc:
            raise CloudLoginError(str(exc)) from exc
        except CloudUnavailable as exc:
            raise CloudUnavailableError(str(exc)) from exc
        except Exception as exc:
            # A plugin bug must not answer with its message (it may echo the request).
            _log.warning(
                "cloud provider discovery failed", provider=provider, error=type(exc).__name__
            )
            msg = f"the {provider} cloud failed ({type(exc).__name__})"
            raise CloudUnavailableError(msg) from exc
        return CloudDevices(devices=list(found.devices), reused_session=found.reused_session)
