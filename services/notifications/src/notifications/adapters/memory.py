"""In-memory store (tests and local experiments) with the semantics of the PostgreSQL store.

Transactions run one at a time and are all or nothing (the snapshot is restored on error).
"""

import asyncio
import copy
from collections.abc import AsyncIterator, Collection, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from notifications.application.ports import Tx
from notifications.domain.geofence import MowerFence
from notifications.domain.live_activity import ActivityDevice, LiveActivity
from notifications.domain.models import (
    ApnsDevice,
    Delivery,
    DeliveryStatus,
    DirectoryEntry,
    InboxItem,
    MaintenanceWindow,
    Message,
    MowerWatch,
    NewDelivery,
    NotificationImage,
    NtfyTarget,
    Preferences,
    PushSubscription,
    TargetKind,
)


@dataclass
class DeliveryRow:
    """A row of the outbox."""

    id: UUID
    user_id: UUID
    account_id: UUID
    channel: str
    message: dict[str, Any]
    target_kind: TargetKind
    target_id: UUID | None
    status: DeliveryStatus
    attempts: int
    next_attempt_at: datetime
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None


@dataclass
class _Data:
    directory: dict[UUID, DirectoryEntry] = field(default_factory=dict)
    watches: dict[UUID, MowerWatch] = field(default_factory=dict)
    preferences: dict[tuple[UUID, UUID], Preferences] = field(default_factory=dict)
    tests: dict[tuple[UUID, UUID], datetime] = field(default_factory=dict)
    push: dict[UUID, PushSubscription] = field(default_factory=dict)
    ntfy: dict[tuple[UUID, UUID], NtfyTarget] = field(default_factory=dict)
    apns: dict[UUID, ApnsDevice] = field(default_factory=dict)
    inbox: dict[UUID, InboxItem] = field(default_factory=dict)
    deliveries: dict[UUID, DeliveryRow] = field(default_factory=dict)
    geofences: dict[UUID, MowerFence] = field(default_factory=dict)
    maintenance: dict[int, MaintenanceWindow] = field(default_factory=dict)
    silences: dict[tuple[UUID, UUID, str], datetime] = field(default_factory=dict)
    images: dict[UUID, NotificationImage] = field(default_factory=dict)
    activity_devices: dict[tuple[UUID, UUID], ActivityDevice] = field(default_factory=dict)
    live_activities: dict[tuple[UUID, UUID, UUID], LiveActivity] = field(default_factory=dict)


class _MemoryTx:
    def __init__(self, data: _Data) -> None:
        self._data = data

    async def watch(self, mower_id: UUID) -> MowerWatch | None:
        return self._data.watches.get(mower_id)

    async def put_watch(self, watch: MowerWatch) -> None:
        self._data.watches[watch.mower_id] = watch

    async def offline_candidates(
        self, no_state_before: datetime, disconnected_before: datetime, limit: int
    ) -> list[MowerWatch]:
        found = [
            w
            for w in self._data.watches.values()
            if not w.offline
            and (
                w.last_seen_at <= no_state_before
                or (w.disconnected_at is not None and w.disconnected_at <= disconnected_before)
            )
        ]
        found.sort(key=lambda w: w.last_seen_at)
        return found[:limit]

    async def held_offline(self, limit: int) -> list[MowerWatch]:
        found = [w for w in self._data.watches.values() if w.offline and w.offline_held]
        found.sort(key=lambda w: w.last_seen_at)
        return found[:limit]

    async def put_directory(self, entry: DirectoryEntry, *, replace: bool) -> None:
        if replace or entry.mower_id not in self._data.directory:
            self._data.directory[entry.mower_id] = entry

    async def drop_mower(self, mower_id: UUID) -> None:
        self._data.directory.pop(mower_id, None)
        self._data.watches.pop(mower_id, None)
        self._data.geofences.pop(mower_id, None)

    async def put_geofence(self, fence: MowerFence) -> bool:
        stored = self._data.geofences.get(fence.mower_id)
        if stored is not None and stored.observed_at > fence.observed_at:
            return False
        self._data.geofences[fence.mower_id] = fence
        return True


class MemoryStore:
    """The :class:`~notifications.application.ports.Store` port in memory."""

    def __init__(self) -> None:
        self.data = _Data()
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Tx]:
        async with self._lock:
            snapshot = copy.copy(self.data)
            for name in vars(snapshot):
                setattr(snapshot, name, dict(getattr(self.data, name)))
            try:
                yield _MemoryTx(self.data)
            except BaseException:
                self.data = snapshot
                raise

    async def directory_entry(self, mower_id: UUID) -> DirectoryEntry | None:
        return self.data.directory.get(mower_id)

    async def geofence(self, mower_id: UUID) -> MowerFence | None:
        return self.data.geofences.get(mower_id)

    async def maintenance_window(self) -> MaintenanceWindow | None:
        return self.data.maintenance.get(1)

    async def put_maintenance_window(self, window: MaintenanceWindow) -> None:
        self.data.maintenance[1] = window

    async def preferences(self, user_id: UUID, account_id: UUID) -> Preferences | None:
        return self.data.preferences.get((user_id, account_id))

    async def put_preferences(self, preferences: Preferences) -> None:
        stored = replace(preferences, stored=True)
        self.data.preferences[(preferences.user_id, preferences.account_id)] = stored

    async def subscribers(self, account_id: UUID, mower_id: UUID) -> list[Preferences]:
        found = [
            p
            for (_, account), p in self.data.preferences.items()
            if account == account_id and p.subscribed(mower_id)
        ]
        found.sort(key=lambda p: str(p.user_id))
        return found

    async def default_recipients(self, account_id: UUID) -> list[UUID]:
        users = {s.user_id for s in self.data.push.values() if s.account_id == account_id}
        users |= {d.user_id for d in self.data.apns.values() if d.account_id == account_id}
        users |= {user for (user, account) in self.data.ntfy if account == account_id}
        users -= {user for (user, account) in self.data.preferences if account == account_id}
        return sorted(users, key=str)

    async def try_test(
        self, user_id: UUID, account_id: UUID, now: datetime, every: timedelta
    ) -> bool:
        last = self.data.tests.get((user_id, account_id))
        if last is not None and now - last < every:
            return False
        self.data.tests[(user_id, account_id)] = now
        return True

    async def add_push_subscription(self, subscription: PushSubscription) -> PushSubscription:
        for existing in list(self.data.push.values()):
            if existing.endpoint == subscription.endpoint:
                updated = replace(
                    existing,
                    user_id=subscription.user_id,
                    account_id=subscription.account_id,
                    p256dh=subscription.p256dh,
                    auth=subscription.auth,
                    user_agent=subscription.user_agent,
                )
                self.data.push[existing.id] = updated
                return updated
        self.data.push[subscription.id] = subscription
        return subscription

    async def push_subscriptions(self, user_id: UUID, account_id: UUID) -> list[PushSubscription]:
        found = [
            s
            for s in self.data.push.values()
            if s.user_id == user_id and s.account_id == account_id
        ]
        found.sort(key=lambda s: (s.created_at, str(s.id)))
        return found

    async def delete_push_subscription(
        self, user_id: UUID, account_id: UUID, subscription_id: UUID
    ) -> bool:
        existing = self.data.push.get(subscription_id)
        if existing is None or existing.user_id != user_id or existing.account_id != account_id:
            return False
        del self.data.push[subscription_id]
        return True

    async def remove_push_subscription(self, subscription_id: UUID) -> None:
        self.data.push.pop(subscription_id, None)

    async def add_apns_device(self, device: ApnsDevice) -> ApnsDevice:
        if device.installation_id is not None:
            for current in list(self.data.apns.values()):
                if (current.user_id, current.installation_id) == (
                    device.user_id,
                    device.installation_id,
                ):
                    self.data.apns = {
                        k: d
                        for k, d in self.data.apns.items()
                        if d.token != device.token or k == current.id
                    }
                    updated = replace(
                        current,
                        token=device.token,
                        account_id=device.account_id,
                        device_name=device.device_name,
                    )
                    self.data.apns[current.id] = updated
                    return updated
        for existing in list(self.data.apns.values()):
            if existing.token == device.token:
                updated = replace(
                    existing,
                    user_id=device.user_id,
                    account_id=device.account_id,
                    device_name=device.device_name,
                    installation_id=device.installation_id or existing.installation_id,
                )
                self.data.apns[existing.id] = updated
                return updated
        self.data.apns[device.id] = device
        return device

    async def apns_devices(self, user_id: UUID, account_id: UUID) -> list[ApnsDevice]:
        found = [
            d
            for d in self.data.apns.values()
            if d.user_id == user_id and d.account_id == account_id
        ]
        found.sort(key=lambda d: (d.created_at, str(d.id)))
        return found

    async def delete_apns_device(self, user_id: UUID, account_id: UUID, device_id: UUID) -> bool:
        existing = self.data.apns.get(device_id)
        if existing is None or existing.user_id != user_id or existing.account_id != account_id:
            return False
        del self.data.apns[device_id]
        return True

    async def remove_apns_device(self, device_id: UUID) -> None:
        self.data.apns.pop(device_id, None)

    async def ntfy_target(self, user_id: UUID, account_id: UUID) -> NtfyTarget | None:
        return self.data.ntfy.get((user_id, account_id))

    async def put_ntfy_target(self, target: NtfyTarget) -> None:
        self.data.ntfy[(target.user_id, target.account_id)] = target

    async def delete_ntfy_target(self, user_id: UUID, account_id: UUID) -> bool:
        return self.data.ntfy.pop((user_id, account_id), None) is not None

    async def record(self, item: InboxItem, deliveries: Sequence[NewDelivery], keep: int) -> bool:
        async with self._lock:
            if any(
                i.user_id == item.user_id and i.dedupe_key == item.dedupe_key
                for i in self.data.inbox.values()
            ):
                return False
            self.data.inbox[item.id] = item
            for new in deliveries:
                row_id = uuid4()
                self.data.deliveries[row_id] = DeliveryRow(
                    id=row_id,
                    user_id=new.user_id,
                    account_id=new.account_id,
                    channel=new.channel,
                    message=new.message.to_json(),
                    target_kind=new.target_kind,
                    target_id=new.target_id,
                    status=DeliveryStatus.PENDING,
                    attempts=0,
                    next_attempt_at=item.created_at,
                    created_at=item.created_at,
                    updated_at=item.created_at,
                )
            mine = sorted(
                (
                    i
                    for i in self.data.inbox.values()
                    if i.user_id == item.user_id and i.account_id == item.account_id
                ),
                key=lambda i: (i.created_at, str(i.id)),
                reverse=True,
            )
            for old in mine[keep:]:
                del self.data.inbox[old.id]
            return True

    def _view(self, item: InboxItem) -> InboxItem:
        """The item as read: ``has_image`` says whether its photo exists."""
        image = self.data.images.get(item.image_id) if item.image_id else None
        return replace(item, has_image=image is not None and image.ready)

    def _mine(self, user_id: UUID, account_id: UUID) -> list[InboxItem]:
        found = [
            self._view(i)
            for i in self.data.inbox.values()
            if i.user_id == user_id and i.account_id == account_id
        ]
        found.sort(key=lambda i: (i.created_at, str(i.id)), reverse=True)
        return found

    # --- camera images (ADR 0056) --------------------------------------------------------

    async def put_image(self, image: NotificationImage, keep: int) -> None:
        self.data.images[image.id] = image
        for old in [i for i in self.data.images.values() if i.expires_at <= image.created_at]:
            del self.data.images[old.id]
        newest = sorted(
            self.data.images.values(), key=lambda i: (i.created_at, str(i.id)), reverse=True
        )
        for old in newest[keep:]:
            del self.data.images[old.id]

    def _live_image(self, item: InboxItem | None, now: datetime) -> NotificationImage | None:
        image = self.data.images.get(item.image_id) if item and item.image_id else None
        return image if image is not None and image.expires_at > now else None

    async def notification_image(
        self, notification_id: UUID, now: datetime
    ) -> NotificationImage | None:
        return self._live_image(self.data.inbox.get(notification_id), now)

    async def inbox_image(
        self, user_id: UUID, account_id: UUID, item_id: UUID, now: datetime
    ) -> NotificationImage | None:
        item = self.data.inbox.get(item_id)
        if item is None or item.user_id != user_id or item.account_id != account_id:
            return None
        return self._live_image(item, now)

    async def prune_images(self, now: datetime) -> int:
        expired = [i.id for i in self.data.images.values() if i.expires_at <= now]
        for image_id in expired:
            del self.data.images[image_id]
        return len(expired)

    async def inbox(
        self, user_id: UUID, account_id: UUID, limit: int, *, unread_only: bool
    ) -> list[InboxItem]:
        items = self._mine(user_id, account_id)
        if unread_only:
            items = [i for i in items if i.read_at is None]
        return items[:limit]

    async def unread_count(self, user_id: UUID, account_id: UUID) -> int:
        return sum(1 for i in self._mine(user_id, account_id) if i.read_at is None)

    async def mark_read(
        self, user_id: UUID, account_id: UUID, item_id: UUID, now: datetime
    ) -> InboxItem | None:
        item = self.data.inbox.get(item_id)
        if item is None or item.user_id != user_id or item.account_id != account_id:
            return None
        if item.read_at is None:
            item = replace(item, read_at=now)
            self.data.inbox[item_id] = item
        return self._view(item)

    async def acknowledge(
        self, user_id: UUID, account_id: UUID, item_id: UUID, now: datetime
    ) -> InboxItem | None:
        item = await self.mark_read(user_id, account_id, item_id, now)
        if item is not None and item.acked_at is None:
            item = replace(item, acked_at=now)
            self.data.inbox[item_id] = item
        return self._view(item) if item is not None else None

    async def silence(self, user_id: UUID, account_id: UUID, key: str, until: datetime) -> None:
        current = self.data.silences.get((user_id, account_id, key))
        if current is None or current < until:
            self.data.silences[(user_id, account_id, key)] = until

    async def silenced(
        self, account_id: UUID, key: str, user_ids: Sequence[UUID], now: datetime
    ) -> set[UUID]:
        return {
            u
            for u in user_ids
            if (until := self.data.silences.get((u, account_id, key))) is not None and until > now
        }

    def _target(self, row: DeliveryRow) -> dict[str, Any] | None:
        match row.target_kind:
            case TargetKind.PUSH_SUBSCRIPTION:
                sub = self.data.push.get(row.target_id) if row.target_id else None
                if sub is None or sub.user_id != row.user_id:
                    return None
                return sub.subscription_info()
            case TargetKind.APNS_DEVICE:
                device = self.data.apns.get(row.target_id) if row.target_id else None
                if device is None or device.user_id != row.user_id:
                    return None
                return device.target()
            case TargetKind.NTFY:
                target = self.data.ntfy.get((row.user_id, row.account_id))
                if target is None:
                    return None
                return {
                    "server_url": target.server_url,
                    "topic": target.topic,
                    "token": target.token,
                }
            case _:
                return None

    async def claim_deliveries(
        self,
        now: datetime,
        lease: timedelta,
        limit: int,
        *,
        channel: str | None = None,
        exclude: Collection[str] = (),
    ) -> list[Delivery]:
        async with self._lock:
            due = sorted(
                (
                    r
                    for r in self.data.deliveries.values()
                    if r.status is DeliveryStatus.PENDING
                    and r.next_attempt_at <= now
                    and (channel is None or r.channel == channel)
                    and r.channel not in exclude
                ),
                key=lambda r: (r.next_attempt_at, str(r.id)),
            )[:limit]
            claimed: list[Delivery] = []
            for row in due:
                row.attempts += 1
                row.next_attempt_at = now + lease
                row.updated_at = now
                claimed.append(
                    Delivery(
                        id=row.id,
                        user_id=row.user_id,
                        account_id=row.account_id,
                        channel=row.channel,
                        message=Message.from_json(row.message),
                        attempts=row.attempts,
                        target_kind=row.target_kind,
                        target_id=row.target_id,
                        target=self._target(row),
                        created_at=row.created_at,
                    )
                )
            return claimed

    async def finish_delivery(
        self,
        delivery_id: UUID,
        status: DeliveryStatus,
        *,
        now: datetime,
        error: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> None:
        row = self.data.deliveries.get(delivery_id)
        if row is None:
            return
        row.status = status
        row.last_error = error
        row.updated_at = now
        if next_attempt_at is not None:
            row.next_attempt_at = next_attempt_at

    async def prune_deliveries(self, before: datetime) -> int:
        old = [
            k
            for k, r in self.data.deliveries.items()
            if r.status is not DeliveryStatus.PENDING and r.updated_at < before
        ]
        for key in old:
            del self.data.deliveries[key]
        return len(old)

    async def put_activity_device(self, device: ActivityDevice) -> None:
        self.data.activity_devices[(device.user_id, device.installation_id)] = device

    async def activity_devices(self, account_id: UUID) -> list[ActivityDevice]:
        found = [d for d in self.data.activity_devices.values() if d.account_id == account_id]
        return sorted(found, key=lambda d: (str(d.user_id), str(d.installation_id)))

    async def delete_activity_device(self, user_id: UUID, installation_id: UUID) -> bool:
        removed = self.data.activity_devices.pop((user_id, installation_id), None)
        self.data.live_activities = {
            k: v
            for k, v in self.data.live_activities.items()
            if (k[0], k[1]) != (user_id, installation_id)
        }
        return removed is not None

    async def put_live_activity(self, activity: LiveActivity) -> None:
        key = (activity.user_id, activity.installation_id, activity.mower_id)
        self.data.live_activities[key] = activity

    async def live_activity(
        self, user_id: UUID, installation_id: UUID, mower_id: UUID
    ) -> LiveActivity | None:
        return self.data.live_activities.get((user_id, installation_id, mower_id))

    async def live_activities(self, mower_id: UUID) -> list[LiveActivity]:
        found = [a for a in self.data.live_activities.values() if a.mower_id == mower_id]
        return sorted(found, key=lambda a: (str(a.user_id), str(a.installation_id)))

    async def delete_live_activity(
        self, user_id: UUID, installation_id: UUID, mower_id: UUID
    ) -> bool:
        return self.data.live_activities.pop((user_id, installation_id, mower_id), None) is not None

    async def drop_user(self, user_id: UUID, account_id: UUID | None) -> None:
        def match(user: UUID, account: UUID) -> bool:
            return user == user_id and (account_id is None or account == account_id)

        data = self.data
        data.preferences = {k: v for k, v in data.preferences.items() if not match(*k)}
        data.tests = {k: v for k, v in data.tests.items() if not match(*k)}
        data.ntfy = {k: v for k, v in data.ntfy.items() if not match(*k)}
        data.push = {k: v for k, v in data.push.items() if not match(v.user_id, v.account_id)}
        data.apns = {k: v for k, v in data.apns.items() if not match(v.user_id, v.account_id)}
        data.inbox = {k: v for k, v in data.inbox.items() if not match(v.user_id, v.account_id)}
        data.deliveries = {
            k: v for k, v in data.deliveries.items() if not match(v.user_id, v.account_id)
        }
        data.silences = {k: v for k, v in data.silences.items() if not match(k[0], k[1])}
        data.activity_devices = {
            k: v for k, v in data.activity_devices.items() if not match(v.user_id, v.account_id)
        }
        data.live_activities = {
            k: v for k, v in data.live_activities.items() if not match(v.user_id, v.account_id)
        }
