"""The entry point object of the Husqvarna provider (``mowdeck.cloud_providers``: ``husqvarna``).

Importable without aioautomower: the gateway loads every installed spec, also of disabled
providers, so the vendor SDK is imported only in :func:`create`, when the provider is enabled.
Off by default (``enabled_by_default=False``): nobody tested it on a real Husqvarna mower yet;
operators enable it with ``MOWDECK_CLOUD_PROVIDERS_ENABLE=husqvarna``.
"""

from typing import Final

from mowdeck_contracts.v1 import CapabilityProfile
from mowdeck_control import CloudProviderPlugin, ProviderContext, ProviderSpec

from mowdeck_control_husqvarna.descriptor import PROVIDER


def create(context: ProviderContext) -> CloudProviderPlugin:
    """The plugin for ``context`` (the real Husqvarna cloud, or the fake one)."""
    from mowdeck_control_husqvarna.provider import create_provider

    return create_provider(context)


def capability_profiles() -> list[CapabilityProfile]:
    """The model families (ADR 0071); see :mod:`mowdeck_control_husqvarna.profiles`."""
    from mowdeck_control_husqvarna.profiles import capability_profiles as profiles

    return profiles()


SPEC: Final = ProviderSpec(
    provider=PROVIDER,
    create=create,
    enabled_by_default=False,
    capability_profiles=capability_profiles,
)
"""The entry point object (``mowdeck.cloud_providers``: ``husqvarna``)."""
