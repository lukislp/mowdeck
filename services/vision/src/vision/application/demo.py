# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account purge: forget everything Vision holds of one account.

Two callers: Identity's cleanup job when a visitor's demo sandbox expires (ADR 0076, the demo
endpoint) and the account erasure of a deleted account (ADR 0082,
:mod:`vision.application.erasure`). Vision first stops the analyses and executions running for
the account, so none of them writes a row afterwards, then deletes the account's rows in one
transaction and drops what it keeps of the account's mowers in memory (the bus view and the
remembered errors of the automatic analysis).
"""

from collections.abc import Awaitable, Callable, Collection
from uuid import UUID

from vision.application.ports import MowerView, Store


class DemoPurge:
    """Removes one account from the service."""

    def __init__(
        self,
        store: Store,
        view: MowerView | None = None,
        forget_mowers: Callable[[Collection[UUID]], None] | None = None,
        stop: Callable[[UUID], Awaitable[int]] | None = None,
    ) -> None:
        self._store = store
        self._view = view
        self._forget_mowers = forget_mowers
        self._stop = stop

    async def purge_account(self, account_id: UUID) -> int:
        """Delete every row of ``account_id``; returns how many (0 on a repeated call). The
        in-memory state does not count: it holds no rows."""
        if self._stop is not None:
            await self._stop(account_id)
        deleted = await self._store.purge_account(account_id)
        mowers = self._view.forget_account(account_id) if self._view is not None else set()
        if self._forget_mowers is not None:
            self._forget_mowers(mowers)
        return deleted
