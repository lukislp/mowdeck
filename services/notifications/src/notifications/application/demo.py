# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account purge: forget everything Notifications holds of one account.

Two callers: Identity's cleanup job when a visitor's demo sandbox expires (ADR 0076, the demo
endpoint) and the account erasure of a deleted account (ADR 0082,
:mod:`notifications.application.erasure`). Notifications deletes the account's rows in one
transaction and then drops what the use cases keep per mower in memory (photo budget, command
throttle, parsed geofence, Live Activity throttles and area names).
"""

from collections.abc import Callable, Collection, Sequence
from uuid import UUID

from notifications.application.ports import Store


class DemoPurge:
    """Removes one account from the service."""

    def __init__(
        self, store: Store, forget: Sequence[Callable[[Collection[UUID]], None]] = ()
    ) -> None:
        self._store = store
        self._forget = tuple(forget)

    async def purge_account(self, account_id: UUID) -> int:
        """Delete every row of ``account_id``; returns how many (0 on a repeated call or for
        an account the service never knew)."""
        result = await self._store.purge_account(account_id)
        for forget in self._forget:
            forget(result.mower_ids)
        return result.deleted
