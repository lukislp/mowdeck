"""Demo sandbox purge (ADR 0076): remove everything Fleet holds of one demo account.

Identity's cleanup job calls this when a visitor's sandbox expires. First every mower goes the
way of a normal removal (:meth:`MowerService.remove_in`, as the system): its rows and tuples go
and ``mower.removed`` is published, so the Device-Gateway closes the simulated connection. One
unit of work per mower, so a retry after a partial failure continues where it stopped. Then one
unit of work removes the rest: groups and sites (the default site too) with their tuples, base
stations, linked accounts, command records, drive sessions, leftovers keyed by the account and,
last, the account's audit rows. Idempotent: an account without data answers 0.
"""

from uuid import UUID

from fleet.application.camera import CameraService
from fleet.application.context import SYSTEM
from fleet.application.hierarchy import group_tuples, site_tuples
from fleet.application.mowers import MowerService
from fleet.application.ports import Tx
from fleet.application.work import UnitOfWork, Work


class AccountPurge:
    """Deletes one demo account's data (rows, tuples, cached images)."""

    def __init__(self, uow: UnitOfWork, camera: CameraService) -> None:
        self.uow = uow
        self.camera = camera

    async def purge(self, account_id: UUID) -> int:
        """Remove everything of ``account_id``; returns the number of rows and objects removed."""
        mower_ids = await self._mower_ids(account_id)
        deleted = 0
        for mower_id in mower_ids:
            deleted += await self._remove_mower(account_id, mower_id)
        deleted += self.camera.forget(mower_ids)
        async with self.uow() as work:
            deleted += await self._remove_places(work, account_id)
            deleted += await self._remove_records(work.tx, account_id)
        return deleted

    async def _mower_ids(self, account_id: UUID) -> list[UUID]:
        async with self.uow.store.transaction() as tx:
            return [m.id for m in await tx.mowers.find(account_id=account_id)]

    async def _remove_mower(self, account_id: UUID, mower_id: UUID) -> int:
        async with self.uow() as work:
            item = await work.tx.mowers.get(id=mower_id, account_id=account_id)
            if item is None:
                return 0  # removed meanwhile
            return await MowerService.remove_in(work, SYSTEM, item)

    @staticmethod
    async def _remove_places(work: Work, account_id: UUID) -> int:
        """Leftover memberships, links and area names, then groups and sites with tuples."""
        tx = work.tx
        deleted = await tx.mower_groups.delete(account_id=account_id)
        deleted += await tx.mower_links.delete(account_id=account_id)
        deleted += await tx.area_names.delete(account_id=account_id)
        for group in await tx.groups.find(account_id=account_id):
            work.delete(*group_tuples(group))
        deleted += await tx.groups.delete(account_id=account_id)
        for place in await tx.sites.find(account_id=account_id):
            work.delete(*site_tuples(place))
        return deleted

    @staticmethod
    async def _remove_records(tx: Tx, account_id: UUID) -> int:
        """Base stations, linked accounts, sites, commands, drive sessions and the audit."""
        deleted = await tx.base_stations.delete(account_id=account_id)
        deleted += await tx.linked_accounts.delete(account_id=account_id)
        deleted += await tx.sites.delete(account_id=account_id)
        deleted += await tx.commands.delete(account_id=account_id)
        deleted += await tx.drive_sessions.delete(account_id=account_id)
        return deleted + await tx.purge_audit(account_id)
