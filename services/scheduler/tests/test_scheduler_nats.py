"""NATS: a manual run goes over core NATS to Fleet (``fleet.commands.issue``), Fleet publishes the
command on JetStream, the gateway's results come back, and the map and capability snapshots on
the ``DEVICE`` stream give the Scheduler the mower's areas, location and capabilities.

Runs when ``MOWDECK_TEST_NATS_URL`` is set (CI starts NATS 2.15 with JetStream). Locally::

    docker run -d --name scheduler-test-nats -p 4222:4222 nats:2.15.0-alpine -js
    MOWDECK_TEST_NATS_URL=nats://127.0.0.1:4222 uv run pytest services/scheduler -k nats
"""

import asyncio
import os
from collections.abc import Awaitable, Callable

import pytest
from mowdeck_service_kit.bus import JetStreamBus
from scheduler_support import API, harness

NATS_URL = os.environ.get("MOWDECK_TEST_NATS_URL")

pytestmark = pytest.mark.skipif(not NATS_URL, reason="needs MOWDECK_TEST_NATS_URL")


async def eventually(probe: Callable[[], Awaitable[bool]], seconds: float = 10.0) -> None:
    """Wait until ``probe()`` is true (polls every 0.1 s)."""
    for _ in range(int(seconds * 10)):
        if await probe():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"not true within {seconds} s")


async def test_scheduler_nats_run_now_goes_through_fleet() -> None:
    assert NATS_URL
    bus = JetStreamBus(NATS_URL, name="scheduler-test")
    await bus.connect()
    gateway = JetStreamBus(NATS_URL, name="gateway-test")
    await gateway.connect()
    try:
        async with harness(bus=bus, nats_enabled=True) as h:
            owner = await h.owner()
            mower_id = await h.register(owner)
            h.bus = gateway  # type: ignore[assignment]  # publish like the Device-Gateway
            await h.map(mower_id, owner.account_id, areas=("lawn",))

            async def mapped() -> bool:
                return mower_id in h.store.data.places

            await eventually(mapped)
            await h.capabilities(mower_id, owner.account_id, native_patterns=["stripes"])

            async def capable() -> bool:
                return mower_id in h.store.data.capabilities

            await eventually(capable)
            created = await h.schedule(
                owner, target_type="mower", target_id=str(mower_id), area_ids=[]
            )
            response = await h.client.post(
                f"{API}/schedules/{created['id']}/run-now", headers=owner.headers
            )
            assert response.status_code == 202, response.text
            [run] = response.json()["items"]

            async def executed() -> bool:
                record = await h.fleet_command(owner, run["command_id"])
                return bool(record["status"] == "executed")

            await eventually(executed)
            record = await h.fleet_command(owner, run["command_id"])
    finally:
        await gateway.close()
    assert run["outcome"] == "started" and run["details"] == "areas lawn, angle 0"
    assert record["origin"] == "scheduler" and record["schedule_id"] == created["id"]
