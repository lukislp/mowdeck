# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""``account.erase.telemetry`` (core NATS request, queue ``telemetry-erasure``, internal):
Identity tells Telemetry to delete everything of a deleted account (ADR 0082). Exactly one
replica answers, with an ``AccountEraseReply``; decoding and replying are the kit's
(:func:`mowdeck_service_kit.erasure.erasure_handler`), the deleting is the use case's.
"""

from mowdeck_contracts.v1.subjects import account_erase_subject
from mowdeck_service_kit.bus import EventBus, Subscription
from mowdeck_service_kit.erasure import AccountErasure, erasure_handler

ERASURE_QUEUE = "telemetry-erasure"
"""Queue group of ``account.erase.telemetry``: exactly one replica erases."""


async def subscribe_erasure(bus: EventBus, erasure: AccountErasure) -> Subscription:
    """Answer ``account.erase.telemetry`` with the erasure use case."""
    return await bus.subscribe(
        account_erase_subject("telemetry"),
        erasure_handler("telemetry", erasure.erase),
        queue=ERASURE_QUEUE,
    )
