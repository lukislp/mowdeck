"""NATS: ``entitlements.check`` over core NATS with the kit client, ``identity.account.created``
on JetStream, and ``entitlements.plan.changed`` dropping the client's cache.

Runs when ``MOWDECK_TEST_NATS_URL`` is set (CI starts NATS 2.15 with JetStream). Locally::

    docker run -d --name entitlements-test-nats -p 4222:4222 nats:2.15.0-alpine -js
    MOWDECK_TEST_NATS_URL=nats://127.0.0.1:4222 uv run pytest services/entitlements -k nats
"""

import asyncio
import os
from collections.abc import Awaitable, Callable
from uuid import uuid4

import pytest
from entitlements_support import harness
from mowdeck_service_kit.bus import JetStreamBus
from mowdeck_service_kit.entitlements import EntitlementsClient

NATS_URL = os.environ.get("MOWDECK_TEST_NATS_URL")

pytestmark = pytest.mark.skipif(not NATS_URL, reason="needs MOWDECK_TEST_NATS_URL")


async def eventually(probe: Callable[[], Awaitable[bool]], seconds: float = 10.0) -> None:
    """Wait until ``probe()`` is true (polls every 0.1 s)."""
    for _ in range(int(seconds * 10)):
        if await probe():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"not true within {seconds} s")


async def test_entitlements_nats_check_and_invalidation_round_trip() -> None:
    assert NATS_URL
    bus = JetStreamBus(NATS_URL, name="entitlements-test")
    await bus.connect()
    other = JetStreamBus(NATS_URL, name="fleet-test")
    await other.connect()
    try:
        async with harness(bus=bus, nats_enabled=True, entitlements_billing_enabled=True) as h:
            client = EntitlementsClient(other, billing_enabled=True, timeout_seconds=2)
            await client.subscribe()
            account_id = uuid4()
            await h.account_created(account_id, name="Nats garden")

            async def has_row() -> bool:
                return (await h.services.entitlements.accounts(0, 50)).total >= 1

            await eventually(has_row)
            reply = await client.check_limit(account_id, "max_mowers", 4)
            assert (reply.allowed, reply.plan, reply.limit) == (False, "home", 4)
            assert len(client) == 1
            operator = await h.operator()
            assigned = await h.assign(operator, account_id, plan="fleet", reason="nats")
            assert assigned.status_code == 200, assigned.text

            async def dropped() -> bool:
                return len(client) == 0

            await eventually(dropped)
            assert (await client.check_limit(account_id, "max_mowers", 4)).allowed
    finally:
        await other.close()
        await bus.close()
