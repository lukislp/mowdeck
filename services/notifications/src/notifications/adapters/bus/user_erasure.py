# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""``event.identity.user.deleted`` (JetStream ``EVENTS``, durable ``notifications-users``): a
user was deleted in Identity (ADR 0082), so Notifications forgets the person. One replica
handles each event; decoding and redelivery are the kit's
(:func:`mowdeck_service_kit.user_erasure.user_deleted_handler`), the forgetting is the use case's.
"""

from mowdeck_contracts.v1 import IDENTITY_USER_DELETED
from mowdeck_contracts.v1.subjects import event_subject_for_type
from mowdeck_service_kit.bus import EventBus, Subscription
from mowdeck_service_kit.user_erasure import UserErasure, user_deleted_handler

USERS_CONSUMER = "notifications-users"
"""Durable consumer (and deliver group) of ``identity.user.deleted``, shared by all replicas."""


async def subscribe_user_erasure(bus: EventBus, erasure: UserErasure) -> Subscription:
    """Forget a deleted user with the erasure use case."""
    return await bus.subscribe(
        event_subject_for_type(IDENTITY_USER_DELETED),
        user_deleted_handler("notifications", erasure.erase),
        durable=USERS_CONSUMER,
        queue=USERS_CONSUMER,
    )
