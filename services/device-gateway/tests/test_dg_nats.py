"""Full flow through a real NATS JetStream server (skipped without MOWDECK_TEST_NATS_URL).

docker run -d --name mowdeck-test-nats -p 4222:4222 \
    public.ecr.aws/docker/library/nats:2.15.0-alpine -js
MOWDECK_TEST_NATS_URL=nats://127.0.0.1:4222 uv run pytest services/device-gateway
"""

import os

import pytest
from dg_support import ISSUER, TestProvider, envelope, make_settings, registration, wait_until
from mowdeck_contracts.v1 import (
    CameraProvider,
    CameraReleaseReply,
    CameraReleaseRequest,
    CameraSessionReply,
    CameraSessionRequest,
    CommandResult,
    CommandStatus,
    MowerActivity,
    MowerState,
    StartMow,
)
from mowdeck_contracts.v1.subjects import (
    camera_release_subject,
    camera_session_subject,
    command_result_subject,
    telemetry_subject,
)
from mowdeck_service_kit.bus import BusMessage, JetStreamBus, publish_command
from mowdeck_service_kit.testing import lifespan_client

from device_gateway.main import build_app

NATS_URL = os.environ.get("MOWDECK_TEST_NATS_URL")

pytestmark = pytest.mark.skipif(not NATS_URL, reason="MOWDECK_TEST_NATS_URL is not set")


async def test_start_command_result_and_telemetry_through_jetstream() -> None:
    assert NATS_URL is not None
    reg = registration()
    settings = make_settings(
        nats_enabled=True,
        nats_url=NATS_URL,
        bootstrap_mowers=[reg.model_dump(mode="json")],
    )
    app = build_app(settings, connection_provider=TestProvider())
    client = JetStreamBus(NATS_URL, name="device-gateway-test-client")
    await client.connect()
    results: list[CommandResult] = []
    states: list[MowerState] = []

    async def on_result(message: BusMessage) -> None:
        results.append(message.model(CommandResult))

    async def on_state(message: BusMessage) -> None:
        states.append(message.model(MowerState))

    try:
        await client.subscribe(command_result_subject(reg.mower_id), on_result)
        await client.subscribe(telemetry_subject(reg.mower_id), on_state)
        async with lifespan_client(app) as http:
            assert (await http.get("/readyz")).status_code == 200
            await wait_until(lambda: states, within=10)
            command = envelope(reg, StartMow(area_ids=["front"]))
            await publish_command(client, command)
            await wait_until(lambda: results, within=10)
            assert results[0].command_id == command.command_id
            assert results[0].status is CommandStatus.EXECUTED
            await wait_until(
                lambda: any(s.activity is MowerActivity.MOWING for s in states), within=10
            )
    finally:
        await client.close()


async def test_camera_session_request_reply_through_nats() -> None:
    """The camera request is core NATS: JetStream (stream DEVICE) must not answer it first."""
    assert NATS_URL is not None
    reg = registration()
    settings = make_settings(
        nats_enabled=True,
        nats_url=NATS_URL,
        bootstrap_mowers=[reg.model_dump(mode="json")],
    )
    app = build_app(settings, connection_provider=TestProvider())
    client = JetStreamBus(NATS_URL, name="device-gateway-camera-test-client")
    await client.connect()
    states: list[MowerState] = []

    async def on_state(message: BusMessage) -> None:
        states.append(message.model(MowerState))

    try:
        await client.subscribe(telemetry_subject(reg.mower_id), on_state)
        async with lifespan_client(app):
            await wait_until(lambda: states, within=10)
            request = CameraSessionRequest(
                mower_id=reg.mower_id, account_id=reg.account_id, requested_by=ISSUER
            )
            reply = await client.request(camera_session_subject(reg.mower_id), request, 5.0)
            session = reply.model(CameraSessionReply).session
            assert session is not None
            assert session.provider is CameraProvider.FAKE
            # The release is core NATS as well (ADR 0045): no stream may answer it first.
            release = CameraReleaseRequest(
                mower_id=reg.mower_id,
                account_id=reg.account_id,
                requested_by=ISSUER,
                channel=session.channel,
            )
            released = await client.request(camera_release_subject(reg.mower_id), release, 5.0)
            assert released.model(CameraReleaseReply).released is True
    finally:
        await client.close()
