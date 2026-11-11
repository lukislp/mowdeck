# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""``account.erase.entitlements`` (core NATS request, queue ``entitlements-erasure``,
internal): Identity tells Entitlements to delete everything of a deleted account (ADR 0082).
Exactly one replica answers, with an ``AccountEraseReply``; decoding and replying are the kit's
(:func:`mowdeck_service_kit.erasure.erasure_handler`), the deleting is the use case's.
"""

from mowdeck_contracts.v1.subjects import account_erase_subject
from mowdeck_service_kit.bus import EventBus, Subscription
from mowdeck_service_kit.erasure import AccountErasure, erasure_handler

ERASURE_QUEUE = "entitlements-erasure"
"""Queue group of ``account.erase.entitlements``: exactly one replica erases."""


async def subscribe_erasure(bus: EventBus, erasure: AccountErasure) -> Subscription:
    """Answer ``account.erase.entitlements`` with the erasure use case."""
    return await bus.subscribe(
        account_erase_subject("entitlements"),
        erasure_handler("entitlements", erasure.erase),
        queue=ERASURE_QUEUE,
    )
