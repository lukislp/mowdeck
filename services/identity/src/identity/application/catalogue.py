"""The permission catalogue: keys of the authorization model plus module descriptors.

* Every key of ``mowdeck_authz.PERMISSION_RELATIONS`` is always present (``in_model``), with
  the levels and subject types the model allows, even before the module that owns it exists.
* Module descriptors (``MOWDECK_IDENTITY_MODULE_URLS``, plus Identity's own) add descriptions
  and **account keys**: keys without a model relation, grantable on the whole account only and
  enforced by the module from the token's ``perms``.
* Keys that disappear from every descriptor are marked ``retired`` but kept, so roles do not
  break; model keys never retire.
"""

import copy
import logging
from collections.abc import Iterable
from datetime import datetime

from mowdeck_authz import PERMISSION_RELATIONS
from mowdeck_contracts.v1 import ModuleDescriptor

from identity.application.ports import Clock, ModuleSource, Store, Tx
from identity.domain.models import CatalogueEntry, LevelType
from identity.domain.permissions import Catalogue

_log = logging.getLogger("identity.catalogue")

MODEL_MODULE = "authorization-model"
"""``module`` of keys that come from the model and no descriptor declared yet."""

_LEVEL_ORDER = [level.value for level in LevelType]


def model_entries(now: datetime) -> dict[str, CatalogueEntry]:
    """Entries for every key of the authorization model."""
    return {
        key: CatalogueEntry(
            key=key,
            description=key,
            module=MODEL_MODULE,
            levels=[lvl for lvl in _LEVEL_ORDER if lvl in entry.levels],
            in_model=True,
            first_seen_at=now,
            last_seen_at=now,
            users_only="api_token" not in entry.subject_types,
        )
        for key, entry in PERMISSION_RELATIONS.items()
    }


def merge_descriptors(
    existing: Iterable[CatalogueEntry],
    descriptors: Iterable[ModuleDescriptor],
    now: datetime,
    *,
    seen_modules: set[str] | None = None,
) -> list[CatalogueEntry]:
    """The new catalogue from the stored entries and the descriptors fetched now.

    ``seen_modules`` are the modules that answered; keys of modules that did not answer stay
    as they are (an unreachable module never retires its keys).
    """
    merged = {e.key: copy.deepcopy(e) for e in existing}
    for key, entry in model_entries(now).items():
        if key in merged:
            current = merged[key]
            current.in_model, current.levels = True, entry.levels
            current.users_only, current.retired = entry.users_only, False
        else:
            merged[key] = entry
    declared: set[str] = set()
    modules = set(seen_modules or set())
    for descriptor in descriptors:
        modules.add(descriptor.name)
        for permission in descriptor.permissions:
            declared.add(permission.key)
            model = PERMISSION_RELATIONS.get(permission.key)
            found = merged.get(permission.key)
            if found is None:
                found = CatalogueEntry(
                    key=permission.key,
                    description=permission.description,
                    module=descriptor.name,
                    levels=[LevelType.ACCOUNT.value],
                    in_model=False,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                merged[permission.key] = found
                if model is None:
                    _log.info(
                        "account permission registered (no model relation)",
                        extra={"key": permission.key, "module_name": descriptor.name},
                    )
            found.description = permission.description
            found.module = descriptor.name
            found.implied_by = [k for k in permission.implied_by if k != permission.key]
            found.last_seen_at = now
            found.retired = False
    for key, entry in merged.items():
        if not entry.in_model and entry.module in modules and key not in declared:
            entry.retired = True
    return sorted(merged.values(), key=lambda e: e.key)


class CatalogueService:
    """Keeps the catalogue in the database and a copy in memory."""

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
        self._catalogue: Catalogue | None = None
        self._fetched: dict[str, ModuleDescriptor] = {}

    def descriptors(self) -> list[ModuleDescriptor]:
        """Identity's own descriptor plus the last descriptor each module URL answered with.

        A module that stops answering keeps its last descriptor (like its permission keys), so
        a short outage does not empty the web app's menu.
        """
        return [self.own_descriptor, *self._fetched.values()]

    async def current(self, tx: Tx | None = None) -> Catalogue:
        """The catalogue (loaded once, then kept in memory until :meth:`refresh`)."""
        if self._catalogue is None:
            if tx is not None:
                self._catalogue = Catalogue(await tx.catalogue.find())
            else:
                async with self.store.transaction() as own:
                    self._catalogue = Catalogue(await own.catalogue.find())
            if not len(self._catalogue):
                self._catalogue = Catalogue(model_entries(self.clock()).values())
        return self._catalogue

    async def refresh(self, *, fetch_modules: bool = True) -> list[str]:
        """Merge the model, the own descriptor and (``fetch_modules``) the module descriptors.

        Returns the URLs that did not answer (they are retried on the next refresh).
        """
        descriptors = [self.own_descriptor]
        missing: list[str] = []
        if fetch_modules and self.source is not None:
            for fetched in await self.source.fetch():
                if fetched.descriptor is None:
                    missing.append(fetched.url)
                else:
                    descriptors.append(fetched.descriptor)
                    self._fetched[fetched.url] = fetched.descriptor
        now = self.clock()
        async with self.store.transaction() as tx:
            existing = await tx.catalogue.find()
            merged = merge_descriptors(existing, descriptors, now)
            known = {e.key for e in existing}
            for entry in merged:
                if entry.key in known:
                    await tx.catalogue.update(entry)
                else:
                    await tx.catalogue.add(entry)
        self._catalogue = Catalogue(merged)
        if missing:
            _log.warning("module descriptors missing", extra={"urls": missing})
        return missing
