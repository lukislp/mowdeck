"""Presence of the watched accounts on the bus (demo sandboxes, ADR 0076).

``presence.accounts`` (core NATS, :data:`~mowdeck_contracts.v1.ACCOUNT_PRESENCE`): the
Device-Gateway pauses the simulated mowers of an account nobody watches. Only a demo installation
publishes it (``RealtimeConfig.presence_seconds`` above 0).
"""

from collections.abc import Callable

from mowdeck_contracts.v1 import ACCOUNT_PRESENCE, AccountPresence
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import BusError, EventBus

_log = get_logger("realtime.presence")


class BusPresencePublisher:
    """The :class:`~realtime.application.ports.PresencePublisher` on the service's bus."""

    def __init__(self, bus: Callable[[], EventBus | None]) -> None:
        self._bus = bus

    async def announce(self, presence: AccountPresence) -> None:
        """Publish; a missing or failing bus is logged (the next keep-alive heals it)."""
        bus = self._bus()
        if bus is None or not bus.is_connected:
            return
        try:
            await bus.publish(ACCOUNT_PRESENCE, presence)
        except (BusError, OSError) as exc:
            _log.warning("presence not announced", error=repr(exc))
