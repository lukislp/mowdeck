"""NATS: the map snapshot on the ``DEVICE`` stream reaches the Planner (also when it was
published before the Planner started), and the coverage query goes over core NATS to a
Telemetry stand-in.

Runs when ``MOWDECK_TEST_NATS_URL`` is set (CI starts NATS 2.15 with JetStream). Locally::

    docker run -d --name planner-test-nats -p 4222:4222 nats:2.15.0-alpine -js
    MOWDECK_TEST_NATS_URL=nats://127.0.0.1:4222 uv run pytest services/planner -k nats
"""

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    TRACK_PRECISION,
    MowerMapSnapshot,
    PositionTrackReply,
    PositionTrackRequest,
    encode_polyline,
)
from mowdeck_contracts.v1.subjects import TELEMETRY_POSITIONS_QUERY, device_subject
from mowdeck_control import default_map
from mowdeck_service_kit.bus import BusMessage, JetStreamBus
from planner_support import API, T0, harness, sim_capabilities

NATS_URL = os.environ.get("MOWDECK_TEST_NATS_URL")

pytestmark = pytest.mark.skipif(not NATS_URL, reason="needs MOWDECK_TEST_NATS_URL")


async def eventually(probe: Callable[[], Awaitable[bool]], seconds: float = 10.0) -> None:
    """Wait until ``probe()`` is true (polls every 0.1 s)."""
    for _ in range(int(seconds * 10)):
        if await probe():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"not true within {seconds} s")


async def test_planner_nats_snapshot_and_track_query() -> None:
    assert NATS_URL
    gateway = JetStreamBus(NATS_URL, name="gateway-test")
    await gateway.connect()
    bus = JetStreamBus(NATS_URL, name="planner-test")
    await bus.connect()
    early_mower = uuid4()

    async def telemetry(message: BusMessage) -> None:
        request = message.model(PositionTrackRequest)
        points = [(48.137154 + i * 1e-6, 11.576124) for i in range(10)]
        await message.respond(
            PositionTrackReply(
                mower_id=request.mower_id,
                count=len(points),
                track=encode_polyline(points, TRACK_PRECISION),
            )
        )

    responder = await gateway.subscribe(TELEMETRY_POSITIONS_QUERY, telemetry, queue="telemetry")
    try:
        await gateway.publish(
            device_subject(early_mower, "map"),
            MowerMapSnapshot(
                mower_id=early_mower, account_id=uuid4(), observed_at=T0, map=default_map()
            ),
        )
        async with harness(bus=bus, nats_enabled=True) as h:

            async def early_known() -> bool:
                return early_mower in h.store.data.facts

            await eventually(early_known)
            owner = await h.owner()
            mower_id = await h.register(owner)
            h.bus = gateway  # type: ignore[assignment]  # publish like the Device-Gateway
            await h.publish_map(mower_id, owner.account_id)
            await h.publish_capabilities(mower_id, owner.account_id, sim_capabilities())

            async def known() -> bool:
                facts = h.store.data.facts.get(mower_id)
                return facts is not None and facts.capabilities is not None

            await eventually(known)
            response = await h.client.post(
                f"{API}/coverage/gaps",
                headers=owner.headers,
                json={
                    "mower_id": str(mower_id),
                    "start": (T0 - timedelta(hours=1)).isoformat(),
                    "end": T0.isoformat(),
                    "area": {"area_id": "front"},
                },
            )
    finally:
        await responder.unsubscribe()
        await gateway.close()
    assert response.status_code == 200, response.text
    assert response.json()["track_points"] == 10
