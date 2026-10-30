"""Provider conformance kit (ADR 0069): the vendor-neutral checks every adapter package runs.

A provider package proves that it honours the MowDeck contracts (ADR 0012, 0046, 0066, 0067)
by subclassing :class:`ProviderConformance` in its tests and naming itself and its fake vendor
client in a :class:`ProviderUnderTest`. The checks are plain functions in
:mod:`mowdeck_provider_testkit.checks`, so they can also be called one by one.

The kit imports no vendor package and names no vendor: it learns everything from the installed
distribution under test.
"""

from mowdeck_provider_testkit.harness import MowerUnderTest, open_mower
from mowdeck_provider_testkit.package import InstalledPackage, installed
from mowdeck_provider_testkit.subject import APP_LOCALES, ConnectionFactory, ProviderUnderTest
from mowdeck_provider_testkit.suite import ProviderConformance

__all__ = [
    "APP_LOCALES",
    "ConnectionFactory",
    "InstalledPackage",
    "MowerUnderTest",
    "ProviderConformance",
    "ProviderUnderTest",
    "installed",
    "open_mower",
]
