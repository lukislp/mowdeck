"""JetStream and core NATS: the whole recover flow over a real server (telemetry and maps from
the streams, the photo request, Fleet's requests, frames on ``command.mower.<id>``), and a
late subscriber that still finds the newest state and map (``last_per_subject``).

Runs when ``MOWDECK_TEST_NATS_URL`` is set (CI starts NATS 2.15 with JetStream). Locally::

    docker run -d --name vision-test-nats -p 4222:4222 \\
        public.ecr.aws/docker/library/nats:2.15.0-alpine -js
    MOWDECK_TEST_NATS_URL=nats://127.0.0.1:4222 uv run pytest services/vision -k nats
"""

import os

import pytest
from mowdeck_contracts.v1 import DriveEndReason, ManualDrive
from mowdeck_service_kit.bus import JetStreamBus
from vision_support import API, harness, wait_for

from vision.adapters.bus.subscriptions import subscribe_all
from vision.adapters.bus.view import BusMowerView
from vision.adapters.memory import MemoryStore
from vision.domain.models import ProposalState

NATS_URL = os.environ.get("MOWDECK_TEST_NATS_URL")
pytestmark = pytest.mark.skipif(not NATS_URL, reason="needs MOWDECK_TEST_NATS_URL")


async def test_vision_nats_recover_flow_over_jetstream() -> None:
    assert NATS_URL
    bus = JetStreamBus(NATS_URL, name="vision-test")
    await bus.connect()
    async with harness(bus=bus, nats_enabled=True) as h:
        owner = h.owner()
        rig = await h.rig(owner)
        rig.sim.inject_stuck()
        created = (
            await h.client.post(
                f"{API}/mowers/{rig.mower_id}/recover", json={}, headers=owner.headers
            )
        ).json()
        proposal = await h.until(created["id"], owner, ProposalState.PROPOSED)
        assert proposal["image_count"] == 1, "the photo came over core NATS"
        await h.client.post(f"{API}/proposals/{created['id']}/approve", headers=owner.headers)
        done = await h.until(
            created["id"], owner, ProposalState.RECOVERED, ProposalState.NOT_RECOVERED
        )
        assert done["state"] == "recovered", done["detail"]
        assert h.fleet.stops[-1].reason is DriveEndReason.COMPLETED
        assert any(isinstance(c.command, ManualDrive) for c in rig.commands)
        # A view that subscribes late starts with the newest state and map per mower.
        late = BusMowerView()
        subscriptions = await subscribe_all(bus, late, MemoryStore())
        try:
            await wait_for(
                lambda: (
                    late.state(rig.mower_id) is not None
                    and late.mower_map(rig.mower_id) is not None
                )
            )
        finally:
            for subscription in subscriptions:
                await subscription.unsubscribe()
