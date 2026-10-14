"""Publishes command results, telemetry and connection events on the event bus."""

from mowdeck_contracts.v1 import (
    CommandResult,
    EventEnvelope,
    MowerCapabilitiesSnapshot,
    MowerConnectionEvent,
    MowerMapSnapshot,
    MowerPathSnapshot,
    MowerState,
)
from mowdeck_contracts.v1.subjects import device_subject, telemetry_subject
from mowdeck_service_kit.bus import EventBus, publish_command_result, publish_event


class BusPublisher:
    """:class:`~device_gateway.application.ports.GatewayPublisher` on the kit's event bus."""

    def __init__(self, bus: EventBus, *, producer: str) -> None:
        self._bus = bus
        self._producer = producer

    async def command_result(self, result: CommandResult) -> None:
        """``command.result.mower.<id>`` (stream ``COMMANDS``, msg id ``<command>:<status>``)."""
        await publish_command_result(self._bus, result)

    async def telemetry(self, state: MowerState) -> None:
        """``telemetry.mower.<id>`` (stream ``TELEMETRY``); heartbeats repeat a state on purpose,
        so no msg id is set."""
        await self._bus.publish(telemetry_subject(state.mower_id), state)

    async def connection_event(self, event_type: str, payload: MowerConnectionEvent) -> None:
        """``event.mower.connected`` or ``event.mower.disconnected`` (stream ``EVENTS``)."""
        await publish_event(
            self._bus,
            EventEnvelope[MowerConnectionEvent](
                type=event_type,
                producer=self._producer,
                account_id=payload.account_id,
                payload=payload,
            ),
        )

    async def map_snapshot(self, snapshot: MowerMapSnapshot, fingerprint: str) -> None:
        """``device.mower.<id>.map`` (stream ``DEVICE``, msg id ``<mower>:map:<fingerprint>``)."""
        await self._bus.publish(
            device_subject(snapshot.mower_id, "map"),
            snapshot,
            msg_id=f"{snapshot.mower_id}:map:{fingerprint}",
        )

    async def capabilities_snapshot(
        self, snapshot: MowerCapabilitiesSnapshot, fingerprint: str
    ) -> None:
        """``device.mower.<id>.capabilities`` (stream ``DEVICE``)."""
        await self._bus.publish(
            device_subject(snapshot.mower_id, "capabilities"),
            snapshot,
            msg_id=f"{snapshot.mower_id}:capabilities:{fingerprint}",
        )

    async def path_snapshot(self, snapshot: MowerPathSnapshot, fingerprint: str) -> None:
        """``device.mower.<id>.path`` (stream ``DEVICE``, msg id ``<mower>:path:<fingerprint>``;
        the fingerprint covers ``observed_at``, so clearing the path twice is never dropped as
        a duplicate)."""
        await self._bus.publish(
            device_subject(snapshot.mower_id, "path"),
            snapshot,
            msg_id=f"{snapshot.mower_id}:path:{fingerprint}",
        )
