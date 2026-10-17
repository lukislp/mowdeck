"""JetStream: Fleet publishes a command, a durable consumer receives it, its result updates the
record; ``fleet.mowers.list`` over core NATS.

Runs when ``MOWDECK_TEST_NATS_URL`` is set (CI starts NATS 2.15 with JetStream). Locally::

    docker run -d --name fleet-test-nats -p 4222:4222 \
        public.ecr.aws/docker/library/nats:2.15.0-alpine -js
    MOWDECK_TEST_NATS_URL=nats://127.0.0.1:4222 uv run pytest services/fleet -k nats
"""

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fleet_support import API, harness
from mowdeck_contracts.v1 import (
    CameraProvider,
    CameraSessionReply,
    CameraSessionRequest,
    CommandEnvelope,
    CommandResult,
    CommandStatus,
    EventEnvelope,
    MowerListReply,
    MowerListRequest,
)
from mowdeck_contracts.v1.camera import CameraSession
from mowdeck_contracts.v1.relay import (
    CAMERA_RELAY_STOPPED,
    CameraRelayStopped,
    RelayStopReason,
)
from mowdeck_contracts.v1.subjects import (
    ALL_CAMERA_SESSIONS,
    ALL_COMMANDS,
    FLEET_MOWERS_LIST,
    camera_relay_event_subject,
)
from mowdeck_service_kit.bus import BusMessage, JetStreamBus, publish_command_result

from fleet.application.camera import CAMERA_OPENED, CAMERA_STREAM_STOPPED

NATS_URL = os.environ.get("MOWDECK_TEST_NATS_URL")

pytestmark = pytest.mark.skipif(not NATS_URL, reason="needs MOWDECK_TEST_NATS_URL")


async def eventually(probe: Callable[[], Awaitable[bool]], seconds: float = 10.0) -> None:
    """Wait until ``probe()`` is true (polls every 0.1 s)."""
    for _ in range(int(seconds * 10)):
        if await probe():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"not true within {seconds} s")


async def test_fleet_nats_command_round_trip() -> None:
    assert NATS_URL
    bus = JetStreamBus(NATS_URL, name="fleet-test")
    await bus.connect()
    gateway = JetStreamBus(NATS_URL, name="gateway-test")
    await gateway.connect()
    received: list[CommandEnvelope] = []

    async def device_gateway(message: BusMessage) -> None:
        envelope = message.model(CommandEnvelope)
        received.append(envelope)
        await publish_command_result(
            gateway,
            CommandResult(
                command_id=envelope.command_id,
                mower_id=envelope.mower_id,
                status=CommandStatus.EXECUTED,
            ),
        )

    consumer = f"gateway-test-{uuid4().hex[:8]}"
    subscription = await gateway.subscribe(ALL_COMMANDS, device_gateway, durable=consumer)
    try:
        async with harness(bus=bus, nats_enabled=True) as h:
            owner = await h.owner()
            mower_id = (await h.register(owner))["id"]
            reply = await gateway.request(FLEET_MOWERS_LIST, MowerListRequest(), timeout=5)
            listed = {str(r.mower_id) for r in reply.model(MowerListReply).mowers}
            response = await h.client.post(
                f"{API}/mowers/{mower_id}/commands",
                headers=owner.headers,
                json={"command": {"kind": "return_to_dock"}},
            )
            assert response.status_code == 202, response.text
            command_id = response.json()["command_id"]

            async def executed() -> bool:
                view = await h.client.get(f"{API}/commands/{command_id}", headers=owner.headers)
                return bool(view.json()["status"] == "executed")

            await eventually(executed)
    finally:
        await subscription.unsubscribe()
        await gateway.close()
    assert mower_id in listed
    assert [str(e.command_id) for e in received if str(e.mower_id) == mower_id] == [command_id]


async def test_fleet_nats_camera_auto_without_a_relay_and_relay_audit() -> None:
    """Over real NATS: ``auto`` takes the direct path at once when no relay is deployed (no
    responders), and a relay stop event on the ``CAMERA`` stream becomes one audit row even when
    it is published twice (redelivery)."""
    assert NATS_URL
    bus = JetStreamBus(NATS_URL, name="fleet-test-camera")
    await bus.connect()
    gateway = JetStreamBus(NATS_URL, name="gateway-test-camera")
    await gateway.connect()

    async def device_gateway(message: BusMessage) -> None:
        message.model(CameraSessionRequest)
        session = CameraSession(
            provider=CameraProvider.FAKE,
            app_id="",
            channel="sim",
            token="",
            uid=0,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        await message.respond(CameraSessionReply(session=session))

    subscription = await gateway.subscribe(ALL_CAMERA_SESSIONS, device_gateway)
    try:
        async with harness(bus=bus, nats_enabled=True) as h:
            owner = await h.owner()
            mower_id = UUID((await h.register(owner))["id"])
            response = await h.client.post(
                f"{API}/mowers/{mower_id}/camera/session", headers=owner.headers
            )
            assert response.status_code == 200, response.text
            assert response.json()["provider"] == "fake"
            now = datetime.now(UTC)
            envelope = EventEnvelope[Any](
                type=CAMERA_RELAY_STOPPED,
                account_id=owner.account_id,
                producer="camera-relay",
                payload=CameraRelayStopped(
                    relay_session_id=uuid4(),
                    account_id=owner.account_id,
                    mower_id=mower_id,
                    reason=RelayStopReason.IDLE,
                    started_at=now - timedelta(seconds=30),
                    stopped_at=now,
                    peak_viewers=1,
                ).model_dump(mode="json"),
            )
            subject = camera_relay_event_subject(mower_id, "stopped")
            await gateway.publish(subject, envelope)
            await gateway.publish(subject, envelope)  # the same event again

            async def audited() -> bool:
                async with h.store.transaction() as tx:
                    return await tx.audit.count(action=CAMERA_STREAM_STOPPED) == 1

            await eventually(audited)
            await asyncio.sleep(0.5)
            async with h.store.transaction() as tx:
                assert await tx.audit.count(action=CAMERA_STREAM_STOPPED) == 1
                (opened,) = await tx.audit.find(action=CAMERA_OPENED)
            assert opened.details["transport"] == "direct"
    finally:
        await subscription.unsubscribe()
        await gateway.close()
