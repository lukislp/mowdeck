"""The delivery worker with fake channels: success, retry with backoff, give up, gone."""

from datetime import timedelta
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import NotificationKind, NotificationSeverity
from notifications_support import AUTH, P256DH, T0, ManualClock, RecordingChannel

from notifications.adapters.channels import ChannelRegistry
from notifications.adapters.memory import MemoryStore
from notifications.application.context import NotificationsConfig
from notifications.application.outbox import Outbox
from notifications.domain.models import (
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
