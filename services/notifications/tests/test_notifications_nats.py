"""JetStream: states published like the Device-Gateway end up as an inbox item and a delivery.

Runs when ``MOWDECK_TEST_NATS_URL`` is set (CI starts NATS 2.15 with JetStream). Locally::

    docker run -d --name notifications-test-nats -p 4222:4222 \
        public.ecr.aws/docker/library/nats:2.15.0-alpine -js
    MOWDECK_TEST_NATS_URL=nats://127.0.0.1:4222 uv run pytest services/notifications -k nats
"""

import asyncio
import os
from collections.abc import Awaitable, Callable

import pytest
from mowdeck_contracts.v1 import (
    ConnectionKind,
    EventEnvelope,
    MowerActivity,
    MowerListReply,
    MowerRegistration,
    NotificationSent,
)
from mowdeck_contracts.v1.subjects import FLEET_MOWERS_LIST, telemetry_subject
from mowdeck_service_kit.bus import BusMessage, JetStreamBus
from notifications_support import harness, make_state

NATS_URL = os.environ.get("MOWDECK_TEST_NATS_URL")

pytestmark = pytest.mark.skipif(not NATS_URL, reason="needs MOWDECK_TEST_NATS_URL")


async def eventually(probe: Callable[[], Awaitable[bool]], seconds: float = 10.0) -> None:
    """Wait until ``probe()`` is true (polls every 0.1 s)."""
    for _ in range(int(seconds * 10)):
        if await probe():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"not true within {seconds} s")


async def test_notifications_nats_states_become_a_delivery() -> None:
    assert NATS_URL
    bus = JetStreamBus(NATS_URL, name="notifications-test")
    await bus.connect()
    gateway = JetStreamBus(NATS_URL, name="gateway-test")
    await gateway.connect()
    feed: list[NotificationSent] = []

    async def fleet(message: BusMessage) -> None:
        """A stand-in for Fleet: lists the mower of the account under test."""
        await message.respond(MowerListReply(mowers=[registration]))

    async def app_feed(message: BusMessage) -> None:
        feed.append(message.model(EventEnvelope[NotificationSent]).payload)

    try:
        async with harness(
            bus=bus,
            nats_enabled=True,
            background_tasks=True,
            notifications_worker_interval_seconds=0.1,
        ) as h:
            owner = await h.owner()
            mower_id = await h.add_mower(owner, announce=False)
            registration = MowerRegistration(
                mower_id=mower_id,
                account_id=owner.account_id,
                name="Front lawn",
                connection_kind=ConnectionKind.SIMULATED,
            )
            responder = await gateway.subscribe(FLEET_MOWERS_LIST, fleet, queue="fleet")
            listener = await gateway.subscribe("event.notifications.sent", app_feed)
            try:

                async def seeded() -> bool:
                    return bool(h.app.state.directory_seeded)

                await eventually(seeded)
                await h.subscribe_all(owner, enabled_kinds=["mowing_finished"])
                for delta, activity in (
                    (0, MowerActivity.DOCKED),
                    (10, MowerActivity.MOWING),
                    (900, MowerActivity.CHARGING),
                ):
                    h.clock.advance(delta)
                    await gateway.publish(
                        telemetry_subject(mower_id),
                        make_state(mower_id, owner.account_id, at=h.clock.now, activity=activity),
                    )

                async def delivered() -> bool:
                    return bool(h.channel.sent) and bool(feed)

                await eventually(delivered)
                items = await h.inbox(owner)
            finally:
                await responder.unsubscribe()
                await listener.unsubscribe()
    finally:
        await gateway.close()
    assert [i["kind"] for i in items] == ["mowing_finished"]
    assert items[0]["title"] == "Front lawn finished mowing"
    assert [d.message.kind.value for d in h.channel.sent] == ["mowing_finished"]
    assert feed[0].user_id == owner.user_id and feed[0].channels == ["log"]
