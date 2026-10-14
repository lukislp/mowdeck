"""All use cases of Entitlements, wired from their ports."""

from dataclasses import dataclass

from mowdeck_contracts.v1 import ModuleDescriptor

from entitlements.application.catalogue import CatalogueService
from entitlements.application.entitlements import EntitlementsConfig, EntitlementsService
from entitlements.application.ports import Clock, EventPublisher, ModuleSource, Store


@dataclass(frozen=True, slots=True)
class Services:
    """The use cases, created once at startup."""

    entitlements: EntitlementsService
    catalogue: CatalogueService


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
    return Services(
        entitlements=EntitlementsService(store, catalogue, events, config, clock),
        catalogue=catalogue,
    )
