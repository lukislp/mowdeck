# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""All use cases of Entitlements, wired from their ports."""

from dataclasses import dataclass

from mowdeck_contracts.v1 import ModuleDescriptor
from mowdeck_service_kit.erasure import AccountErasure

from entitlements.application.catalogue import CatalogueService
from entitlements.application.entitlements import EntitlementsConfig, EntitlementsService
from entitlements.application.erasure import account_erasure
from entitlements.application.ports import Clock, EventPublisher, ModuleSource, Store


@dataclass(frozen=True, slots=True)
class Services:
    """The use cases, created once at startup."""

    entitlements: EntitlementsService
    catalogue: CatalogueService
    erasure: AccountErasure
    """Answers ``account.erase.entitlements`` (ADR 0082)."""


def build_services(
    *,
    store: Store,
    own_descriptor: ModuleDescriptor,
    events: EventPublisher,
    config: EntitlementsConfig,
    clock: Clock,
    source: ModuleSource | None = None,
) -> Services:
    """Wire the use cases."""
    catalogue = CatalogueService(store, own_descriptor, clock, source)
    entitlements = EntitlementsService(store, catalogue, events, config, clock)
    return Services(
        entitlements=entitlements,
        catalogue=catalogue,
        erasure=account_erasure(entitlements, store),
    )
