"""InMemoryBus semantics and the envelope publish helpers."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandResult,
    CommandStatus,
    EmergencyStop,
    EventEnvelope,
    PauseMow,
    PrincipalKind,
    PrincipalRef,
)
from mowdeck_contracts.v1.subjects import command_subject
from mowdeck_service_kit.bus import (
    MSG_ID_HEADER,
    PRIORITY_HEADER,
    BusError,
    BusMessage,
    BusNotConnectedError,
    DropMessage,
    EventBus,
    InMemoryBus,
    NoRespondersError,
    RequestTimeoutError,
    Subscription,
    publish_command,
    publish_command_result,
    publish_event,
)
from mowdeck_service_kit.correlation import (
    CORRELATION_HEADER,
    correlation_scope,
    get_correlation_id,
)
from pydantic import ValidationError

ISSUER = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _command(command: Any = None, ttl: timedelta = timedelta(seconds=30)) -> CommandEnvelope:
    return CommandEnvelope.create(
        mower_id=uuid4(),
        account_id=uuid4(),
        issued_by=ISSUER,
        command=command or PauseMow(),
        ttl=ttl,
        correlation_id="corr-cmd",
    )


async def test_implements_protocols() -> None:
    bus = InMemoryBus()
    assert isinstance(bus, EventBus)
    sub = await bus.subscribe("event.>", _noop)
    assert isinstance(sub, Subscription)


async def _noop(_: BusMessage) -> None:
    return None


async def test_publish_and_subscribe_with_wildcards() -> None:
    bus = InMemoryBus()
    received: list[BusMessage] = []

    async def handler(message: BusMessage) -> None:
        received.append(message)

    await bus.subscribe("telemetry.mower.*", handler)
    result = await bus.publish("telemetry.mower.abc", b"{}")
    await bus.publish("event.mower.registered", b"{}")
    assert result.stream == "TELEMETRY"
    assert result.sequence == 1
    assert [m.subject for m in received] == ["telemetry.mower.abc"]
    assert [m.subject for m in bus.messages("event.>")] == ["event.mower.registered"]


async def test_msg_id_deduplication_within_window() -> None:
    clock = Clock()
    bus = InMemoryBus(clock=clock)
    received: list[BusMessage] = []

    async def handler(message: BusMessage) -> None:
        received.append(message)

    await bus.subscribe("command.mower.*", handler)
    first = await bus.publish("command.mower.m1", b"a", msg_id="cmd-1")
    second = await bus.publish("command.mower.m1", b"a", msg_id="cmd-1")
    assert not first.duplicate
    assert second.duplicate
    assert second.sequence == first.sequence
    assert len(received) == 1
    assert received[0].msg_id == "cmd-1"

    clock.now += 121  # COMMANDS duplicate window is 120 s
    third = await bus.publish("command.mower.m1", b"a", msg_id="cmd-1")
    assert not third.duplicate
    assert len(received) == 2
    # Different streams have separate windows; no msg id means no de-duplication.
    assert not (await bus.publish("event.x.y", b"", msg_id="cmd-1")).duplicate
    assert not (await bus.publish("event.x.y", b"")).duplicate
    assert not (await bus.publish("event.x.y", b"")).duplicate


async def test_queue_groups_load_balance_and_fan_out() -> None:
    bus = InMemoryBus()
    calls: dict[str, int] = {"a": 0, "b": 0, "audit": 0}

    def counter(name: str) -> Any:
        async def handler(_: BusMessage) -> None:
            calls[name] += 1

        return handler

    await bus.subscribe("command.mower.*", counter("a"), durable="device-gateway")
    await bus.subscribe("command.mower.*", counter("b"), durable="device-gateway")
    await bus.subscribe("command.mower.*", counter("audit"))
    for _ in range(4):
        await bus.publish("command.mower.m1", b"")
    assert calls == {"a": 2, "b": 2, "audit": 4}


async def test_failed_handler_is_redelivered_then_dropped() -> None:
    bus = InMemoryBus(max_deliver=3)
    attempts: list[int] = []

    async def flaky(message: BusMessage) -> None:
        attempts.append(message.delivery_attempt)
        if message.delivery_attempt < 2:
            msg = "transient"
            raise RuntimeError(msg)

    sub = await bus.subscribe("event.mower.>", flaky)
    await bus.publish("event.mower.registered", b"")
    assert attempts == [1, 2]
    assert bus.dropped == []

    await sub.unsubscribe()
    attempts.clear()

    async def always_fails(message: BusMessage) -> None:
        attempts.append(message.delivery_attempt)
        msg = "permanent"
        raise RuntimeError(msg)

    await bus.subscribe("event.mower.>", always_fails)
    await bus.publish("event.mower.deleted", b"")
    assert attempts == [1, 2, 3]
    assert [m.subject for m in bus.dropped] == ["event.mower.deleted"]


async def test_drop_message_terminates_without_retry() -> None:
    bus = InMemoryBus()
    attempts = 0

    async def poison(_: BusMessage) -> None:
        nonlocal attempts
        attempts += 1
        raise DropMessage("cannot parse")

    await bus.subscribe("event.>", poison)
    await bus.publish("event.a.b", b"garbage")
    assert attempts == 1
    assert len(bus.dropped) == 1


async def test_core_subjects_and_request_reply() -> None:
    bus = InMemoryBus()
    notified: list[bytes] = []

    async def responder(message: BusMessage) -> None:
        await message.respond(b"pong:" + message.data, {"X-Reply": "1"})

    async def listener(message: BusMessage) -> None:
        notified.append(message.data)

    await bus.subscribe("svc.fleet.ping", responder, queue="fleet")
    await bus.subscribe("svc.notify", listener)
    reply = await bus.request("svc.fleet.ping", b"1", timeout=1)
    assert reply.data == b"pong:1"
    assert reply.headers == {"X-Reply": "1"}
    result = await bus.publish("svc.notify", b"hello")
    assert result.stream is None
    assert notified == [b"hello"]


async def test_request_errors() -> None:
    bus = InMemoryBus()
    with pytest.raises(NoRespondersError):
        await bus.request("svc.nobody", b"")
    with pytest.raises(ValueError, match="belongs to a stream"):
        await bus.request("event.a.b", b"")

    async def silent(_: BusMessage) -> None:
        await asyncio.sleep(10)

    await bus.subscribe("svc.slow", silent)
    with pytest.raises(RequestTimeoutError):
        await bus.request("svc.slow", b"", timeout=0.05)


async def test_respond_without_reply_subject_fails() -> None:
    message = BusMessage(subject="svc.x", data=b"")
    with pytest.raises(BusError, match="no reply subject"):
        await message.respond(b"")


async def test_durable_requires_stream_subject() -> None:
    with pytest.raises(ValueError, match="durable"):
        await InMemoryBus().subscribe("svc.x", _noop, durable="d")


async def test_last_per_subject_replays_the_newest_message_of_each_subject() -> None:
    bus = InMemoryBus()
    first, second = uuid4(), uuid4()
    await bus.publish(f"device.mower.{first}.map", b"old")
    await bus.publish(f"device.mower.{first}.map", b"new")
    await bus.publish(f"device.mower.{second}.capabilities", b"caps")
    seen: list[tuple[str, bytes]] = []

    async def handler(message: BusMessage) -> None:
        seen.append((message.subject, message.data))

    await bus.subscribe("device.mower.*.map", handler, last_per_subject=True)
    assert seen == [(f"device.mower.{first}.map", b"new")]
    await bus.publish(f"device.mower.{second}.map", b"live")
    assert seen[-1] == (f"device.mower.{second}.map", b"live")
    plain: list[bytes] = []

    async def other(message: BusMessage) -> None:
        plain.append(message.data)

    await bus.subscribe("device.mower.*.map", other)  # new messages only
    assert plain == []
    with pytest.raises(ValueError, match="last-per-subject"):
        await bus.subscribe("svc.x", _noop, last_per_subject=True)


async def test_closed_bus_rejects_operations() -> None:
    bus = InMemoryBus()
    await bus.close()
    await bus.close()  # idempotent
    assert not bus.is_connected
    with pytest.raises(BusNotConnectedError):
        await bus.publish("event.a.b", b"")
    with pytest.raises(BusNotConnectedError):
        await bus.subscribe("event.a.b", _noop)
    with pytest.raises(BusNotConnectedError):
        await bus.request("svc.a", b"")


async def test_correlation_id_travels_with_messages() -> None:
    bus = InMemoryBus()
    seen: list[str | None] = []

    async def handler(message: BusMessage) -> None:
        seen.append(get_correlation_id())

    await bus.subscribe("event.>", handler)
    with correlation_scope("from-http"):
        await bus.publish("event.a.b", b"")
    await bus.publish("event.a.b", b"")
    assert seen[0] == "from-http"
    assert seen[1] is not None
    assert seen[1] != "from-http"
    assert bus.published[0].headers[CORRELATION_HEADER] == "from-http"


async def test_publish_event_helper() -> None:
    bus = InMemoryBus()
    event = EventEnvelope[dict[str, Any]](
        type="mower.registered", producer="fleet", payload={"name": "Luba"}
    )
    received: list[EventEnvelope[dict[str, Any]]] = []

    async def handler(message: BusMessage) -> None:
        received.append(message.model(EventEnvelope[dict[str, Any]]))

    await bus.subscribe("event.mower.>", handler)
    first = await publish_event(bus, event)
    second = await publish_event(bus, event)
    assert not first.duplicate
    assert second.duplicate
    assert received == [event]
    headers = bus.published[0].headers
    assert headers[MSG_ID_HEADER] == str(event.id)
    assert headers[CORRELATION_HEADER] == event.correlation_id
    assert headers["Mowdeck-Event-Type"] == "mower.registered"
    assert headers["Content-Type"] == "application/json"


async def test_publish_command_helper() -> None:
    bus = InMemoryBus()
    stop = _command(EmergencyStop())
    result = await publish_command(bus, stop)
    assert result.stream == "COMMANDS"
    message = bus.published[0]
    assert message.subject == command_subject(stop.mower_id)
    assert message.msg_id == str(stop.command_id)
    assert message.headers[PRIORITY_HEADER] == "emergency"
    assert message.model(CommandEnvelope) == stop
    assert (await publish_command(bus, stop)).duplicate


async def test_publish_command_rejects_expired_commands() -> None:
    expired = CommandEnvelope.create(
        mower_id=uuid4(),
        account_id=uuid4(),
        issued_by=ISSUER,
        command=PauseMow(),
        ttl=timedelta(seconds=1),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="expired"):
        await publish_command(InMemoryBus(), expired)


async def test_publish_command_result_helper() -> None:
    bus = InMemoryBus()
    command = _command()
    accepted = CommandResult(
        command_id=command.command_id,
        mower_id=command.mower_id,
        status=CommandStatus.ACCEPTED,
        correlation_id=command.correlation_id,
    )
    executed = accepted.model_copy(update={"status": CommandStatus.EXECUTED})
    assert not (await publish_command_result(bus, accepted)).duplicate
    assert (await publish_command_result(bus, accepted)).duplicate
    assert not (await publish_command_result(bus, executed)).duplicate
    assert bus.published[0].subject == f"command.result.mower.{command.mower_id}"
    assert bus.published[0].headers[CORRELATION_HEADER] == command.correlation_id
    no_correlation = accepted.model_copy(update={"correlation_id": None, "command_id": uuid4()})
    await publish_command_result(bus, no_correlation)


async def test_model_decoding_errors_surface() -> None:
    with pytest.raises(ValidationError):
        BusMessage(subject="x", data=b"{}").model(CommandEnvelope)
