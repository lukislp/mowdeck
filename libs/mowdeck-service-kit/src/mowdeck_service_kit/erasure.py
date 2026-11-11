# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: Apache-2.0

"""Account erasure (ADR 0082): how a service answers ``account.erase.<service>``.

Identity asks every service that keeps per-account data to delete it (the contract and its
semantics: :mod:`mowdeck_contracts.v1.erasure`). The part that is the same in every service
lives here, once:

* :class:`AccountErasure`, the use case. A service gives it three things it owns: its purge
  (the one of the demo sandboxes, ADR 0076), a count of the rows it still holds of an account,
  and what it must forget in memory. The use case runs them in that order and refuses to
  report success while rows are left.
* :func:`erasure_handler`, the inbound bus adapter: decode the request, call the use case,
  answer with an :class:`~mowdeck_contracts.v1.AccountEraseReply`. It holds no rule.

A service subscribes in its own bus adapter, so the subject stays visible to the repository's
subject scans (the NATS permissions, the architecture diagrams)::

    await bus.subscribe(
        account_erase_subject("fleet"),
        erasure_handler("fleet", services.erasure.erase),
        queue="fleet",
    )

What the reply means:

* ``ok=True``: the purge ran and the service's tables hold no row of the account. ``deleted``
  is 0 when there was nothing (a repeated request, an account the service never knew).
* ``ok=False``: the purge raised, or rows of the account are still there afterwards. Identity
  keeps the account in its purging state and asks again. The ``error`` names the kind of
  failure and the tables, never row contents: the detail is in the service's log.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from uuid import UUID

from mowdeck_contracts.v1 import AccountEraseReply, AccountEraseRequest
from pydantic import ValidationError

from mowdeck_service_kit.bus import BusMessage, MessageHandler
from mowdeck_service_kit.logging import get_logger

_log = get_logger(__name__)

Purge = Callable[[UUID], Awaitable[int]]
"""Deletes everything the service holds of an account; the number of rows and objects removed
(0 when nothing was left). Idempotent."""

Leftovers = Callable[[UUID], Awaitable[Mapping[str, int]]]
"""The rows the service still holds of an account, per table; empty when it holds none."""

Forget = Callable[[UUID], None]
"""Drops what a use case of this process keeps of an account in memory."""

Erase = Callable[[UUID], Awaitable[int]]
"""What the handler calls: :meth:`AccountErasure.erase`."""

ERROR_MAX_LENGTH = 500
"""``AccountEraseReply.error`` is at most this long."""


class ErasureIncomplete(Exception):  # noqa: N818 - a state, read like one in the use case
    """The purge ran and the service still holds rows of the account.

    The message names the tables and their row counts and nothing else, so it is safe to send
    to Identity and to keep in its erasure record."""

    def __init__(self, leftovers: Mapping[str, int]) -> None:
        self.leftovers = dict(leftovers)
        tables = ", ".join(f"{name}: {count}" for name, count in sorted(self.leftovers.items()))
        super().__init__(f"rows of the account remain ({tables})")


class AccountErasure:
    """Erases one account in one service: purge, forget, then check that nothing is left."""

    def __init__(
        self,
        purge: Purge,
        leftovers: Leftovers,
        forget: Sequence[Forget] = (),
    ) -> None:
        self._purge = purge
        self._leftovers = leftovers
        self._forget = tuple(forget)

    async def erase(self, account_id: UUID) -> int:
        """Delete everything of ``account_id``; returns the number of rows removed (0 on a
        repeated call). Raises :class:`ErasureIncomplete` when rows are left afterwards, and
        whatever the purge raises: the caller reports both as a failure."""
        deleted = await self._purge(account_id)
        for forget in self._forget:
            forget(account_id)
        remaining = {
            table: count for table, count in (await self._leftovers(account_id)).items() if count
        }
        if remaining:
            raise ErasureIncomplete(remaining)
        return deleted


def erasure_handler(service: str, erase: Erase) -> MessageHandler:
    """The handler of ``account.erase.<service>``: answers every request, success or not.

    A request that cannot be decoded and an erasure that raised are answered with ``ok=False``
    (Identity retries with backoff); nothing is left unanswered on purpose, because a missing
    reply only tells Identity that the request timed out.
    """

    async def handle(message: BusMessage) -> None:
        try:
            request = message.model(AccountEraseRequest)
        except ValidationError as exc:
            _log.warning("invalid erasure request", service=service, errors=exc.error_count())
            await message.respond(
                AccountEraseReply(
                    ok=False, error=f"invalid request: {exc.error_count()} validation errors"
                )
            )
            return
        account_id = str(request.account_id)
        try:
            deleted = await erase(request.account_id)
        except ErasureIncomplete as exc:
            _log.error(
                "account not erased: rows remain",
                service=service,
                account_id=account_id,
                leftovers=exc.leftovers,
            )
            await message.respond(AccountEraseReply(ok=False, error=str(exc)[:ERROR_MAX_LENGTH]))
        except Exception as exc:
            _log.exception("account erasure failed", service=service, account_id=account_id)
            await message.respond(
                AccountEraseReply(ok=False, error=f"{type(exc).__name__}: see the log of {service}")
            )
        else:
            _log.info(
                "account erased",
                service=service,
                account_id=account_id,
                deleted=deleted,
                reason=request.reason,
            )
            await message.respond(AccountEraseReply(ok=True, deleted=deleted))

    return handle
