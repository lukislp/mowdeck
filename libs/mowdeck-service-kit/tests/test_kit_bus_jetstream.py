"""JetStreamBus integration tests against a real NATS server.

Skipped unless ``MOWDECK_TEST_NATS_URL`` points at a NATS server with JetStream enabled, e.g.::

    docker run -d --name mowdeck-test-nats -p 4222:4222 \
        public.ecr.aws/docker/library/nats:2.15.0-alpine -js
    MOWDECK_TEST_NATS_URL=nats://127.0.0.1:4222 uv run pytest libs/mowdeck-service-kit
"""

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    EmergencyStop,
    EventEnvelope,
    PrincipalKind,
    PrincipalRef,
)
from mowdeck_contracts.v1.subjects import STREAMS, StreamSpec
from mowdeck_service_kit.bus import (
    BusMessage,
    BusNotConnectedError,
    DropMessage,
    JetStreamBus,
    NoRespondersError,
    RequestTimeoutError,
    publish_command,
    publish_event,
    stream_config,
)
from mowdeck_service_kit.correlation import correlation_scope, get_correlation_id
from mowdeck_service_kit.testing import lifespan_client, make_test_descriptor, make_test_settings

NATS_URL = os.environ.get("MOWDECK_TEST_NATS_URL")

requires_nats = pytest.mark.skipif(
    not NATS_URL,
    reason="JetStream integration tests need MOWDECK_TEST_NATS_URL "
    "(e.g. docker run -p 4222:4222 public.ecr.aws/docker/library/nats:2.15.0-alpine -js)",
)


def test_stream_config_translation() -> None:
    spec = StreamSpec(name="X", subjects=("x.>",), description="d", storage="memory")
    config = stream_config(spec)
    assert config.name == "X"
    assert config.subjects == ["x.>"]
    assert config.duplicate_window == 120.0


@pytest.fixture
async def bus() -> AsyncIterator[JetStreamBus]:
    assert NATS_URL is not None
    instance = JetStreamBus(NATS_URL, name="kit-tests", max_deliver=3, nak_delay_seconds=0.05)
    await instance.connect()
    await instance.connect()  # idempotent
    try:
        yield instance
    finally:
        await instance.close()


async def _wait_for(predicate: Callable[[], bool], within: float = 5.0) -> None:
    for _ in range(int(within / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    msg = "condition not met in time"
    raise AssertionError(msg)


@requires_nats
async def test_streams_are_ensured(bus: JetStreamBus) -> None:
    assert bus.is_connected
    js = bus._jetstream()
    for spec in STREAMS:
        info = await js.stream_info(spec.name)
        assert info.config.subjects == list(spec.subjects)
        assert info.config.duplicate_window == float(spec.duplicate_window_seconds)
    await bus.ensure_streams()  # second run updates in place


@requires_nats
async def test_publish_subscribe_and_dedup(bus: JetStreamBus) -> None:
    mower = uuid4()
    received: list[BusMessage] = []
    correlation: list[str | None] = []

    async def handler(message: BusMessage) -> None:
        received.append(message)
        correlation.append(get_correlation_id())

    sub = await bus.subscribe(f"command.mower.{mower}", handler)
    issuer = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)
    command = CommandEnvelope.create(
        mower_id=mower,
        account_id=uuid4(),
        issued_by=issuer,
        command=EmergencyStop(),
        ttl=timedelta(seconds=30),
        correlation_id="it-corr-1",
    )
    first = await publish_command(bus, command)
    second = await publish_command(bus, command)
    assert first.stream == "COMMANDS"
    assert not first.duplicate
    assert second.duplicate
    assert second.sequence == first.sequence
    await _wait_for(lambda: len(received) >= 1)
    await asyncio.sleep(0.3)  # a duplicate would arrive by now
    assert len(received) == 1
    assert received[0].model(CommandEnvelope) == command
    assert received[0].msg_id == str(command.command_id)
    assert received[0].headers["Mowdeck-Priority"] == "emergency"
    assert correlation == ["it-corr-1"]
    await sub.unsubscribe()
    await sub.unsubscribe()  # second unsubscribe is ignored


@requires_nats
async def test_last_per_subject_starts_with_the_stored_snapshot(bus: JetStreamBus) -> None:
    mower = uuid4()
    subject = f"device.mower.{mower}.capabilities"
    await bus.publish(subject, b"first")
    await bus.publish(subject, b"second")
    received: list[bytes] = []

    async def handler(message: BusMessage) -> None:
        received.append(message.data)

    sub = await bus.subscribe(subject, handler, last_per_subject=True)
    await _wait_for(lambda: len(received) >= 1)
    assert received == [b"second"]
    await bus.publish(subject, b"third")
    await _wait_for(lambda: len(received) >= 2)
    assert received == [b"second", b"third"]
    await sub.unsubscribe()
    with pytest.raises(ValueError, match="last-per-subject"):
        await bus.subscribe("svc.core.x", handler, last_per_subject=True)


@requires_nats
async def test_queue_group_delivers_once(bus: JetStreamBus) -> None:
    domain = f"t{uuid4().hex[:8]}"
    counts = {"a": 0, "b": 0}

    def counter(name: str) -> object:
        async def handler(_: BusMessage) -> None:
            counts[name] += 1

        return handler

    durable = f"grp-{domain}"
    sub_a = await bus.subscribe(f"event.{domain}.>", counter("a"), queue=durable)  # type: ignore[arg-type]
    sub_b = await bus.subscribe(f"event.{domain}.>", counter("b"), queue=durable)  # type: ignore[arg-type]
    for _ in range(6):
        event = EventEnvelope[dict[str, str]](type=f"{domain}.happened", producer="t", payload={})
        await publish_event(bus, event)
    await _wait_for(lambda: counts["a"] + counts["b"] >= 6)
    await asyncio.sleep(0.3)
    assert counts["a"] + counts["b"] == 6
    await sub_a.unsubscribe()
    await sub_b.unsubscribe()


@requires_nats
async def test_nak_redelivers_and_drop_terminates(bus: JetStreamBus) -> None:
    domain = f"t{uuid4().hex[:8]}"
    attempts: list[int] = []
    dropped: list[int] = []

    async def flaky(message: BusMessage) -> None:
        if message.subject.endswith(".poison"):
            dropped.append(message.delivery_attempt)
            raise DropMessage("poison")
        attempts.append(message.delivery_attempt)
        if message.delivery_attempt < 2:
            msg = "transient"
            raise RuntimeError(msg)

    sub = await bus.subscribe(f"event.{domain}.>", flaky)
    await bus.publish(f"event.{domain}.flaky", b"{}")
    await bus.publish(f"event.{domain}.poison", b"{}")
    await _wait_for(lambda: attempts == [1, 2] and dropped == [1])
    await asyncio.sleep(0.3)
    assert attempts == [1, 2]
    assert dropped == [1]
    await sub.unsubscribe()


@requires_nats
async def test_exhausted_redeliveries_are_terminated(bus: JetStreamBus) -> None:
    domain = f"t{uuid4().hex[:8]}"
    attempts: list[int] = []

    async def always_fails(message: BusMessage) -> None:
        attempts.append(message.delivery_attempt)
        msg = "permanent"
        raise RuntimeError(msg)

    sub = await bus.subscribe(f"event.{domain}.>", always_fails)
    await bus.publish(f"event.{domain}.x", b"{}")
    await _wait_for(lambda: len(attempts) >= 3)
    await asyncio.sleep(0.3)
    assert attempts == [1, 2, 3]  # max_deliver=3
    await sub.unsubscribe()


@requires_nats
async def test_request_reply_over_core_nats(bus: JetStreamBus) -> None:
    subject = f"svc.kit-tests.{uuid4().hex[:8]}"

    async def responder(message: BusMessage) -> None:
        await message.respond(b"pong:" + message.data, {"X-Reply": "yes"})

    sub = await bus.subscribe(subject, responder, queue="kit")
    with correlation_scope("req-1"):
        reply = await bus.request(subject, b"ping", timeout=2)
    assert reply.data == b"pong:ping"
    assert reply.headers.get("X-Reply") == "yes"
    core = await bus.publish(subject, b"fire-and-forget")
    assert core.stream is None
    await sub.unsubscribe()

    with pytest.raises(NoRespondersError):
        await bus.request(f"svc.nobody.{uuid4().hex[:8]}", b"", timeout=1)

    async def silent(_: BusMessage) -> None:
        await asyncio.sleep(2)

    slow = await bus.subscribe(f"{subject}.slow", silent)
    with pytest.raises(RequestTimeoutError):
        await bus.request(f"{subject}.slow", b"", timeout=0.2)
    await slow.unsubscribe()
    with pytest.raises(ValueError, match="belongs to a stream"):
        await bus.request("event.a.b", b"")
    with pytest.raises(ValueError, match="durable"):
        await bus.subscribe("svc.x", responder, durable="d")


@requires_nats
async def test_closed_bus_raises(bus: JetStreamBus) -> None:
    await bus.close()
    await bus.close()
    assert not bus.is_connected
    with pytest.raises(BusNotConnectedError):
        await bus.publish("event.a.b", b"")


@requires_nats
async def test_service_connects_and_drains_on_shutdown() -> None:
    from mowdeck_service_kit import create_app

    settings = make_test_settings(nats_enabled=True, nats_url=NATS_URL)
    app = create_app(settings, make_test_descriptor())
    async with lifespan_client(app) as client:
        ready = await client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["checks"]["nats"]["status"] == "ok"
        bus = app.state.bus
        assert isinstance(bus, JetStreamBus)
    assert not bus.is_connected
