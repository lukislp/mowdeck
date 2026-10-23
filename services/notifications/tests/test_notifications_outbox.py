"""The delivery worker with fake channels: success, retry with backoff, give up, gone, a hung
channel (deadline, isolation, per-channel limit), lease reclaim, expiry and stall detection."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import NotificationKind, NotificationSeverity
from notifications_support import AUTH, P256DH, T0, ManualClock, RecordingChannel

from notifications.adapters.channels import ChannelRegistry
from notifications.adapters.memory import MemoryStore
from notifications.application.context import NotificationsConfig
from notifications.application.outbox import Outbox
from notifications.domain.models import (
    Delivery,
    DeliveryOutcome,
    DeliveryResult,
    DeliveryStatus,
    InboxItem,
    Message,
    NewDelivery,
    NtfyTarget,
    PushSubscription,
    TargetKind,
)
from notifications.main import delivery_worker, outbox_watchdog

CONFIG = NotificationsConfig(
    max_attempts=3, backoff_base=timedelta(seconds=30), backoff_max=timedelta(minutes=5)
)


class Observer:
    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []

    def delivered(self, channel: str, status: str) -> None:
        self.seen.append((channel, status))


def message() -> Message:
    return Message(
        kind=NotificationKind.TEST,
        severity=NotificationSeverity.INFO,
        title="t",
        body="b",
        url=None,
        tag="test",
        mower_id=None,
    )


async def enqueue(
    store: MemoryStore,
    channel: str = "log",
    *,
    user: UUID | None = None,
    account: UUID | None = None,
    target_kind: TargetKind = TargetKind.NONE,
    target_id: UUID | None = None,
) -> UUID:
    user = user or uuid4()
    account = account or uuid4()
    item = InboxItem(
        id=uuid4(),
        user_id=user,
        account_id=account,
        mower_id=None,
        kind=NotificationKind.TEST,
        severity=NotificationSeverity.INFO,
        title="t",
        body="b",
        url=None,
        dedupe_key=str(uuid4()),
        occurred_at=T0,
        created_at=T0,
    )
    delivery = NewDelivery(user, account, channel, message(), target_kind, target_id)
    before = set(store.data.deliveries)
    assert await store.record(item, [delivery], 200)
    (created,) = set(store.data.deliveries) - before
    return created


def setup(
    *channels: RecordingChannel,
) -> tuple[MemoryStore, ManualClock, Outbox, Observer]:
    store, clock, observer = MemoryStore(), ManualClock(), Observer()
    registry = ChannelRegistry({c.name: c for c in channels})
    return store, clock, Outbox(store, registry, CONFIG, clock, observer), observer


async def test_notifications_outbox_sends_once() -> None:
    channel = RecordingChannel()
    store, _, outbox, observer = setup(channel)
    delivery_id = await enqueue(store)
    assert await outbox.run_once() == 1
    assert await outbox.run_once() == 0
    row = store.data.deliveries[delivery_id]
    assert (row.status, row.attempts, row.last_error) == (DeliveryStatus.SENT, 1, None)
    assert observer.seen == [("log", "sent")] and len(channel.sent) == 1


async def test_notifications_outbox_retries_with_backoff_then_gives_up() -> None:
    channel = RecordingChannel(
        results=[DeliveryResult(DeliveryOutcome.RETRY, "503")] * 3,
    )
    store, clock, outbox, observer = setup(channel)
    delivery_id = await enqueue(store)
    row = store.data.deliveries[delivery_id]
    assert await outbox.run_once() == 1
    assert row.status is DeliveryStatus.PENDING and row.next_attempt_at == T0 + timedelta(
        seconds=30
    )
    clock.advance(29)
    assert await outbox.run_once() == 0  # not due yet
    clock.advance(1)
    assert await outbox.run_once() == 1
    assert row.next_attempt_at == clock.now + timedelta(seconds=60)
    clock.advance(60)
    assert await outbox.run_once() == 1
    final = store.data.deliveries[delivery_id]  # a fresh read for the type checker
    assert final.status is DeliveryStatus.FAILED and final.attempts == 3
    assert row.last_error == "gave up after 3 attempts: 503"
    assert observer.seen == [("log", "retry"), ("log", "retry"), ("log", "failed")]


async def test_notifications_outbox_retry_after_and_exceptions() -> None:
    channel = RecordingChannel(
        results=[DeliveryResult(DeliveryOutcome.RETRY, "429", timedelta(minutes=10))]
    )
    store, clock, outbox, _ = setup(channel)
    delivery_id = await enqueue(store)
    await outbox.run_once()
    row = store.data.deliveries[delivery_id]
    assert row.next_attempt_at == T0 + timedelta(minutes=10)  # longer than the backoff
    channel.error = RuntimeError("boom")
    clock.advance(600)
    await outbox.run_once()
    assert row.status is DeliveryStatus.PENDING and "boom" in (row.last_error or "")


async def test_notifications_outbox_gone_removes_the_subscription() -> None:
    channel = RecordingChannel("webpush", results=[DeliveryResult(DeliveryOutcome.GONE, "410")])
    store, _, outbox, observer = setup(channel)
    user, account = uuid4(), uuid4()
    subscription = await store.add_push_subscription(
        PushSubscription(uuid4(), user, account, "https://push.example.test/x", P256DH, AUTH, T0)
    )
    delivery_id = await enqueue(
        store,
        "webpush",
        user=user,
        account=account,
        target_kind=TargetKind.PUSH_SUBSCRIPTION,
        target_id=subscription.id,
    )
    await outbox.run_once()
    assert store.data.deliveries[delivery_id].status is DeliveryStatus.DROPPED
    assert await store.push_subscriptions(user, account) == []
    assert observer.seen == [("webpush", "dropped")]
    assert channel.sent[0].target == subscription.subscription_info()


async def test_notifications_outbox_missing_target_unknown_channel_and_failure() -> None:
    ntfy = RecordingChannel("ntfy", results=[DeliveryResult(DeliveryOutcome.FAILED, "400")])
    store, _, outbox, observer = setup(ntfy)
    user, account = uuid4(), uuid4()
    gone = await enqueue(store, "ntfy", target_kind=TargetKind.NTFY)  # no target stored
    unknown = await enqueue(store, "telegram")
    await store.put_ntfy_target(
        NtfyTarget(user, account, "https://ntfy.example.test", "t", None, T0)
    )
    failing = await enqueue(store, "ntfy", user=user, account=account, target_kind=TargetKind.NTFY)
    await outbox.run_once()
    rows = store.data.deliveries
    assert rows[gone].status is DeliveryStatus.DROPPED
    assert rows[unknown].status is DeliveryStatus.FAILED
    assert rows[unknown].last_error == "channel not configured"
    assert rows[failing].status is DeliveryStatus.FAILED and rows[failing].last_error == "400"
    assert sorted(observer.seen) == [
        ("ntfy", "dropped"),
        ("ntfy", "failed"),
        ("telegram", "failed"),
    ]


async def test_notifications_outbox_prunes_finished_deliveries() -> None:
    channel = RecordingChannel()
    store, clock, outbox, _ = setup(channel)
    sent = await enqueue(store)
    await outbox.run_once()
    pending = await enqueue(store)
    store.data.deliveries[pending].next_attempt_at = T0 + timedelta(days=30)
    clock.advance(8 * 24 * 3600)
    assert await outbox.prune() == 1
    assert sent not in store.data.deliveries and pending in store.data.deliveries


async def test_notifications_outbox_claims_respect_the_batch_size() -> None:
    channel = RecordingChannel()
    store, clock = MemoryStore(), ManualClock()
    outbox = Outbox(
        store, ChannelRegistry({"log": channel}), NotificationsConfig(batch_size=2), clock
    )
    for _ in range(5):
        await enqueue(store)
    assert await outbox.run_once() == 2
    assert await outbox.run_once() == 2
    assert await outbox.run_once() == 1
    assert await outbox.run_once() == 0


class HangingChannel:
    """Never answers while ``hang`` is set, like a request on a stuck HTTP/2 connection."""

    def __init__(self, name: str = "apns", max_concurrency: int | None = None) -> None:
        self.name = name
        self.hang = True
        self.calls = 0
        self.cancelled = 0
        if max_concurrency is not None:
            self.max_concurrency = max_concurrency

    async def send(self, delivery: Delivery) -> DeliveryResult:
        self.calls += 1
        if not self.hang:
            return DeliveryResult.sent()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        raise AssertionError("unreachable")  # pragma: no cover


async def until(condition: Callable[[], bool], seconds: float = 1.0) -> None:
    for _ in range(round(seconds / 0.005)):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met in time")


async def test_notifications_outbox_a_hung_send_times_out_without_blocking_others() -> None:
    apns, log = HangingChannel(), RecordingChannel()
    store, clock, observer = MemoryStore(), ManualClock(), Observer()
    config = replace(CONFIG, send_deadline=timedelta(seconds=0.3))
    outbox = Outbox(store, ChannelRegistry({"apns": apns, "log": log}), config, clock, observer)
    stuck = await enqueue(store, "apns")
    first = await enqueue(store)
    assert await outbox.dispatch() == 2
    rows = store.data.deliveries
    # The log delivery finishes while the APNs send still hangs ...
    await until(lambda: rows[first].status is DeliveryStatus.SENT, 0.25)
    assert outbox.in_flight() == 1
    # ... and work queued meanwhile goes out too: the loop is not blocked.
    later = await enqueue(store)
    assert await outbox.dispatch() == 1
    await until(lambda: rows[later].status is DeliveryStatus.SENT, 0.25)
    assert rows[stuck].status is DeliveryStatus.PENDING and rows[stuck].attempts == 1
    # The deadline cancels the hung send: a retryable failure with an error.
    await outbox.drain()
    row = rows[stuck]
    assert apns.cancelled == 1
    assert row.status is DeliveryStatus.PENDING and row.attempts == 1
    assert "send deadline" in (row.last_error or "")
    assert row.next_attempt_at == T0 + timedelta(seconds=30)
    assert ("apns", "retry") in observer.seen
    # The retry succeeds once the channel answers again.
    apns.hang = False
    clock.advance(30)
    assert await outbox.run_once() == 1
    assert (rows[stuck].status, rows[stuck].attempts) == (DeliveryStatus.SENT, 2)


async def test_notifications_outbox_respects_the_channel_concurrency_limit() -> None:
    apns, log = HangingChannel(max_concurrency=1), RecordingChannel()
    store, clock = MemoryStore(), ManualClock()
    outbox = Outbox(store, ChannelRegistry({"apns": apns, "log": log}), CONFIG, clock)
    assert outbox.limit("apns") == 1 and outbox.limit("log") == CONFIG.send_concurrency
    waiting = [await enqueue(store, "apns") for _ in range(3)]
    assert await outbox.dispatch() == 1
    assert await outbox.dispatch() == 0  # the only APNs slot is busy
    await enqueue(store)
    assert await outbox.dispatch() == 1  # other channels are not held up
    assert sorted(store.data.deliveries[d].attempts for d in waiting) == [0, 0, 1]
    await until(lambda: apns.calls == 1)
    await outbox.aclose()
    assert apns.cancelled == 1


async def test_notifications_outbox_reclaims_a_lost_claim_after_the_lease() -> None:
    """A claimed delivery whose worker died (crash, restart, hang) goes out after the lease."""
    apns = HangingChannel()
    store, clock = MemoryStore(), ManualClock()
    registry = ChannelRegistry({"apns": apns})
    dead = Outbox(store, registry, CONFIG, clock)
    delivery_id = await enqueue(store, "apns")
    assert await dead.dispatch() == 1
    await dead.aclose()  # the process is gone; the claim stays as it was
    row = store.data.deliveries[delivery_id]
    assert (row.status, row.attempts) == (DeliveryStatus.PENDING, 1)
    assert row.next_attempt_at == T0 + CONFIG.lease
    apns.hang = False
    restarted = Outbox(store, registry, CONFIG, clock)
    clock.advance(CONFIG.lease.total_seconds() - 1)
    assert await restarted.run_once() == 0
    clock.advance(1)
    assert await restarted.run_once() == 1
    assert (row.status, row.attempts) == (DeliveryStatus.SENT, 2)


async def test_notifications_outbox_survives_a_failing_store_write() -> None:
    log = RecordingChannel()
    store, clock, outbox, _ = setup(log)
    delivery_id = await enqueue(store)
    original = store.finish_delivery

    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database down")

    store.finish_delivery = broken  # type: ignore[method-assign]
    assert await outbox.run_once() == 1  # logged, not raised
    row = store.data.deliveries[delivery_id]
    assert (row.status, row.attempts) == (DeliveryStatus.PENDING, 1)
    store.finish_delivery = original  # type: ignore[method-assign]
    clock.advance(CONFIG.lease.total_seconds())
    assert await outbox.run_once() == 1
    assert row.status is DeliveryStatus.SENT and len(log.sent) == 2  # at least once


async def test_notifications_outbox_drops_expired_deliveries() -> None:
    log = RecordingChannel()
    store, clock, outbox, observer = setup(log)
    fresh = await enqueue(store)
    stale = await enqueue(store)
    rows = store.data.deliveries
    rows[stale].next_attempt_at = T0 + timedelta(hours=1)  # due exactly when its TTL ends
    clock.advance(3599)
    assert await outbox.run_once() == 1
    clock.advance(1)
    assert await outbox.run_once() == 1
    assert rows[fresh].status is DeliveryStatus.SENT
    assert rows[stale].status is DeliveryStatus.DROPPED
    assert (rows[stale].last_error or "").startswith("expired before delivery")
    assert len(log.sent) == 1 and ("log", "dropped") in observer.seen


async def test_notifications_outbox_reports_a_stall() -> None:
    apns = HangingChannel()
    store, clock = MemoryStore(), ManualClock()
    # A lease longer than the test, so the stuck delivery is not claimed a second time.
    config = replace(CONFIG, stall_after=timedelta(minutes=5), lease=timedelta(minutes=30))
    outbox = Outbox(store, ChannelRegistry({"apns": apns}), config, clock)
    assert await outbox.dispatch() == 0  # idle counts as progress
    clock.advance(301)
    assert outbox.stalled_for() is not None
    assert await outbox.dispatch() == 0 and outbox.stalled_for() is None
    await enqueue(store, "apns")
    assert await outbox.dispatch() == 1
    clock.advance(300)
    assert outbox.stalled_for() is None
    clock.advance(1)
    assert await outbox.dispatch() == 0  # nothing new, but the send is still stuck
    assert outbox.stalled_for() == timedelta(seconds=301)
    await outbox.aclose()
    assert outbox.stalled_for() is None


async def test_notifications_outbox_worker_loop_survives_failing_rounds() -> None:
    calls: list[int] = []

    class FlakyOutbox:
        async def dispatch(self) -> int:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("claim failed")
            return 0

        async def wait(self, seconds: float) -> None:
            await asyncio.sleep(seconds)

        def stalled_for(self) -> timedelta | None:
            return timedelta(minutes=6)

        def in_flight(self) -> int:
            return 1

    services: Any = SimpleNamespace(outbox=FlakyOutbox())
    task = asyncio.create_task(delivery_worker(services, 0.01))
    await until(lambda: len(calls) >= 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await outbox_watchdog(services)  # logs the stall, does not raise
