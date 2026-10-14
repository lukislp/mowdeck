"""Cache invalidation across processes (install the ``fastapi`` extra, which brings the kit).

Identity publishes ``identity.grants.changed`` (payload
:class:`~mowdeck_contracts.v1.GrantsChanged`) after every committed change of authorization
tuples. Each service subscribes once at startup and drops the cached decisions of the named
subjects, so a revoked right stops working at once instead of after the cache TTL::

    async def startup(app: FastAPI) -> None:
        app.state.authz_events = await subscribe_cache_invalidation(app.state.bus, authorizer)

The subscription is ephemeral (every instance needs every event) and new: missed events while a
service was down do not matter, because its cache was empty then. If an event is lost, the
worst case is the cache TTL (default 5 s).
"""

import logging

from mowdeck_contracts.v1 import IDENTITY_GRANTS_CHANGED, EventEnvelope, GrantsChanged
from mowdeck_contracts.v1.subjects import event_subject_for_type
from mowdeck_service_kit.bus import BusMessage, DropMessage, EventBus, Subscription
from pydantic import ValidationError

from mowdeck_authz.writer import SupportsInvalidate

_log = logging.getLogger("mowdeck_authz.events")

GRANTS_CHANGED_SUBJECT = event_subject_for_type(IDENTITY_GRANTS_CHANGED)
"""``event.identity.grants.changed``."""


def apply_grants_changed(cache: SupportsInvalidate, change: GrantsChanged) -> None:
    """Drop the cached decisions named by ``change``."""
    if change.all_subjects:
        cache.invalidate()
        return
    for subject in change.subjects:
        cache.invalidate_subject(subject)


async def subscribe_cache_invalidation(bus: EventBus, cache: SupportsInvalidate) -> Subscription:
    """Subscribe ``cache`` (an ``OpenFgaAuthorizer``) to ``identity.grants.changed``."""

    async def on_grants_changed(message: BusMessage) -> None:
        try:
            event = message.model(EventEnvelope[GrantsChanged])
        except ValidationError as exc:
            raise DropMessage(
                f"malformed grants.changed event: {exc.error_count()} errors"
            ) from exc
        apply_grants_changed(cache, event.payload)
        _log.debug(
            "authorization cache invalidated",
            extra={"subjects": len(event.payload.subjects), "all": event.payload.all_subjects},
        )

    return await bus.subscribe(GRANTS_CHANGED_SUBJECT, on_grants_changed)
