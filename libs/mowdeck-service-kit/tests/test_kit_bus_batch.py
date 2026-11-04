# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: Apache-2.0

"""Batch subscriptions (ADR 0078): the worker that batches deliveries, the in-memory bus and,
with ``MOWDECK_TEST_NATS_URL``, JetStream (acknowledged only after the handler, flushed on
unsubscribe, redelivered after a failure)."""

import asyncio
import os
from collections.abc import Callable
from uuid import uuid4

import pytest
from mowdeck_service_kit.bus import BusMessage, DropMessage, InMemoryBus, JetStreamBus, Outcome
from mowdeck_service_kit.bus.base import BatchWorker, check_batch_limits, dispatch_batch

NATS_URL = os.environ.get("MOWDECK_TEST_NATS_URL")
requires_nats = pytest.mark.skipif(not NATS_URL, reason="needs MOWDECK_TEST_NATS_URL")


def _message(n: int) -> BusMessage:
    return BusMessage(subject=f"telemetry.mower.{n}", data=str(n).encode())


class Recorder:
    """A batch handler that records its batches and answers configurable outcomes."""

    def __init__(self, answer: Callable[[BusMessage], Outcome] = lambda _: "ack") -> None:
        self.batches: list[list[int]] = []
        self.answer = answer
        self.gate: asyncio.Event | None = None

    async def __call__(self, messages: list[BusMessage]) -> list[Outcome]:
        if self.gate is not None:
            await self.gate.wait()
        self.batches.append([int(m.data) for m in messages])
        return [self.answer(m) for m in messages]


def _worker(
    handler: Recorder, settled: list[tuple[int, Outcome]], *, max_batch: int = 10, wait: float = 0
) -> BatchWorker[int]:
    async def settle(item: int, _message: BusMessage, outcome: Outcome) -> None:
        settled.append((item, outcome))

    return BatchWorker(
        handler,
        to_message=_message,
        settle=settle,
        max_batch=max_batch,
        max_wait_seconds=wait,
        name="test",
    )


async def test_kit_batch_worker_batches_what_queued_in_order() -> None:
    handler = Recorder()
    settled: list[tuple[int, Outcome]] = []
    worker = _worker(handler, settled, max_batch=3)
    handler.gate = asyncio.Event()
    worker.start()
    for n in range(7):
        worker.put(n)
    await asyncio.sleep(0)  # the first batch takes what is queued (up to 3) and waits at the gate
    handler.gate.set()
    await worker.stop(5)
    assert handler.batches == [[0, 1, 2], [3, 4, 5], [6]]
    assert settled == [(n, "ack") for n in range(7)]  # settled in delivery order


async def test_kit_batch_worker_lingers_for_more() -> None:
    handler = Recorder()
    settled: list[tuple[int, Outcome]] = []
    worker = _worker(handler, settled, wait=0.2)
    worker.start()
    worker.put(1)
    await asyncio.sleep(0.05)
    worker.put(2)  # arrives within the linger: same batch
    await asyncio.sleep(0.4)
    worker.put(3)
    await worker.stop(5)
    assert handler.batches == [[1, 2], [3]]


async def test_kit_batch_worker_without_linger_does_not_wait() -> None:
    handler = Recorder()
    settled: list[tuple[int, Outcome]] = []
    worker = _worker(handler, settled)
    worker.start()
    worker.put(1)
    await asyncio.sleep(0.05)
    assert handler.batches == [[1]]
    assert settled == [(1, "ack")]
    await worker.stop(5)


async def test_kit_batch_worker_stop_flushes_everything_queued() -> None:
    """Shutdown: what was delivered before ``stop`` is handled and settled, nothing is lost."""
    handler = Recorder()
    settled: list[tuple[int, Outcome]] = []
    worker = _worker(handler, settled, max_batch=2, wait=1.0)
    worker.start()
    for n in range(5):
        worker.put(n)
    await worker.stop(5)
    assert [n for batch in handler.batches for n in batch] == [0, 1, 2, 3, 4]
    assert [n for n, _ in settled] == [0, 1, 2, 3, 4]
    await worker.stop(5)  # idempotent


async def test_kit_batch_worker_stop_in_time_leaves_the_rest_unsettled() -> None:
    """A handler that does not finish within the stop timeout is cancelled: its messages are
    not settled, so the server redelivers them (never acknowledged without being handled)."""
    handler = Recorder()
    settled: list[tuple[int, Outcome]] = []
    handler.gate = asyncio.Event()  # never opened
    worker = _worker(handler, settled)
    worker.start()
    worker.put(1)
    await asyncio.sleep(0)
    await worker.stop(0.1)
    assert settled == []


async def test_kit_batch_worker_outcomes_per_message() -> None:
    handler = Recorder(lambda m: "term" if m.data == b"2" else "ack")
    settled: list[tuple[int, Outcome]] = []
    worker = _worker(handler, settled)
    worker.start()
    for n in (1, 2, 3):
        worker.put(n)
    await worker.stop(5)
    assert settled == [(1, "ack"), (2, "term"), (3, "ack")]


async def test_kit_batch_worker_settle_failure_does_not_stop_the_others() -> None:
    handler = Recorder()
    settled: list[tuple[int, Outcome]] = []

    async def settle(item: int, _message: BusMessage, outcome: Outcome) -> None:
        if item == 1:
            raise RuntimeError("ack lost")
        settled.append((item, outcome))

    worker = BatchWorker(
        handler, to_message=_message, settle=settle, max_batch=10, max_wait_seconds=0, name="t"
    )
    worker.start()
    for n in (0, 1, 2):
        worker.put(n)
    await worker.stop(5)
    assert settled == [(0, "ack"), (2, "ack")]


async def test_kit_dispatch_batch_maps_failures() -> None:
    messages = [_message(1), _message(2)]

    async def boom(_: list[BusMessage]) -> list[Outcome]:
        raise RuntimeError("database down")

    async def poison(_: list[BusMessage]) -> list[Outcome]:
        raise DropMessage("poison")

    async def short(_: list[BusMessage]) -> list[Outcome]:
        return ["ack"]

    assert await dispatch_batch(boom, messages) == ["nak", "nak"]
    assert await dispatch_batch(poison, messages) == ["term", "term"]
    assert await dispatch_batch(short, messages) == ["nak", "nak"]


def test_kit_batch_limits() -> None:
    check_batch_limits(1, 0)
    with pytest.raises(ValueError, match="max_batch"):
        check_batch_limits(0, 0)
    with pytest.raises(ValueError, match="max_wait"):
        check_batch_limits(5, -1)
    with pytest.raises(ValueError, match="max_wait"):
        check_batch_limits(5, 60)


async def test_kit_memory_bus_batch_subscription_redelivers_and_drops() -> None:
    bus = InMemoryBus(max_deliver=3)
    attempts: list[int] = []

    async def handler(messages: list[BusMessage]) -> list[Outcome]:
        attempts.extend(m.delivery_attempt for m in messages)
        if messages[0].data == b"poison":
            return ["term"]
        return ["nak"] if messages[0].delivery_attempt < 2 else ["ack"]

    await bus.subscribe_batch("telemetry.mower.*", handler, durable="t", queue="t")
    await bus.publish("telemetry.mower.1", b"ok")
    assert attempts == [1, 2]
    await bus.publish("telemetry.mower.1", b"poison")
    assert [m.data for m in bus.dropped] == [b"poison"]
    with pytest.raises(ValueError, match="stream subject"):
        await bus.subscribe_batch("presence.accounts", handler)


@pytest.fixture
async def nats_bus() -> JetStreamBus:
    assert NATS_URL is not None
    bus = JetStreamBus(
        NATS_URL,
        name="kit-batch-tests",
        max_deliver=3,
        nak_delay_seconds=0.05,
        ack_wait_seconds=1.0,
    )
    await bus.connect()
    return bus


async def _eventually(predicate: Callable[[], bool], within: float = 5.0) -> None:
    for _ in range(int(within / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


@requires_nats
async def test_kit_jetstream_batch_subscription(nats_bus: JetStreamBus) -> None:
    """Batches in delivery order, a failed batch redelivered, the rest acknowledged."""
    mower = uuid4()
    subject = f"telemetry.mower.{mower}"
    durable = f"kit-batch-{uuid4().hex[:8]}"
    seen: list[tuple[bytes, int]] = []
    failed_once = False

    async def handler(messages: list[BusMessage]) -> list[Outcome]:
        nonlocal failed_once
        if not failed_once and any(m.data == b"3" for m in messages):
            failed_once = True
            raise RuntimeError("database down")  # the whole batch comes again
        seen.extend((m.data, m.delivery_attempt) for m in messages)
        return ["ack"] * len(messages)

    handle = await nats_bus.subscribe_batch(
        f"telemetry.mower.{mower}",
        handler,
        durable=durable,
        queue=durable,
        max_batch=4,
        max_wait_seconds=0.1,
    )
    try:
        for n in range(1, 6):
            await nats_bus.publish(subject, str(n).encode())
        await _eventually(lambda: {d for d, _ in seen} == {b"1", b"2", b"3", b"4", b"5"})
        assert any(d == b"3" and attempt == 2 for d, attempt in seen)
        assert handle.worker.batches < 5 + 1  # at least two of them shared a batch
    finally:
        await nats_bus.close()  # stops the batch worker first
    assert handle.worker.queued == 0


@requires_nats
async def test_kit_jetstream_batch_unsubscribe_settles_what_arrived(
    nats_bus: JetStreamBus,
) -> None:
    """Unsubscribing hands the received messages to the handler and acknowledges them, so a
    new subscriber on the same durable gets nothing twice."""
    mower = uuid4()
    subject = f"telemetry.mower.{mower}"
    durable = f"kit-batch-{uuid4().hex[:8]}"
    first: list[bytes] = []
    second: list[bytes] = []

    async def slow(messages: list[BusMessage]) -> list[Outcome]:
        await asyncio.sleep(0.05)
        first.extend(m.data for m in messages)
        return ["ack"] * len(messages)

    async def fast(messages: list[BusMessage]) -> list[Outcome]:
        second.extend(m.data for m in messages)
        return ["ack"] * len(messages)

    try:
        handle = await nats_bus.subscribe_batch(
            subject, slow, durable=durable, queue=durable, max_batch=2, max_wait_seconds=1.0
        )
        for n in range(6):
            await nats_bus.publish(subject, str(n).encode())
        await _eventually(lambda: handle.worker.queued > 0 or len(first) == 6)
        await handle.unsubscribe()
        handled = list(first)
        again = await nats_bus.subscribe_batch(subject, fast, durable=durable, queue=durable)
        # Messages the client held but never handed over come again after the ack wait (1 s).
        await _eventually(lambda: len(set(handled) | set(second)) == 6)
        await again.unsubscribe()
    finally:
        await nats_bus.close()
    assert handled  # what arrived before the unsubscribe was handled
    assert not set(handled) & set(second)  # and acknowledged: never delivered again
    assert set(handled) | set(second) == {str(n).encode() for n in range(6)}


async def test_kit_memory_bus_live_view_without_acknowledgement() -> None:
    """``acknowledge=False``: one delivery, no redelivery after a failure; not for shared or
    durable consumers."""
    bus = InMemoryBus(max_deliver=3)
    calls: list[int] = []

    async def failing(message: BusMessage) -> None:
        calls.append(message.delivery_attempt)
        raise RuntimeError("socket gone")

    await bus.subscribe("telemetry.mower.*", failing, acknowledge=False)
    await bus.publish("telemetry.mower.1", b"x")
    assert calls == [1]
    with pytest.raises(ValueError, match="live views"):
        await bus.subscribe("telemetry.mower.*", failing, durable="d", acknowledge=False)
    with pytest.raises(ValueError, match="live views"):
        await bus.subscribe("telemetry.mower.*", failing, queue="q", acknowledge=False)


@requires_nats
async def test_kit_jetstream_live_view_without_acknowledgement(nats_bus: JetStreamBus) -> None:
    """An ``AckPolicy.NONE`` consumer gets every state once, also when its handler fails."""
    mower = uuid4()
    subject = f"telemetry.mower.{mower}"
    received: list[tuple[bytes, int]] = []

    async def handler(message: BusMessage) -> None:
        received.append((message.data, message.delivery_attempt))
        if message.data == b"1":
            raise RuntimeError("socket gone")

    try:
        await nats_bus.subscribe(subject, handler, acknowledge=False)
        for n in range(1, 4):
            await nats_bus.publish(subject, str(n).encode())
        await _eventually(lambda: len(received) >= 3)
        await asyncio.sleep(0.3)  # a redelivery would come after the nak delay (0.05 s)
    finally:
        await nats_bus.close()
    assert received == [(b"1", 1), (b"2", 1), (b"3", 1)]
