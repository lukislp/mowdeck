"""Publisher of the health history events.

``telemetry.blade_hours.threshold`` goes to ``event.telemetry.blade_hours.threshold`` (stream
``EVENTS``) with the envelope id as message id. Without a bus (tests, ``nats_enabled=false``)
nothing is published.
"""

from collections.abc import Callable

from mowdeck_contracts.v1 import (
    TELEMETRY_BLADE_HOURS_THRESHOLD,
    BladeHoursThreshold,
    EventEnvelope,
)
from mowdeck_service_kit.bus import EventBus, publish_event


class BusHealthEvents:
    """Implements :class:`~telemetry.application.ports.HealthEvents` on the event bus.

    ``bus`` is a callable so the bus the app connects at startup is looked up at publish time.
    """

    def __init__(self, bus: Callable[[], EventBus | None]) -> None:
        self._bus = bus

    async def blade_threshold(self, event: BladeHoursThreshold) -> None:
        """Publish ``telemetry.blade_hours.threshold`` (no-op without a bus)."""
        bus = self._bus()
        if bus is None:
            return
        await publish_event(
            bus,
            EventEnvelope[BladeHoursThreshold](
                type=TELEMETRY_BLADE_HOURS_THRESHOLD,
                producer="telemetry",
                account_id=event.account_id,
                payload=event,
            ),
        )
