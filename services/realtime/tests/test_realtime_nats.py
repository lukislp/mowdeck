"""JetStream: a state and a command result published on NATS reach clients of two instances.

Two Realtime apps share one NATS server; both receive every message (ephemeral consumers, no
queue group), so a client on either instance gets the update. Runs when
``MOWDECK_TEST_NATS_URL`` is set (CI starts NATS 2.15 with JetStream). Locally::

    docker run -d --name realtime-test-nats -p 4222:4222 \
        public.ecr.aws/docker/library/nats:2.15.0-alpine -js
    MOWDECK_TEST_NATS_URL=nats://127.0.0.1:4222 uv run pytest services/realtime -k nats
"""

import os
from uuid import uuid4

import pytest
from mowdeck_authz.testing import InMemoryAuthorizer
from mowdeck_contracts.v1 import CommandResult, CommandStatus, MowerActivity
from mowdeck_contracts.v1.subjects import telemetry_subject
from mowdeck_service_kit.bus import JetStreamBus, publish_command_result
from realtime_support import harness, make_state

NATS_URL = os.environ.get("MOWDECK_TEST_NATS_URL")

pytestmark = pytest.mark.skipif(not NATS_URL, reason="needs MOWDECK_TEST_NATS_URL")


async def test_realtime_nats_jetstream_publish_reaches_clients_of_every_instance() -> None:
    assert NATS_URL
    first_bus = JetStreamBus(NATS_URL, name="realtime-test-1")
    second_bus = JetStreamBus(NATS_URL, name="realtime-test-2")
    gateway = JetStreamBus(NATS_URL, name="gateway-test")
    for bus in (first_bus, second_bus, gateway):
        await bus.connect()
    authz = InMemoryAuthorizer()  # shared, like one OpenFGA store
    try:
        async with (
            harness(bus=first_bus, authz=authz, nats_enabled=True) as one,
            harness(bus=second_bus, authz=authz, nats_enabled=True) as two,
        ):
            owner = await one.owner()
            mower_id = await one.add_mower(owner)
            on_one = await one.connect()
            await on_one.auth(owner.token)
            on_two = await two.connect()
            await on_two.auth(two.keys.mint(owner.principal))
            await on_one.subscribe(mower_id)
            await on_two.subscribe(mower_id)
            readiness = (one.app.state.bus.is_connected, two.app.state.bus.is_connected)
            assert readiness == (True, True)

            await gateway.publish(
                telemetry_subject(mower_id),
                make_state(mower_id, owner.account_id, activity=MowerActivity.MOWING),
            )
            command_id = uuid4()
            await publish_command_result(
                gateway,
                CommandResult(
                    command_id=command_id, mower_id=mower_id, status=CommandStatus.EXECUTED
                ),
            )
            for client in (on_one, on_two):
                state = await client.expect("state", timeout=10)
                assert state["state"]["activity"] == "mowing"
                result = await client.expect("command_result", timeout=10)
                assert result["result"]["command_id"] == str(command_id)
    finally:
        await gateway.close()
