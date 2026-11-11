# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""``AccountEraser`` over core NATS: ``account.erase.<service>`` (ADR 0082)."""

from datetime import UTC, datetime
from uuid import UUID

from mowdeck_contracts.v1 import AccountEraseReply, AccountEraseRequest
from mowdeck_contracts.v1.subjects import account_erase_subject
from mowdeck_service_kit.bus import EventBus


class BusAccountEraser:
    """Sends the erasure request of one service and waits for its reply.

    Without a bus (NATS disabled), without an answer (the service is down: NATS answers "no
    responders" at once) and with ``ok=False`` it raises, so the purge keeps the service open
    and asks again later.
    """

    def __init__(self, bus: EventBus | None, timeout: float = 10.0) -> None:
        self.bus = bus
        self.timeout = timeout

    async def erase(self, service: str, account_id: UUID) -> int:
        """Ask ``service`` to delete everything of the account; the number of rows it deleted."""
        if self.bus is None:
            msg = "no event bus: the services cannot be asked to delete"
            raise RuntimeError(msg)
        request = AccountEraseRequest(account_id=account_id, requested_at=datetime.now(UTC))
        message = await self.bus.request(
            account_erase_subject(service), request, timeout=self.timeout
        )
        reply = message.model(AccountEraseReply)
        if not reply.ok:
            msg = f"{service} refused: {reply.error or 'no reason given'}"
            raise RuntimeError(msg)
        return reply.deleted
