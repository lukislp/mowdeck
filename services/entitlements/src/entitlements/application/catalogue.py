"""The feature catalogue: the ``features`` of every module descriptor.

Like Identity's permission catalogue: Entitlements' own descriptor plus the descriptors of
``MOWDECK_ENTITLEMENTS_MODULE_URLS``. A key no answering module declares any more is marked
``retired`` but kept; modules that did not answer keep their keys untouched.
"""

import copy
from collections.abc import Iterable
from datetime import datetime

from mowdeck_contracts.v1 import ModuleDescriptor
from mowdeck_service_kit import get_logger

from entitlements.application.ports import Clock, ModuleSource, Store
from entitlements.domain.models import FeatureEntry

_log = get_logger("entitlements.catalogue")


def merge_features(
    existing: Iterable[FeatureEntry], descriptors: Iterable[ModuleDescriptor], now: datetime
) -> list[FeatureEntry]:
    """The new catalogue from the stored entries and the descriptors that answered now."""
    merged = {e.key: copy.deepcopy(e) for e in existing}
    declared: set[str] = set()
    modules: set[str] = set()
    for descriptor in descriptors:
        modules.add(descriptor.name)
        for feature in descriptor.features:
            declared.add(feature.key)
            found = merged.get(feature.key)
            if found is None:
                merged[feature.key] = FeatureEntry(
                    key=feature.key,
                    description=feature.description,
                    module=descriptor.name,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                continue
            found.description = feature.description
            found.module = descriptor.name
            found.last_seen_at = now
            found.retired = False
    for key, entry in merged.items():
        if entry.module in modules and key not in declared:
            entry.retired = True
    return sorted(merged.values(), key=lambda e: e.key)


class CatalogueService:
    """Keeps the catalogue in the database and its keys in memory."""

    def __init__(
        self,
        store: Store,
        own_descriptor: ModuleDescriptor,
        clock: Clock,
        source: ModuleSource | None = None,
    ) -> None:
        self.store = store
        self.own_descriptor = own_descriptor
        self.clock = clock
        self.source = source
        self._keys: set[str] | None = None

    async def entries(self) -> list[FeatureEntry]:
        """Every catalogue entry, by key."""
        async with self.store.transaction() as tx:
            return await tx.list_features()

    async def known_keys(self) -> set[str]:
        """Keys declared by a module (retired ones included: they once existed)."""
        if self._keys is None:
            self._keys = {e.key for e in await self.entries()}
        return set(self._keys)

    async def refresh(self, *, fetch_modules: bool = True) -> list[str]:
        """Merge the own descriptor and (``fetch_modules``) the others; returns missing URLs."""
        descriptors = [self.own_descriptor]
        missing: list[str] = []
        if fetch_modules and self.source is not None:
            for fetched in await self.source.fetch():
                if fetched.descriptor is None:
                    missing.append(fetched.url)
                else:
                    descriptors.append(fetched.descriptor)
        now = self.clock()
        async with self.store.transaction() as tx:
            merged = merge_features(await tx.list_features(), descriptors, now)
            for entry in merged:
                await tx.save_feature(entry)
        self._keys = {e.key for e in merged}
        if missing:
            _log.warning("module descriptors missing", urls=missing)
        return missing
