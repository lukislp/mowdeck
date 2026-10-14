"""Publishes ``entitlements.plan.changed`` (``event.entitlements.plan.changed``)."""

from mowdeck_contracts.v1 import ENTITLEMENTS_PLAN_CHANGED, EventEnvelope, PlanChanged
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import EventBus, publish_event
from mowdeck_service_kit.correlation import get_correlation_id

_log = get_logger("entitlements.events")


class BusEventPublisher:
    """``EventPublisher`` port. Without a bus (NATS disabled) events are only logged."""

    def __init__(self, bus: EventBus | None, producer: str = "entitlements") -> None:
        self.bus = bus
        self.producer = producer

    async def plan_changed(self, change: PlanChanged) -> None:
        """Publish; a failure is logged and swallowed (the change is already committed)."""
        if self.bus is None:
            _log.info("event not published (no bus)", event_type=ENTITLEMENTS_PLAN_CHANGED)
            return
        values: dict[str, object] = {
            "type": ENTITLEMENTS_PLAN_CHANGED,
            "producer": self.producer,
            "account_id": change.account_id,
            "payload": change,
        }
        correlation_id = get_correlation_id()
        if correlation_id:
            values["correlation_id"] = correlation_id
        try:
            await publish_event(self.bus, EventEnvelope[PlanChanged].model_validate(values))
        except Exception:
            _log.exception("publishing an event failed", event_type=ENTITLEMENTS_PLAN_CHANGED)
