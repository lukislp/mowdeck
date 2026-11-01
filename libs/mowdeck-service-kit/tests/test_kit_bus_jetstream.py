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

import nats
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
from mowdeck_service_kit.bus.base import BusError
from mowdeck_service_kit.bus.jetstream import _NatsSubscriptionHandle, durable_deliver_subject
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


def test_credentials_come_from_the_settings_and_stay_hidden() -> None:
    settings = make_test_settings(
        service_name="camera-relay",
        nats_url="nats://a:4222, nats://b:4222",
        nats_user="camera-relay",
        nats_password="hidden-relay-password-0123456789",  # noqa: S106 - a test value
    )
    bus = JetStreamBus.from_settings(settings)
    assert bus.servers == ["nats://a:4222", "nats://b:4222"]
    assert bus.user == "camera-relay"
    assert bus.inbox_prefix == "_INBOX.camera-relay"
    assert bus.manage_streams is False  # the nats-streams job does it (ADR 0073)
    assert "hidden-relay-password" not in repr(bus)
    assert "hidden-relay-password" not in str(vars(bus).get("user"))
    assert JetStreamBus.from_settings(settings, manage_streams=True).manage_streams is True


def test_without_credentials_the_bus_manages_the_streams() -> None:
    bus = JetStreamBus.from_settings(make_test_settings(nats_url="nats://x:4222"))
    assert bus.user is None
    assert bus.inbox_prefix == "_INBOX"
    assert bus.manage_streams is True


def test_user_and_password_come_together() -> None:
    with pytest.raises(ValueError, match="password"):
        JetStreamBus("nats://x:4222", user="fleet")
    with pytest.raises(ValueError, match="password"):
        JetStreamBus("nats://x:4222", password="x")  # noqa: S106 - a test value


def test_durable_deliver_subject_lives_in_the_inbox() -> None:
    assert (
        durable_deliver_subject("_INBOX.fleet", "EVENTS", "fleet-accounts")
        == "_INBOX.fleet.durable.EVENTS.fleet-accounts"
    )


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
async def test_a_durable_consumer_of_an_older_release_moves_into_the_inbox(
    bus: JetStreamBus,
) -> None:
    """Before ADR 0073 nats-py chose a random _INBOX.<nuid>; the bus keeps the consumer (and
    its position) and only delivers to its own inbox from then on."""
    assert NATS_URL is not None
    domain = f"t{uuid4().hex[:8]}"
    durable = f"legacy-{domain}"
    legacy = await nats.connect(NATS_URL)
    try:
        js = legacy.jetstream()
        old = await js.subscribe(
            f"event.{domain}.>", durable=durable, stream="EVENTS", manual_ack=True
        )
        before = await js.consumer_info("EVENTS", durable)
        assert before.config.deliver_subject is not None
        assert before.config.deliver_subject.count(".") == 1  # _INBOX.<nuid>
        await old.unsubscribe()
    finally:
        await legacy.close()
    received: list[str] = []

    async def handler(message: BusMessage) -> None:
        received.append(message.subject)

    sub = await bus.subscribe(f"event.{domain}.>", handler, durable=durable)
    after = await bus._jetstream().consumer_info("EVENTS", durable)
    assert after.config.deliver_subject == f"_INBOX.durable.EVENTS.{durable}"
    # The client listens on the new subject, not on the one the update's answer may name.
    assert isinstance(sub, _NatsSubscriptionHandle)
    assert sub.subscription.subject == f"_INBOX.durable.EVENTS.{durable}"
    assert after.config.filter_subject == f"event.{domain}.>"
    await bus.publish(f"event.{domain}.moved", b"{}")
    await _wait_for(lambda: received == [f"event.{domain}.moved"])
    await sub.unsubscribe()
    # A second subscribe finds the consumer in place and binds to it unchanged.
    again = await bus.subscribe(f"event.{domain}.>", handler, durable=durable)
    await again.unsubscribe()
    with pytest.raises(ValueError, match="queue"):
        await bus.subscribe(f"event.{domain}.>", handler, durable="a", queue="b")


@requires_nats
async def test_without_managing_streams_the_bus_waits_for_them() -> None:
    assert NATS_URL is not None
    spec = StreamSpec(
        name=f"WAIT{uuid4().hex[:8].upper()}",
        subjects=(f"wait{uuid4().hex[:8]}.>",),
        description="test",
        storage="memory",
    )
    waiting = JetStreamBus(NATS_URL, streams=(spec,), manage_streams=False, stream_wait_seconds=1)
    with pytest.raises(BusError, match="nats-streams"):
        await waiting.connect()
    await waiting.close()
    manager = JetStreamBus(NATS_URL, streams=(spec,))
    await manager.connect()
    try:
        late = JetStreamBus(NATS_URL, streams=(spec,), manage_streams=False, stream_wait_seconds=5)
        await late.connect()  # the stream exists now
        assert late.is_connected
        await late.close()
    finally:
        await manager._jetstream().delete_stream(spec.name)
        await manager.close()


@requires_nats
def test_the_streams_job_ensures_every_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    from mowdeck_service_kit.bus import streams as job

    assert NATS_URL is not None
    monkeypatch.setenv("MOWDECK_NATS_URL", NATS_URL)
    monkeypatch.setenv("MOWDECK_LOG_FORMAT", "console")
    assert job.main() == 0
    monkeypatch.setenv("MOWDECK_NATS_USER", "streams")  # a user without a password
    assert job.main() == 2


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
