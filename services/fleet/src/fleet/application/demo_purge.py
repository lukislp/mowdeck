# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account purge: remove everything Fleet holds of one account.

Two callers: Identity's cleanup job when a visitor's demo sandbox expires (ADR 0076, the demo
endpoint) and the account erasure of a deleted account (ADR 0082,
:mod:`fleet.application.erasure`). First every mower goes the
way of a normal removal (:meth:`MowerService.remove_in`, as the system): its rows and tuples go
and ``mower.removed`` is published, so the Device-Gateway closes the simulated connection. One
unit of work per mower, so a retry after a partial failure continues where it stopped. Then one
unit of work removes the rest: groups and sites (the default site too) with their tuples, base
stations, linked accounts, command records, drive sessions, leftovers keyed by the account and,
last, the account's audit rows. What this replica keeps in memory goes too: the cached still
images of the mowers and the login throttle's entries of the account (their key holds the vendor
e-mail address). Idempotent: an account without data answers 0.
"""

from uuid import UUID

from fleet.application.camera import CameraService
from fleet.application.context import SYSTEM
from fleet.application.hierarchy import group_tuples, site_tuples
from fleet.application.mowers import MowerService
from fleet.application.ports import Tx
from fleet.application.throttle import LoginThrottle
from fleet.application.work import UnitOfWork, Work


class AccountPurge:
    """Deletes one account's data (rows, tuples, cached images, throttle entries)."""

    def __init__(self, uow: UnitOfWork, camera: CameraService, throttle: LoginThrottle) -> None:
        self.uow = uow
        self.camera = camera
        self.throttle = throttle

    async def purge(self, account_id: UUID) -> int:
        """Remove everything of ``account_id``; returns the number of rows and objects removed."""
        mower_ids = await self.mower_ids(account_id)
        deleted = 0
        for mower_id in mower_ids:
            deleted += await self._remove_mower(account_id, mower_id)
        deleted += self.camera.forget(mower_ids)
        deleted += self.throttle.forget_account(account_id)
        async with self.uow() as work:
            deleted += await self._remove_places(work, account_id)
            deleted += await self._remove_records(work.tx, account_id)
        return deleted

    async def mower_ids(self, account_id: UUID) -> list[UUID]:
        """The mowers the account has now (the account erasure purges their stream subjects,
        which it can only name while the rows exist)."""
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
