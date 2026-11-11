# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Subscriptions of Identity.

* ``identity.grants.changed`` from other Identity instances drops the cached authorization
  decisions of the named subjects (this instance's own writes clear its cache directly).
* ``identity.user.deleted`` starts the removal of the deleted user's events from the ``EVENTS``
  stream (:mod:`identity.adapters.bus.event_log`, ADR 0082).
"""

from uuid import UUID

from mowdeck_authz.events import subscribe_cache_invalidation
from mowdeck_authz.writer import SupportsInvalidate
from mowdeck_service_kit.bus import EventBus, Subscription

from identity.adapters.bus.event_log import subscribe_user_events
from identity.application.event_erasure import EventErasureService
from identity.domain.erasure import EventScope


async def subscribe_all(
    bus: EventBus, cache: SupportsInvalidate | None, events: EventErasureService | None = None
) -> list[Subscription]:
    """Subscribe every handler of the service and return the subscriptions."""
    subscriptions: list[Subscription] = []
    if events is not None:

        async def user_deleted(user_id: UUID) -> None:
            # Record the work and do its first slice; the background loop does the rest.
            await events.advance(EventScope.USER, user_id)

        subscriptions.append(await subscribe_user_events(bus, user_deleted))
    if cache is not None:
        subscriptions.append(await subscribe_cache_invalidation(bus, cache))
    return subscriptions
