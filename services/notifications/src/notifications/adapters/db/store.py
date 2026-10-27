"""PostgreSQL store on SQLAlchemy Core (async, asyncpg).

* The watch of a mower is read ``FOR UPDATE`` inside the rule transaction; the offline timer
  takes its batch with ``FOR UPDATE SKIP LOCKED``, so replicas sweep disjoint mowers.
* Inbox items are unique per ``(user_id, dedupe_key)`` (``ON CONFLICT DO NOTHING``); the item and
  its deliveries are written in one transaction, and the inbox is trimmed to the newest items.
* The outbox is claimed with ``FOR UPDATE SKIP LOCKED`` in a short transaction that bumps
  ``attempts`` and moves ``next_attempt_at`` by the lease; sending happens outside of it.
"""

from collections.abc import AsyncIterator, Collection, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from mowdeck_contracts.v1 import NotificationKind, NotificationSeverity
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from notifications.adapters.db import tables
from notifications.application.ports import Tx
from notifications.domain.geofence import Fence, MowerFence
from notifications.domain.models import (
    ApnsDevice,
    Delivery,
    DeliveryStatus,
    DirectoryEntry,
    InboxItem,
    Message,
    MowerWatch,
    NewDelivery,
    NotificationImage,
    NtfyTarget,
    Preferences,
    PushSubscription,
    QuietHours,
    TargetKind,
)


def _watch(row: Any) -> MowerWatch:
    return MowerWatch.from_json(row.mower_id, row.account_id, dict(row.state))


def _hhmm(value: time | None) -> str | None:
    return value.strftime("%H:%M") if value is not None else None


def _preferences(row: Any) -> Preferences:
    quiet = None
    if row.quiet_start and row.quiet_end:
        quiet = QuietHours(time.fromisoformat(row.quiet_start), time.fromisoformat(row.quiet_end))
    return Preferences(
        user_id=row.user_id,
        account_id=row.account_id,
        enabled_kinds=frozenset(NotificationKind(k) for k in row.enabled_kinds),
        all_mowers=row.all_mowers,
        mower_ids=frozenset(UUID(m) for m in row.mower_ids),
        quiet_hours=quiet,
        timezone=row.timezone,
        language=row.language,
        channels=frozenset(row.channels),
        stored=True,
    )


def _push(row: Any) -> PushSubscription:
    return PushSubscription(
        id=row.id,
        user_id=row.user_id,
        account_id=row.account_id,
        endpoint=row.endpoint,
        p256dh=row.p256dh,
        auth=row.auth,
        created_at=row.created_at,
        user_agent=row.user_agent,
    )


def _apns(row: Any) -> ApnsDevice:
    return ApnsDevice(
        id=row.id,
        user_id=row.user_id,
        account_id=row.account_id,
        token=row.token,
        device_name=row.device_name,
        created_at=row.created_at,
        installation_id=row.installation_id,
    )


def _ntfy(row: Any) -> NtfyTarget:
    return NtfyTarget(
        user_id=row.user_id,
        account_id=row.account_id,
        server_url=row.server_url,
        topic=row.topic,
        token=row.token,
        updated_at=row.updated_at,
    )


def _inbox(row: Any) -> InboxItem:
    return InboxItem(
        id=row.id,
        user_id=row.user_id,
        account_id=row.account_id,
        mower_id=row.mower_id,
        kind=NotificationKind(row.kind),
        severity=NotificationSeverity(row.severity),
        title=row.title,
        body=row.body,
        url=row.url,
        dedupe_key=row.dedupe_key,
        occurred_at=row.occurred_at,
        created_at=row.created_at,
        read_at=row.read_at,
        acked_at=row.acked_at,
        image_id=row.image_id,
        has_image=bool(getattr(row, "has_image", False)),
    )


def _inbox_select() -> sa.Select[Any]:
    """The inbox columns plus ``has_image``: whether the referenced photo exists."""
    photo = tables.notification_images.c
    exists = (
        sa.select(sa.literal(1))
        .where(photo.id == tables.inbox.c.image_id, photo.data.is_not(None))
        .exists()
    )
    return sa.select(tables.inbox, exists.label("has_image"))


def _image(row: Any) -> NotificationImage:
    return NotificationImage(
        id=row.id,
        account_id=row.account_id,
        mower_id=row.mower_id,
        created_at=row.created_at,
        expires_at=row.expires_at,
        media_type=row.media_type,
        data=bytes(row.data) if row.data is not None else None,
    )


def _fence(row: Any) -> MowerFence:
    return MowerFence(
        mower_id=row.mower_id,
        account_id=row.account_id,
        observed_at=row.observed_at,
        fence=Fence.from_json(dict(row.fence)),
    )


class _SqlTx:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def watch(self, mower_id: UUID) -> MowerWatch | None:
        result = await self._conn.execute(
            sa.select(tables.mower_watch)
            .where(tables.mower_watch.c.mower_id == mower_id)
            .with_for_update()
        )
        row = result.first()
        return _watch(row) if row else None

    async def put_watch(self, watch: MowerWatch) -> None:
        values = {
            "mower_id": watch.mower_id,
            "account_id": watch.account_id,
            "last_seen_at": watch.last_seen_at,
            "disconnected_at": watch.disconnected_at,
            "offline": watch.offline,
            "state": watch.to_json(),
        }
        insert = pg_insert(tables.mower_watch).values(**values)
        await self._conn.execute(
            insert.on_conflict_do_update(
                index_elements=[tables.mower_watch.c.mower_id],
                set_={k: insert.excluded[k] for k in values if k != "mower_id"},
            )
        )

    async def offline_candidates(
        self, no_state_before: datetime, disconnected_before: datetime, limit: int
    ) -> list[MowerWatch]:
        w = tables.mower_watch.c
        result = await self._conn.execute(
            sa.select(tables.mower_watch)
            .where(
                sa.not_(w.offline),
                sa.or_(
                    w.last_seen_at <= no_state_before,
                    sa.and_(
                        w.disconnected_at.is_not(None), w.disconnected_at <= disconnected_before
                    ),
                ),
            )
            .order_by(w.last_seen_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return [_watch(row) for row in result.all()]

    async def put_directory(self, entry: DirectoryEntry, *, replace: bool) -> None:
        insert = pg_insert(tables.mower_directory).values(
            mower_id=entry.mower_id,
            account_id=entry.account_id,
            name=entry.name,
            updated_at=entry.updated_at,
        )
        if replace:
            statement = insert.on_conflict_do_update(
                index_elements=["mower_id"],
                set_={
                    "account_id": insert.excluded.account_id,
                    "name": insert.excluded.name,
                    "updated_at": insert.excluded.updated_at,
                },
            )
        else:
            statement = insert.on_conflict_do_nothing(index_elements=["mower_id"])
        await self._conn.execute(statement)

    async def drop_mower(self, mower_id: UUID) -> None:
        for table in (tables.mower_directory, tables.mower_watch, tables.mower_geofence):
            await self._conn.execute(sa.delete(table).where(table.c.mower_id == mower_id))

    async def put_geofence(self, fence: MowerFence) -> bool:
        g = tables.mower_geofence
        insert = pg_insert(g).values(
            mower_id=fence.mower_id,
            account_id=fence.account_id,
            observed_at=fence.observed_at,
            fence=fence.fence.to_json(),
        )
        statement = insert.on_conflict_do_update(
            index_elements=[g.c.mower_id],
            set_={k: insert.excluded[k] for k in ("account_id", "observed_at", "fence")},
            where=g.c.observed_at <= insert.excluded.observed_at,
        ).returning(g.c.mower_id)
        result = await self._conn.execute(statement)
        return result.first() is not None


class SqlStore:
    """Implements :class:`~notifications.application.ports.Store` on PostgreSQL."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Tx]:
        """``BEGIN`` ... ``COMMIT`` (``ROLLBACK`` on error)."""
        async with self.engine.begin() as conn:
            yield _SqlTx(conn)

    async def directory_entry(self, mower_id: UUID) -> DirectoryEntry | None:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                sa.select(tables.mower_directory).where(
                    tables.mower_directory.c.mower_id == mower_id
                )
            )
            row = result.first()
        return DirectoryEntry(**dict(row._mapping)) if row else None

    async def geofence(self, mower_id: UUID) -> MowerFence | None:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                sa.select(tables.mower_geofence).where(tables.mower_geofence.c.mower_id == mower_id)
            )
            row = result.first()
        return _fence(row) if row else None

    # --- preferences ---------------------------------------------------------------------

    async def preferences(self, user_id: UUID, account_id: UUID) -> Preferences | None:
        p = tables.preferences.c
        async with self.engine.connect() as conn:
            result = await conn.execute(
                sa.select(tables.preferences).where(
                    p.user_id == user_id, p.account_id == account_id
                )
            )
            row = result.first()
        return _preferences(row) if row else None

    async def put_preferences(self, preferences: Preferences) -> None:
        quiet = preferences.quiet_hours
        values = {
            "user_id": preferences.user_id,
            "account_id": preferences.account_id,
            "enabled_kinds": sorted(k.value for k in preferences.enabled_kinds),
            "all_mowers": preferences.all_mowers,
            "mower_ids": sorted(str(m) for m in preferences.mower_ids),
            "quiet_start": _hhmm(quiet.start) if quiet else None,
            "quiet_end": _hhmm(quiet.end) if quiet else None,
            "timezone": preferences.timezone,
            "language": preferences.language,
            "channels": sorted(preferences.channels),
            "updated_at": sa.func.now(),
        }
        insert = pg_insert(tables.preferences).values(**values)
        async with self.engine.begin() as conn:
            await conn.execute(
                insert.on_conflict_do_update(
                    index_elements=["user_id", "account_id"],
                    set_={
                        k: insert.excluded[k] for k in values if k not in ("user_id", "account_id")
                    },
                )
            )

    async def subscribers(self, account_id: UUID, mower_id: UUID) -> list[Preferences]:
        p = tables.preferences.c
        async with self.engine.connect() as conn:
            result = await conn.execute(
                sa.select(tables.preferences)
                .where(
                    p.account_id == account_id,
                    sa.or_(p.all_mowers, p.mower_ids.contains([str(mower_id)])),
                )
                .order_by(p.user_id)
            )
            return [_preferences(row) for row in result.all()]

    async def default_recipients(self, account_id: UUID) -> list[UUID]:
        targets = sa.union(
            sa.select(tables.push_subscriptions.c.user_id).where(
                tables.push_subscriptions.c.account_id == account_id
            ),
            sa.select(tables.apns_devices.c.user_id).where(
                tables.apns_devices.c.account_id == account_id
            ),
            sa.select(tables.ntfy_targets.c.user_id).where(
                tables.ntfy_targets.c.account_id == account_id
            ),
        ).subquery()
        p = tables.preferences.c
        saved = sa.exists().where(p.account_id == account_id, p.user_id == targets.c.user_id)
        async with self.engine.connect() as conn:
            result = await conn.execute(
                sa.select(targets.c.user_id).where(~saved).order_by(targets.c.user_id)
            )
            return [row.user_id for row in result.all()]

    async def try_test(
        self, user_id: UUID, account_id: UUID, now: datetime, every: timedelta
    ) -> bool:
        insert = pg_insert(tables.test_requests).values(
            user_id=user_id, account_id=account_id, last_at=now
        )
        statement = insert.on_conflict_do_update(
            index_elements=["user_id", "account_id"],
            set_={"last_at": insert.excluded.last_at},
            where=tables.test_requests.c.last_at <= now - every,
        ).returning(tables.test_requests.c.user_id)
        async with self.engine.begin() as conn:
            result = await conn.execute(statement)
            return result.first() is not None

    # --- targets -------------------------------------------------------------------------

    async def add_push_subscription(self, subscription: PushSubscription) -> PushSubscription:
        insert = pg_insert(tables.push_subscriptions).values(
            id=subscription.id,
            user_id=subscription.user_id,
            account_id=subscription.account_id,
            endpoint=subscription.endpoint,
            p256dh=subscription.p256dh,
            auth=subscription.auth,
            user_agent=subscription.user_agent,
            created_at=subscription.created_at,
        )
        statement = insert.on_conflict_do_update(
            index_elements=["endpoint"],
            set_={k: insert.excluded[k] for k in ("user_id", "account_id", "p256dh", "auth")}
            | {"user_agent": insert.excluded.user_agent},
        ).returning(tables.push_subscriptions)
        async with self.engine.begin() as conn:
            result = await conn.execute(statement)
            return _push(result.one())

    async def push_subscriptions(self, user_id: UUID, account_id: UUID) -> list[PushSubscription]:
        s = tables.push_subscriptions.c
        async with self.engine.connect() as conn:
            result = await conn.execute(
                sa.select(tables.push_subscriptions)
                .where(s.user_id == user_id, s.account_id == account_id)
                .order_by(s.created_at, s.id)
            )
            return [_push(row) for row in result.all()]

    async def delete_push_subscription(
        self, user_id: UUID, account_id: UUID, subscription_id: UUID
    ) -> bool:
        s = tables.push_subscriptions.c
        async with self.engine.begin() as conn:
            result = await conn.execute(
                sa.delete(tables.push_subscriptions).where(
                    s.id == subscription_id, s.user_id == user_id, s.account_id == account_id
                )
            )
            return bool(result.rowcount)

    async def remove_push_subscription(self, subscription_id: UUID) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.delete(tables.push_subscriptions).where(
                    tables.push_subscriptions.c.id == subscription_id
                )
            )

    async def add_apns_device(self, device: ApnsDevice) -> ApnsDevice:
        # Two registrations of a new installation at once: the second insert hits
        # uq_apns_devices_user_installation and, run again, finds the first one's row.
        try:
            async with self.engine.begin() as conn:
                return await self._put_apns_device(conn, device)
        except sa.exc.IntegrityError:
            async with self.engine.begin() as conn:
                return await self._put_apns_device(conn, device)

    async def _put_apns_device(self, conn: AsyncConnection, device: ApnsDevice) -> ApnsDevice:
        a = tables.apns_devices.c
        if device.installation_id is not None:
            current = (
                await conn.execute(
                    sa.select(a.id)
                    .where(a.user_id == device.user_id, a.installation_id == device.installation_id)
                    .with_for_update()
                )
            ).one_or_none()
            if current is not None:
                # The installation's new token replaces the old one in its row (id kept); a
                # row that already held the new token (an older registration) goes.
                await conn.execute(
                    sa.delete(tables.apns_devices).where(
                        a.token == device.token, a.id != current.id
                    )
                )
                updated = await conn.execute(
                    sa.update(tables.apns_devices)
                    .where(a.id == current.id)
                    .values(
                        token=device.token,
                        account_id=device.account_id,
                        device_name=device.device_name,
                    )
                    .returning(tables.apns_devices)
                )
                return _apns(updated.one())
        insert = pg_insert(tables.apns_devices).values(
            id=device.id,
            user_id=device.user_id,
            account_id=device.account_id,
            token=device.token,
            device_name=device.device_name,
            created_at=device.created_at,
            installation_id=device.installation_id,
        )
        moved = ["user_id", "account_id", "device_name"]
        if device.installation_id is not None:
            moved.append("installation_id")  # without one, a known installation is kept
        statement = insert.on_conflict_do_update(
            index_elements=["token"], set_={k: insert.excluded[k] for k in moved}
        ).returning(tables.apns_devices)
        result = await conn.execute(statement)
        return _apns(result.one())

    async def apns_devices(self, user_id: UUID, account_id: UUID) -> list[ApnsDevice]:
        a = tables.apns_devices.c
        async with self.engine.connect() as conn:
            result = await conn.execute(
                sa.select(tables.apns_devices)
                .where(a.user_id == user_id, a.account_id == account_id)
                .order_by(a.created_at, a.id)
            )
            return [_apns(row) for row in result.all()]

    async def delete_apns_device(self, user_id: UUID, account_id: UUID, device_id: UUID) -> bool:
        a = tables.apns_devices.c
        async with self.engine.begin() as conn:
            result = await conn.execute(
                sa.delete(tables.apns_devices).where(
                    a.id == device_id, a.user_id == user_id, a.account_id == account_id
                )
            )
            return bool(result.rowcount)

    async def remove_apns_device(self, device_id: UUID) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.delete(tables.apns_devices).where(tables.apns_devices.c.id == device_id)
            )

    async def ntfy_target(self, user_id: UUID, account_id: UUID) -> NtfyTarget | None:
        n = tables.ntfy_targets.c
        async with self.engine.connect() as conn:
            result = await conn.execute(
                sa.select(tables.ntfy_targets).where(
                    n.user_id == user_id, n.account_id == account_id
                )
            )
            row = result.first()
        return _ntfy(row) if row else None

    async def put_ntfy_target(self, target: NtfyTarget) -> None:
        values = {
            "user_id": target.user_id,
            "account_id": target.account_id,
            "server_url": target.server_url,
            "topic": target.topic,
            "token": target.token,
            "updated_at": target.updated_at,
        }
        insert = pg_insert(tables.ntfy_targets).values(**values)
        async with self.engine.begin() as conn:
            await conn.execute(
                insert.on_conflict_do_update(
                    index_elements=["user_id", "account_id"],
                    set_={
                        k: insert.excluded[k] for k in values if k not in ("user_id", "account_id")
                    },
                )
            )

    async def delete_ntfy_target(self, user_id: UUID, account_id: UUID) -> bool:
        n = tables.ntfy_targets.c
        async with self.engine.begin() as conn:
            result = await conn.execute(
                sa.delete(tables.ntfy_targets).where(
                    n.user_id == user_id, n.account_id == account_id
                )
            )
            return bool(result.rowcount)

    # --- inbox and outbox ----------------------------------------------------------------

    async def record(self, item: InboxItem, deliveries: Sequence[NewDelivery], keep: int) -> bool:
        i = tables.inbox.c
        async with self.engine.begin() as conn:
            inserted = await conn.execute(
                pg_insert(tables.inbox)
                .values(
                    id=item.id,
                    user_id=item.user_id,
                    account_id=item.account_id,
                    mower_id=item.mower_id,
                    kind=item.kind.value,
                    severity=item.severity.value,
                    title=item.title,
                    body=item.body,
                    url=item.url,
                    dedupe_key=item.dedupe_key,
                    occurred_at=item.occurred_at,
                    created_at=item.created_at,
                    read_at=item.read_at,
                    image_id=item.image_id,
                )
                .on_conflict_do_nothing(index_elements=["user_id", "dedupe_key"])
                .returning(i.id)
            )
            if inserted.first() is None:
                return False
            if deliveries:
                await conn.execute(
                    sa.insert(tables.deliveries),
                    [
                        {
                            "id": uuid4(),
                            "user_id": d.user_id,
                            "account_id": d.account_id,
                            "channel": d.channel,
                            "payload": d.message.to_json(),
                            "target_kind": d.target_kind.value,
                            "target_id": d.target_id,
                            "status": DeliveryStatus.PENDING.value,
                            "attempts": 0,
                            "next_attempt_at": item.created_at,
                            "last_error": None,
                            "created_at": item.created_at,
                            "updated_at": item.created_at,
                        }
                        for d in deliveries
                    ],
                )
            newest = (
                sa.select(i.id)
                .where(i.user_id == item.user_id, i.account_id == item.account_id)
                .order_by(i.created_at.desc(), i.id.desc())
                .limit(keep)
            )
            await conn.execute(
                sa.delete(tables.inbox).where(
                    i.user_id == item.user_id,
                    i.account_id == item.account_id,
                    i.id.not_in(newest.scalar_subquery()),
                )
            )
        return True

    async def inbox(
        self, user_id: UUID, account_id: UUID, limit: int, *, unread_only: bool
    ) -> list[InboxItem]:
        i = tables.inbox.c
        statement = (
            _inbox_select()
            .where(i.user_id == user_id, i.account_id == account_id)
            .order_by(i.created_at.desc(), i.id.desc())
            .limit(limit)
        )
        if unread_only:
            statement = statement.where(i.read_at.is_(None))
        async with self.engine.connect() as conn:
            result = await conn.execute(statement)
            return [_inbox(row) for row in result.all()]

    async def unread_count(self, user_id: UUID, account_id: UUID) -> int:
        i = tables.inbox.c
        async with self.engine.connect() as conn:
            result = await conn.execute(
                sa.select(sa.func.count())
                .select_from(tables.inbox)
                .where(i.user_id == user_id, i.account_id == account_id, i.read_at.is_(None))
            )
            return int(result.scalar_one())

    async def mark_read(
        self, user_id: UUID, account_id: UUID, item_id: UUID, now: datetime
    ) -> InboxItem | None:
        i = tables.inbox.c
        owned = (i.id == item_id, i.user_id == user_id, i.account_id == account_id)
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.update(tables.inbox).where(*owned, i.read_at.is_(None)).values(read_at=now)
            )
            result = await conn.execute(_inbox_select().where(*owned))
            row = result.first()
        return _inbox(row) if row else None

    async def acknowledge(
        self, user_id: UUID, account_id: UUID, item_id: UUID, now: datetime
    ) -> InboxItem | None:
        i = tables.inbox.c
        owned = (i.id == item_id, i.user_id == user_id, i.account_id == account_id)
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.update(tables.inbox)
                .where(*owned)
                .values(
                    read_at=sa.func.coalesce(i.read_at, now),
                    acked_at=sa.func.coalesce(i.acked_at, now),
                )
            )
            result = await conn.execute(_inbox_select().where(*owned))
            row = result.first()
        return _inbox(row) if row else None

    # --- camera images (ADR 0053) --------------------------------------------------------

    async def put_image(self, image: NotificationImage, keep: int) -> None:
        p = tables.notification_images
        values = {
            "account_id": image.account_id,
            "mower_id": image.mower_id,
            "media_type": image.media_type,
            "data": image.data,
            "created_at": image.created_at,
            "expires_at": image.expires_at,
        }
        async with self.engine.begin() as conn:
            await conn.execute(
                pg_insert(p)
                .values(id=image.id, **values)
                .on_conflict_do_update(index_elements=["id"], set_=values)
            )
            await conn.execute(sa.delete(p).where(p.c.expires_at <= image.created_at))
            newest = sa.select(p.c.id).order_by(p.c.created_at.desc(), p.c.id.desc()).limit(keep)
            await conn.execute(sa.delete(p).where(p.c.id.not_in(newest.scalar_subquery())))

    async def notification_image(
        self, notification_id: UUID, now: datetime
    ) -> NotificationImage | None:
        p, i = tables.notification_images, tables.inbox
        statement = (
            sa.select(p)
            .select_from(p.join(i, i.c.image_id == p.c.id))
            .where(i.c.id == notification_id, p.c.expires_at > now)
        )
        async with self.engine.connect() as conn:
            row = (await conn.execute(statement)).first()
        return _image(row) if row else None

    async def inbox_image(
        self, user_id: UUID, account_id: UUID, item_id: UUID, now: datetime
    ) -> NotificationImage | None:
        p, i = tables.notification_images, tables.inbox
        statement = (
            sa.select(p)
            .select_from(p.join(i, i.c.image_id == p.c.id))
            .where(
                i.c.id == item_id,
                i.c.user_id == user_id,
                i.c.account_id == account_id,
                p.c.expires_at > now,
            )
        )
        async with self.engine.connect() as conn:
            row = (await conn.execute(statement)).first()
        return _image(row) if row else None

    async def prune_images(self, now: datetime) -> int:
        p = tables.notification_images
        async with self.engine.begin() as conn:
            result = await conn.execute(sa.delete(p).where(p.c.expires_at <= now))
            return int(result.rowcount)

    async def silence(self, user_id: UUID, account_id: UUID, key: str, until: datetime) -> None:
        s = tables.alarm_silences
        insert = pg_insert(s).values(
            user_id=user_id, account_id=account_id, silence_key=key, until=until
        )
        async with self.engine.begin() as conn:
            await conn.execute(
                insert.on_conflict_do_update(
                    index_elements=["user_id", "account_id", "silence_key"],
                    set_={"until": sa.func.greatest(s.c.until, insert.excluded.until)},
                )
            )

    async def silenced(
        self, account_id: UUID, key: str, user_ids: Sequence[UUID], now: datetime
    ) -> set[UUID]:
        if not user_ids:
            return set()
        s = tables.alarm_silences.c
        async with self.engine.connect() as conn:
            result = await conn.execute(
                sa.select(s.user_id).where(
                    s.user_id.in_(list(user_ids)),
                    s.account_id == account_id,
                    s.silence_key == key,
                    s.until > now,
                )
            )
            return {row.user_id for row in result.all()}

    async def claim_deliveries(
        self,
        now: datetime,
        lease: timedelta,
        limit: int,
        *,
        channel: str | None = None,
        exclude: Collection[str] = (),
    ) -> list[Delivery]:
        d = tables.deliveries.c
        conditions = [d.status == DeliveryStatus.PENDING.value, d.next_attempt_at <= now]
        if channel is not None:
            conditions.append(d.channel == channel)
        if exclude:
            conditions.append(d.channel.not_in(list(exclude)))
        async with self.engine.begin() as conn:
            due = (
                sa.select(d.id)
                .where(*conditions)
                .order_by(d.next_attempt_at, d.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            ids = [row.id for row in (await conn.execute(due)).all()]
            if not ids:
                return []
            result = await conn.execute(
                sa.update(tables.deliveries)
                .where(d.id.in_(ids))
                .values(attempts=d.attempts + 1, next_attempt_at=now + lease, updated_at=now)
                .returning(tables.deliveries)
            )
            rows = sorted(result.all(), key=lambda r: (r.next_attempt_at, str(r.id)))
            push_ids = [
                r.target_id
                for r in rows
                if r.target_kind == TargetKind.PUSH_SUBSCRIPTION.value and r.target_id
            ]
            push: dict[UUID, Any] = {}
            if push_ids:
                found = await conn.execute(
                    sa.select(tables.push_subscriptions).where(
                        tables.push_subscriptions.c.id.in_(push_ids)
                    )
                )
                push = {row.id: row for row in found.all()}
            apns_ids = [
                r.target_id
                for r in rows
                if r.target_kind == TargetKind.APNS_DEVICE.value and r.target_id
            ]
            apns: dict[UUID, Any] = {}
            if apns_ids:
                found = await conn.execute(
                    sa.select(tables.apns_devices).where(tables.apns_devices.c.id.in_(apns_ids))
                )
                apns = {row.id: row for row in found.all()}
            ntfy_users = {
                (r.user_id, r.account_id) for r in rows if r.target_kind == TargetKind.NTFY.value
            }
            ntfy: dict[tuple[UUID, UUID], Any] = {}
            if ntfy_users:
                n = tables.ntfy_targets.c
                found = await conn.execute(
                    sa.select(tables.ntfy_targets).where(
                        sa.tuple_(n.user_id, n.account_id).in_(list(ntfy_users))
                    )
                )
                ntfy = {(row.user_id, row.account_id): row for row in found.all()}
        claimed: list[Delivery] = []
        for row in rows:
            kind = TargetKind(row.target_kind)
            target: dict[str, Any] | None = None
            if kind is TargetKind.PUSH_SUBSCRIPTION:
                sub = push.get(row.target_id)
                if sub is not None and sub.user_id == row.user_id:
                    target = _push(sub).subscription_info()
            elif kind is TargetKind.APNS_DEVICE:
                device = apns.get(row.target_id)
                if device is not None and device.user_id == row.user_id:
                    target = _apns(device).target()
            elif kind is TargetKind.NTFY:
                found_target = ntfy.get((row.user_id, row.account_id))
                if found_target is not None:
                    target = {
                        "server_url": found_target.server_url,
                        "topic": found_target.topic,
                        "token": found_target.token,
                    }
            claimed.append(
                Delivery(
                    id=row.id,
                    user_id=row.user_id,
                    account_id=row.account_id,
                    channel=row.channel,
                    message=Message.from_json(dict(row.payload)),
                    attempts=row.attempts,
                    target_kind=kind,
                    target_id=row.target_id,
                    target=target,
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
        values: dict[str, Any] = {"status": status.value, "last_error": error, "updated_at": now}
        if next_attempt_at is not None:
            values["next_attempt_at"] = next_attempt_at
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.update(tables.deliveries)
                .where(tables.deliveries.c.id == delivery_id)
                .values(**values)
            )

    async def prune_deliveries(self, before: datetime) -> int:
        d = tables.deliveries.c
        async with self.engine.begin() as conn:
            result = await conn.execute(
                sa.delete(tables.deliveries).where(
                    d.status != DeliveryStatus.PENDING.value, d.updated_at < before
                )
            )
            return int(result.rowcount)

    async def drop_user(self, user_id: UUID, account_id: UUID | None) -> None:
        async with self.engine.begin() as conn:
            for table in (
                tables.preferences,
                tables.test_requests,
                tables.push_subscriptions,
                tables.apns_devices,
                tables.ntfy_targets,
                tables.inbox,
                tables.deliveries,
                tables.alarm_silences,
            ):
                condition = table.c.user_id == user_id
                if account_id is not None:
                    condition = sa.and_(condition, table.c.account_id == account_id)
                await conn.execute(sa.delete(table).where(condition))
