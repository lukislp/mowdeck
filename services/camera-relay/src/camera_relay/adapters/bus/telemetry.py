"""Mower states for the relay's limits: ``telemetry.mower.*`` (stream ``TELEMETRY``).

The relay never asks a mower anything (it never wakes one); it reads the states the
Device-Gateway publishes anyway. One ephemeral consumer that starts with the newest state of
every mower (``last_per_subject``), so a restarted relay knows every mower at once. Each state
becomes a :class:`~camera_relay.domain.policy.MowerStatus`:

* ``online``: the gateway's link is up (``connected``) and its heartbeat is recent (at most
  :data:`STALE_AFTER_SECONDS` old; a stored state of a gateway that went away counts as
  offline);
* ``asleep``: the mower itself reports ``offline`` (the vendor's sleeping, powered off or
  unconnected modes);
* ``battery_percent`` and ``charging`` (also while the activity is ``charging``).

The video budget is not part of the telemetry: it comes from the vendor session the relay is
offered (``SourceOffer.quota_remaining_percent``). A state that does not validate is dropped.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Final, Protocol
from uuid import UUID

from mowdeck_contracts.v1 import MowerActivity, MowerState
from mowdeck_contracts.v1.subjects import ALL_TELEMETRY
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import BusMessage, DropMessage, EventBus, MessageHandler, Subscription
from pydantic import ValidationError

from camera_relay.domain.policy import MowerStatus

_log = get_logger("camera_relay.telemetry")

STALE_AFTER_SECONDS: Final = 120.0
"""A state whose gateway heartbeat is older than this counts as an offline mower (the gateway
publishes every 10 s while the link is up)."""


class StatusReports(Protocol):
    """Where mower states go (``RelayService``)."""

    async def mower_status_changed(self, mower_id: UUID, status: MowerStatus) -> None:
        """New state of a mower."""
        ...


def mower_status(
    state: MowerState, now: datetime, *, stale_after: float = STALE_AFTER_SECONDS
) -> MowerStatus:
    """The relay's view of one telemetry state."""
    heartbeat = state.heartbeat_at or state.observed_at
    fresh = now - heartbeat <= timedelta(seconds=stale_after)
    return MowerStatus(
        online=state.connected and fresh,
        asleep=state.activity is MowerActivity.OFFLINE,
        battery_percent=float(state.battery_percent),
        charging=state.charging or state.activity is MowerActivity.CHARGING,
    )


def telemetry_handler(
    reports: StatusReports, clock: Callable[[], datetime], *, stale_after: float
) -> MessageHandler:
    """Feed every state to the use cases."""

    async def handle(message: BusMessage) -> None:
        try:
            state = message.model(MowerState)
        except (ValidationError, ValueError) as exc:
            raise DropMessage("invalid mower state") from exc
        status = mower_status(state, clock(), stale_after=stale_after)
        await reports.mower_status_changed(state.mower_id, status)

    return handle


async def subscribe_mower_states(
    bus: EventBus,
    reports: StatusReports,
    clock: Callable[[], datetime],
    *,
    stale_after: float = STALE_AFTER_SECONDS,
) -> Subscription:
    """Subscribe ``telemetry.mower.*`` from the newest state of every mower on."""
    subscription = await bus.subscribe(
        ALL_TELEMETRY,
        telemetry_handler(reports, clock, stale_after=stale_after),
        last_per_subject=True,
    )
    _log.info("reading mower states for the relay's limits", subject=ALL_TELEMETRY)
    return subscription
