"""Subscriptions of the service.

Add handlers here, for example::

    async def on_mower_registered(message: BusMessage) -> None:
        event = message.model(EventEnvelope[MowerRegistration])
        ...

    subscriptions.append(await bus.subscribe("event.mower.registered", on_mower_registered))

Handler rules: return to acknowledge, raise to get a redelivery, raise ``DropMessage`` to
discard a poison message.
"""

from mowdeck_service_kit.bus import EventBus, Subscription


async def subscribe_all(bus: EventBus) -> list[Subscription]:
    """Subscribe every handler of the service and return the subscriptions."""
    subscriptions: list[Subscription] = []
    del bus  # the template has no handlers yet
    return subscriptions
