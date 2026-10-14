"""Publishes the Scheduler's events (``event.scheduler.*``)."""

from uuid import UUID

from mowdeck_contracts.v1 import (
    SCHEDULER_RUN_RECORDED,
    SCHEDULER_SCHEDULE_DISABLED,
    EventEnvelope,
    ScheduleDisabled,
    ScheduleRunRecorded,
)
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import EventBus, publish_event
from mowdeck_service_kit.correlation import get_correlation_id
from pydantic import BaseModel

_log = get_logger("scheduler.events")


class BusEventPublisher:
    """``EventPublisher`` port. Without a bus events are only logged; failures are logged."""

    def __init__(self, bus: EventBus | None, producer: str = "scheduler") -> None:
        self.bus = bus
        self.producer = producer

    async def _publish(self, event_type: str, account_id: UUID, payload: BaseModel) -> None:
        if self.bus is None:
            _log.info("event not published (no bus)", event_type=event_type)
            return
        values: dict[str, object] = {
            "type": event_type,
            "producer": self.producer,
            "account_id": account_id,
            "payload": payload,
        }
        correlation_id = get_correlation_id()
        if correlation_id:
            values["correlation_id"] = correlation_id
        try:
            envelope = EventEnvelope[type(payload)].model_validate(values)  # type: ignore[misc]
            await publish_event(self.bus, envelope)
        except Exception:
            _log.exception("publishing an event failed", event_type=event_type)

    async def run_recorded(self, run: ScheduleRunRecorded) -> None:
        """``scheduler.run.recorded``."""
        await self._publish(SCHEDULER_RUN_RECORDED, run.account_id, run)

    async def schedule_disabled(self, event: ScheduleDisabled) -> None:
        """``scheduler.schedule.disabled``."""
        await self._publish(SCHEDULER_SCHEDULE_DISABLED, event.account_id, event)
