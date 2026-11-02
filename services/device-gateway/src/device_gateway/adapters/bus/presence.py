"""Who watches which account (demo sandboxes, ADR 0076): ``presence.accounts`` from Realtime.

Core NATS, every gateway instance, no queue group: each instance rests or wakes its own mowers.
A periodic sweep lets the mowers rest once their watchers' reports or a request's wake-up timed
out (a Realtime instance that died sends no "0"). Only a demo installation subscribes.
"""

import asyncio
import contextlib

from mowdeck_contracts.v1 import ACCOUNT_PRESENCE, AccountPresence
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import BusMessage, DropMessage, EventBus, Subscription
from pydantic import ValidationError

from device_gateway.application.gateway import DeviceGateway

_log = get_logger(__name__)

EXPIRE_SECONDS = 5.0
"""How often timed-out watchers and wake-ups are swept."""


class PresenceSubscription:
    """The presence subscription and its sweep task."""

    def __init__(self, subscription: Subscription, sweep: asyncio.Task[None]) -> None:
        self._subscription = subscription
        self._sweep = sweep

    async def stop(self) -> None:
        """Stop the sweep and unsubscribe."""
        self._sweep.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._sweep
        await self._subscription.unsubscribe()


async def _sweep(gateway: DeviceGateway, every: float) -> None:
    while True:
        await asyncio.sleep(every)
        try:
            gateway.expire_presence()
        except Exception:
            _log.exception("presence sweep failed")


async def subscribe_presence(
    bus: EventBus, gateway: DeviceGateway, *, expire_seconds: float = EXPIRE_SECONDS
) -> PresenceSubscription:
    """Follow ``presence.accounts`` and sweep every ``expire_seconds``."""

    async def handle(message: BusMessage) -> None:
        try:
            presence = message.model(AccountPresence)
        except ValidationError as exc:
            raise DropMessage("invalid presence announcement") from exc
        gateway.apply_presence(presence)

    subscription = await bus.subscribe(ACCOUNT_PRESENCE, handle)
    sweep = asyncio.create_task(_sweep(gateway, expire_seconds), name="presence-sweep")
    return PresenceSubscription(subscription, sweep)
