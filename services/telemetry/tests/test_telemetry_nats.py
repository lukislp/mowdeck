"""JetStream: a published MowerState and a command result are consumed and stored.

Runs when ``MOWDECK_TEST_NATS_URL`` is set (CI starts NATS 2.15 with JetStream). Locally::

    docker run -d --name telemetry-test-nats -p 4222:4222 \
        public.ecr.aws/docker/library/nats:2.15.0-alpine -js
    MOWDECK_TEST_NATS_URL=nats://127.0.0.1:4222 uv run pytest services/telemetry -k nats
"""

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    CommandResult,
    CommandStatus,
    ConnectionKind,
    MowerListReply,
    MowerRegistration,
    PositionTrackReply,
    PositionTrackRequest,
)
from mowdeck_contracts.v1.subjects import (
    FLEET_MOWERS_LIST,
    TELEMETRY_POSITIONS_QUERY,
    telemetry_subject,
)
from mowdeck_service_kit.bus import BusMessage, JetStreamBus, publish_command_result
from telemetry_support import API, T0, harness, make_state

NATS_URL = os.environ.get("MOWDECK_TEST_NATS_URL")

pytestmark = pytest.mark.skipif(not NATS_URL, reason="needs MOWDECK_TEST_NATS_URL")


async def eventually(probe: Callable[[], Awaitable[bool]], seconds: float = 10.0) -> None:
    """Wait until ``probe()`` is true (polls every 0.1 s)."""
    for _ in range(int(seconds * 10)):
        if await probe():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"not true within {seconds} s")


async def test_telemetry_nats_state_and_result_are_stored_and_served() -> None:
    assert NATS_URL
    bus = JetStreamBus(NATS_URL, name="telemetry-test")
    await bus.connect()
    gateway = JetStreamBus(NATS_URL, name="gateway-test")
    await gateway.connect()

    async def fleet(message: BusMessage) -> None:
        """A stand-in for Fleet: lists one mower of the account under test."""
        await message.respond(MowerListReply(mowers=[registration]))

    try:
        async with harness(bus=bus, nats_enabled=True, background_tasks=True) as h:
            owner = await h.owner()
            mower_id = await h.add_mower(owner, announce=False)
            registration = MowerRegistration(
                mower_id=mower_id,
                account_id=owner.account_id,
                name="Front lawn",
                connection_kind=ConnectionKind.SIMULATED,
            )
            responder = await gateway.subscribe(FLEET_MOWERS_LIST, fleet, queue="fleet")
            try:
                # The seed fills the directory (Fleet answers once the mower is listed).
                async def seeded() -> bool:
                    return bool(h.app.state.directory_seeded)

                await eventually(seeded)
                await gateway.publish(
                    telemetry_subject(mower_id), make_state(mower_id, owner.account_id, at=T0)
                )
                command_id = uuid4()
                await publish_command_result(
                    gateway,
                    CommandResult(
                        command_id=command_id, mower_id=mower_id, status=CommandStatus.EXECUTED
                    ),
                )

                async def stored() -> bool:
                    state = await h.client.get(
                        f"{API}/mowers/{mower_id}/state", headers=owner.headers
                    )
                    timeline = await h.client.get(
                        f"{API}/mowers/{mower_id}/commands", headers=owner.headers
                    )
                    return state.status_code == 200 and len(timeline.json()["items"]) == 1

                await eventually(stored)
                state = await h.client.get(f"{API}/mowers/{mower_id}/state", headers=owner.headers)
            finally:
                await responder.unsubscribe()
    finally:
        await gateway.close()
    assert state.json()["state"]["activity"] == "docked"


async def test_telemetry_nats_answers_the_track_query() -> None:
    assert NATS_URL
    bus = JetStreamBus(NATS_URL, name="telemetry-track-test")
    await bus.connect()
    planner = JetStreamBus(NATS_URL, name="planner-test")
    await planner.connect()
    try:
        async with harness(bus=bus, nats_enabled=True) as h:
            owner = await h.owner()
            mower_id = await h.add_mower(owner)
            await h.publish_state(
                make_state(mower_id, owner.account_id, at=T0, position=(48.1, 11.5))
            )
            request = PositionTrackRequest(
                account_id=owner.account_id,
                mower_id=mower_id,
                start=T0 - timedelta(minutes=1),
                end=T0 + timedelta(minutes=1),
            )
            message = await planner.request(TELEMETRY_POSITIONS_QUERY, request, timeout=5)
    finally:
        await planner.close()
    reply = message.model(PositionTrackReply)
    assert reply.count == 1
    assert reply.points() == [(48.1, 11.5)]
