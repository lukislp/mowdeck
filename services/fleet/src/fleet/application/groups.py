"""Groups: sets of mowers inside one site or across the sites of an account."""

from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from mowdeck_authz import account, site
from mowdeck_service_kit.entitlements import Entitlements

from fleet.application.access import Access
from fleet.application.context import Actor
from fleet.application.hierarchy import group_parent, group_tuples
from fleet.application.plan import allow_all, require_limit
from fleet.application.ports import Clock, Store
from fleet.application.work import UnitOfWork
from fleet.domain.errors import ConflictError, NotFoundError
from fleet.domain.models import Group
from fleet.domain.rules import CAN_MANAGE_MOWERS, clean_name

_EXISTS = "a group with this name exists"


class GroupService:
    """Use cases of groups."""

    def __init__(
        self,
        uow: UnitOfWork,
        store: Store,
        access: Access,
        clock: Clock,
        entitlements: Entitlements | None = None,
    ) -> None:
        self.uow = uow
        self.store = store
        self.access = access
        self.clock = clock
        self.entitlements = entitlements if entitlements is not None else allow_all()

    async def visible(
        self, actor: Actor, account_id: UUID, *, site_id: UUID | None = None
    ) -> list[Group]:
        """Groups of the account the actor may view, by name, optionally of one site."""
        visible = await self.access.visible_ids(actor.principal, "group", account_id)
        where: dict[str, Any] = {"account_id": account_id}
        if site_id is not None:
            where["site_id"] = site_id
        async with self.store.transaction() as tx:
            rows = await tx.groups.find(order_by="name", **where)
        return [g for g in rows if visible is None or g.id in visible]

    async def get(self, actor: Actor, account_id: UUID, group_id: UUID) -> tuple[Group, list[UUID]]:
        """One group and the ids of its mowers the actor may view."""
        async with self.store.transaction() as tx:
            item = await tx.groups.get(id=group_id, account_id=account_id)
            if item is None:
                raise NotFoundError("group not found")
            members = [m.mower_id for m in await tx.mower_groups.find(group_id=group_id)]
        visible = await self.access.visible_ids(actor.principal, "mower", account_id)
        return item, sorted((m for m in members if visible is None or m in visible), key=str)

    async def _check_site(self, actor: Actor, account_id: UUID, site_id: UUID | None) -> None:
        """``can_manage_mowers`` on the parent site, or on the account for a cross-site group."""
        if site_id is None:
            await self.access.require(
                actor.principal, CAN_MANAGE_MOWERS, account(account_id), hide=False
            )
            return
        await self.access.require(actor.principal, CAN_MANAGE_MOWERS, site(site_id))
        async with self.store.transaction() as tx:
            if await tx.sites.get(id=site_id, account_id=account_id) is None:
                raise NotFoundError("site not found")

    async def create(
        self, actor: Actor, account_id: UUID, *, name: str, site_id: UUID | None = None
    ) -> Group:
        """A new group; writes ``group#account`` and ``group#parent``."""
        await self._check_site(actor, account_id, site_id)
        now = self.clock()
        if actor.subject_id is None:
            raise NotFoundError("group not found")  # groups are created by principals only
        item = Group(
            id=uuid4(),
            account_id=account_id,
            name=clean_name(name, "group"),
            created_by=actor.subject_id,
            created_at=now,
            updated_at=now,
            site_id=site_id,
        )
        async with self.uow() as work:
            current = await work.tx.groups.count(account_id=account_id)
            await require_limit(self.entitlements, account_id, "max_groups", current)
            try:
                await work.tx.groups.add(item)
            except ConflictError:
                raise ConflictError(_EXISTS) from None
            work.write(*group_tuples(item))
            await work.audit(
                actor,
                "group.created",
                account_id,
                "group",
                item.id,
                {"name": item.name, "site_id": str(site_id) if site_id else None},
            )
        return item

    async def update(
        self, actor: Actor, account_id: UUID, group_id: UUID, changes: Mapping[str, Any]
    ) -> Group:
        """Rename, or move to another parent site (``site_id`` None: cross-site group).

        A parent change needs ``can_manage_mowers`` on the new site too and rewrites
        ``group#parent`` in the same transaction.
        """
        moving = "site_id" in changes
        new_site: UUID | None = changes.get("site_id")
        if moving:
            await self._check_site(actor, account_id, new_site)
        async with self.uow() as work:
            item = await work.tx.groups.get(id=group_id, account_id=account_id)
            if item is None:
                raise NotFoundError("group not found")
            if "name" in changes and changes["name"] is not None:
                item.name = clean_name(str(changes["name"]), "group")
            if moving and new_site != item.site_id:
                if item.site_id is not None:
                    work.delete(group_parent(item.id, item.site_id))
                if new_site is not None:
                    work.write(group_parent(item.id, new_site))
                item.site_id = new_site
            item.updated_at = self.clock()
            try:
                await work.tx.groups.update(item)
            except ConflictError:
                raise ConflictError(_EXISTS) from None
            await work.audit(
                actor, "group.updated", account_id, "group", group_id, {"fields": sorted(changes)}
            )
        return item

    async def delete(self, actor: Actor, account_id: UUID, group_id: UUID) -> None:
        """Delete an empty group and its tuples (409 while it has mowers)."""
        async with self.uow() as work:
            item = await work.tx.groups.get(id=group_id, account_id=account_id)
            if item is None:
                raise NotFoundError("group not found")
            if await work.tx.mower_groups.count(group_id=group_id):
                raise ConflictError("the group still has mowers")
            await work.tx.groups.delete(id=group_id)
            work.delete(*group_tuples(item))
            await work.audit(actor, "group.deleted", account_id, "group", group_id)
