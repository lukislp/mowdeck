"""Subscriptions of Identity.

Only one: ``identity.grants.changed`` from other Identity instances drops the cached
authorization decisions of the named subjects (this instance's own writes clear its cache
directly).
"""

from mowdeck_authz.events import subscribe_cache_invalidation
from mowdeck_authz.writer import SupportsInvalidate
from mowdeck_service_kit.bus import EventBus, Subscription


async def subscribe_all(bus: EventBus, cache: SupportsInvalidate | None) -> list[Subscription]:
    """Subscribe every handler of the service and return the subscriptions."""
    if cache is None:
        return []
    return [await subscribe_cache_invalidation(bus, cache)]
