# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure (ADR 0082): what Fleet deletes when an account is deleted for good.

Identity asks on ``account.erase.fleet`` once the grace period of a deleted account is over.
Fleet is asked first: removing the mowers publishes ``mower.removed`` for each, so the
Device-Gateway closes their connections and the other services see no further states.

The use case is the kit's :class:`~mowdeck_service_kit.erasure.AccountErasure` over Fleet's own
parts:

* the purge is :class:`~fleet.application.demo_purge.AccountPurge`, the one of the demo sandboxes
  (ADR 0076): mowers with their groups, links, area names and tuples, groups and sites (the
  default site too) with their tuples, base stations, linked vendor accounts with their encrypted
  credentials, command records, drive sessions and the account's audit rows, and in memory the
  cached still images and the login throttle's entries;
* **the message streams.** The streams keep what the account's mowers sent and were sent
  (positions, the map of the garden, who issued a command) for up to 30 days, on subjects that
  carry the mower id. Fleet owns the mowers, so :class:`ErasureWithStreams` purges those subjects
  (the port :class:`~fleet.application.ports.MowerStreams`) around the row purge: **before**,
  while the rows still say which mowers the account has (a purge that fails leaves the rows, and
  the next attempt knows the mowers again), and **after**, for what a mower still sent until its
  connection closed;
* what is left is counted over every table with an ``account_id`` column
  (:meth:`fleet.application.ports.Tx.account_rows`) and over the stream subjects of the mowers
  this attempt removed. A row or a message that is still there, or a purge that raised, is
  reported as a failure; Identity asks again and the purge continues where it stopped.
"""

from uuid import UUID

from mowdeck_service_kit.erasure import AccountErasure

from fleet.application.demo_purge import AccountPurge
from fleet.application.ports import MowerStreams, Store

STREAM_MESSAGES = "stream messages"
"""Name under which messages left in the streams are reported next to the tables."""


class ErasureWithStreams:
    """The row purge of an account with the stream purge of its mowers around it."""

    def __init__(self, purge: AccountPurge, store: Store, streams: MowerStreams) -> None:
        self._rows = purge
        self._store = store
        self._streams = streams
        self._mowers: dict[UUID, tuple[UUID, ...]] = {}
        """The mowers of the account an attempt is erasing, for the check afterwards (the rows
        that named them are gone by then)."""

    async def purge(self, account_id: UUID) -> int:
        """Purge the streams, the rows, then the streams again; the rows, objects and messages
        removed. Raises when a stream purge fails: nothing is confirmed then."""
        mower_ids = tuple(await self._rows.mower_ids(account_id))
        self._mowers[account_id] = mower_ids
        removed = await self._purge_streams(mower_ids)
        removed += await self._rows.purge(account_id)
        return removed + await self._purge_streams(mower_ids)

    async def leftovers(self, account_id: UUID) -> dict[str, int]:
        """The rows of the account per table, and the messages its mowers still have."""
        async with self._store.transaction() as tx:
            left = dict(await tx.account_rows(account_id))
        messages = 0
        for mower_id in self._mowers.pop(account_id, ()):
            messages += await self._streams.remaining(mower_id)
        if messages:
            left[STREAM_MESSAGES] = messages
        return left

    async def _purge_streams(self, mower_ids: tuple[UUID, ...]) -> int:
        purged = 0
        for mower_id in mower_ids:
            purged += await self._streams.purge(mower_id)
        return purged


def account_erasure(purge: AccountPurge, store: Store, streams: MowerStreams) -> AccountErasure:
    """The erasure of one account in Fleet."""
    erasure = ErasureWithStreams(purge, store, streams)
    return AccountErasure(erasure.purge, erasure.leftovers)
