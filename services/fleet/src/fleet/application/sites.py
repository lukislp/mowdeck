"""Sites: create, list, read, change, delete; and the hidden default site of every account."""

from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from mowdeck_service_kit.entitlements import Entitlements

from fleet.application.access import Access
from fleet.application.context import Actor, FleetConfig
from fleet.application.hierarchy import site_tuples
from fleet.application.plan import allow_all, require_limit
from fleet.application.ports import Clock, Store
from fleet.application.work import UnitOfWork, Work
from fleet.domain.errors import ConflictError, NotFoundError
from fleet.domain.models import Site
from fleet.domain.rules import clean_name, validate_coordinates, validate_timezone


class SiteService:
    """Use cases of sites."""

    def __init__(
        self,
        uow: UnitOfWork,
        store: Store,
        access: Access,
        config: FleetConfig,
        clock: Clock,
        entitlements: Entitlements | None = None,
    ) -> None:
        self.uow = uow
        self.store = store
        self.access = access
        self.config = config
        self.clock = clock
        self.entitlements = entitlements if entitlements is not None else allow_all()

    # --- default site --------------------------------------------------------------------

    async def ensure_default_in(self, work: Work, account_id: UUID) -> Site:
        """The account's default site, created in ``work`` if it does not exist yet.

        Runs when ``identity.account.created`` arrives and lazily whenever a mower is registered
        without a site, so a mower registered seconds after the account never races the event.
        A concurrent creation is resolved by the unique index (``uq_sites_one_default``).
        """
        found = await work.tx.sites.get(account_id=account_id, is_default=True)
        if found is not None:
            return found
        now = self.clock()
        default = Site(
            id=uuid4(),
            account_id=account_id,
            name=self.config.default_site_name,
            timezone=self.config.default_timezone,
            is_default=True,
            created_at=now,
            updated_at=now,
        )
        try:
            await work.tx.sites.add(default)
        except ConflictError:
            found = await work.tx.sites.get(account_id=account_id, is_default=True)
            if found is None:
                raise
            return found
        work.write(*site_tuples(default))
        return default

    async def ensure_default(self, account_id: UUID) -> Site:
        """:meth:`ensure_default_in` in its own unit of work (event handler)."""
        async with self.uow() as work:
            return await self.ensure_default_in(work, account_id)

    # --- reading -------------------------------------------------------------------------

    async def visible(
        self, actor: Actor, account_id: UUID, *, include_default: bool = False
    ) -> list[Site]:
        """Sites of the account the actor may view, by name; the default site on request."""
        visible = await self.access.visible_ids(actor.principal, "site", account_id)
        async with self.store.transaction() as tx:
            rows = await tx.sites.find(account_id=account_id, order_by="name")
        return [
            s
            for s in rows
            if (include_default or not s.is_default) and (visible is None or s.id in visible)
        ]

    async def get(self, account_id: UUID, site_id: UUID) -> Site:
        """One site of the account (404 for any other account)."""
        async with self.store.transaction() as tx:
            found = await tx.sites.get(id=site_id, account_id=account_id)
        if found is None:
            raise NotFoundError("site not found")
        return found

    # --- changes -------------------------------------------------------------------------

    async def create(
        self,
        actor: Actor,
        account_id: UUID,
        *,
        name: str,
        timezone: str | None = None,
        address: Mapping[str, Any] | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> Site:
        """A new named site (the caller checked ``can_manage_mowers`` on the account)."""
        latitude, longitude = validate_coordinates(latitude, longitude)
        now = self.clock()
        item = Site(
            id=uuid4(),
            account_id=account_id,
            name=clean_name(name, "site"),
            timezone=validate_timezone(timezone or self.config.default_timezone),
            is_default=False,
            created_at=now,
            updated_at=now,
            address=dict(address) if address is not None else None,
            created_by=actor.subject_id,
            latitude=latitude,
            longitude=longitude,
        )
        async with self.uow() as work:
            current = await work.tx.sites.count(account_id=account_id)
            await require_limit(self.entitlements, account_id, "max_sites", current)
            await self._add(work, item)
            work.write(*site_tuples(item))
            await work.audit(
                actor, "site.created", account_id, "site", item.id, {"name": item.name}
            )
        return item

    async def update(
        self, actor: Actor, account_id: UUID, site_id: UUID, changes: Mapping[str, Any]
    ) -> Site:
        """Change name, timezone, address or coordinates (``changes`` holds only the given
        fields; ``latitude`` and ``longitude`` change together, ``null`` clears them).

        The default site keeps its name (409); everything else of it may change.
        """
        async with self.uow() as work:
            item = await work.tx.sites.get(id=site_id, account_id=account_id)
            if item is None:
                raise NotFoundError("site not found")
            if "name" in changes and changes["name"] is not None:
                name = clean_name(str(changes["name"]), "site")
                if item.is_default and name != item.name:
                    raise ConflictError("the default site cannot be renamed")
                item.name = name
            if "timezone" in changes and changes["timezone"] is not None:
                item.timezone = validate_timezone(str(changes["timezone"]))
            if "address" in changes:
                address = changes["address"]
                item.address = dict(address) if address is not None else None
            if "latitude" in changes or "longitude" in changes:
                item.latitude, item.longitude = validate_coordinates(
                    changes.get("latitude", item.latitude), changes.get("longitude", item.longitude)
                )
            item.updated_at = self.clock()
            try:
                await work.tx.sites.update(item)
            except ConflictError:
                raise ConflictError("a site with this name exists") from None
            await work.audit(
                actor, "site.updated", account_id, "site", site_id, {"fields": sorted(changes)}
            )
        return item

    async def delete(self, actor: Actor, account_id: UUID, site_id: UUID) -> None:
        """Delete an empty, non-default site and its ``account`` tuple (409 otherwise)."""
        async with self.uow() as work:
            item = await work.tx.sites.get(id=site_id, account_id=account_id)
            if item is None:
                raise NotFoundError("site not found")
            if item.is_default:
                raise ConflictError("the default site cannot be deleted")
            if await work.tx.mowers.count(site_id=site_id) or await work.tx.groups.count(
                site_id=site_id
            ):
                raise ConflictError("the site still has mowers or groups")
            await work.tx.sites.delete(id=site_id)
            work.delete(*site_tuples(item))
            await work.audit(actor, "site.deleted", account_id, "site", site_id)

    @staticmethod
    async def _add(work: Work, item: Site) -> None:
        try:
            await work.tx.sites.add(item)
        except ConflictError:
            raise ConflictError("a site with this name exists") from None
