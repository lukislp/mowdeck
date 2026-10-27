"""Fan-out: from a derived event to inbox items and queued deliveries per recipient.

Recipients are the users of the mower's account whose preferences follow the mower (``all
mowers`` or the mower selected) and enable the event's kind. A user who never saved preferences
but registered a delivery target in the account (push subscription, APNs device, ntfy target)
counts with the defaults: every mower, the default kinds, the default channels, no quiet hours
(:func:`~notifications.domain.preferences.default_preferences`). Before anything is stored for
them, ``can_view`` on the mower is checked for all of them in one batch, so a user whose access
was revoked after subscribing never receives data about the mower. Quiet hours (in the user's
time zone) keep the inbox item but queue no delivery; the theft alarms (``mower_lifted``,
``mower_left_dock_unexpectedly``, ``geofence_exit``) bypass quiet hours. A user who acknowledged
an alarm gets no repeat of it (same kind and mower) while the silence lasts.

Before the per-user fan-out every event is published once for its account as
``notifications.raised`` (rendered in English, independent of preferences, quiet hours and
recipients), so API tokens such as Home Assistant's, which have no inbox, can receive the
account's notifications over Realtime.

Camera photo (ADR 0056): an event of an :data:`~notifications.domain.models.IMAGE_KINDS` kind
reserves an image id before the fan-out; every recipient's inbox item and push message carry it.
The photo is requested after the items are stored, in the background: the push never waits for
the camera. The photo lands under the reserved id (or a failure marker); until then the push
image URL answers 404 and the iOS extension polls. A budget of its own limits the photos per
mower (minimum interval, photos per 24 hours) on top of the alarm cooldowns.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from mowdeck_contracts.v1 import (
    NotificationKind,
    NotificationRaised,
    NotificationSent,
    NotificationSeverity,
)
from mowdeck_service_kit import get_logger

from notifications.application.context import NotificationsConfig
from notifications.application.ports import (
    CapturedPhoto,
    Channels,
    Clock,
    EventPublisher,
    PhotoSource,
    Store,
    ViewChecker,
)
from notifications.domain.messages import MessageCatalog
from notifications.domain.models import (
    ALARM_KINDS,
    IMAGE_KINDS,
    SEVERITY,
    InboxItem,
    Message,
    NewDelivery,
    NotificationEvent,
    NotificationImage,
    Preferences,
    TargetKind,
)
from notifications.domain.preferences import default_preferences, in_quiet_hours, wants

_log = get_logger("notifications.fanout")

WEBPUSH = "webpush"
NTFY = "ntfy"
APNS = "apns"
ACCOUNT_LANGUAGE = "en"
"""Language of ``notifications.raised`` (an account has no language of its own)."""


def raised_id(account_id: UUID, dedupe_key: str) -> UUID:
    """The stable id of an account-scoped notification: the same occurrence (account and
    ``dedupe_key``) always gets the same id."""
    return uuid5(NAMESPACE_URL, f"mowdeck:notifications.raised:{account_id}:{dedupe_key}")


@dataclass(frozen=True, slots=True)
class Notified:
    """What one recipient got."""

    item: InboxItem
    channels: list[str]
    quiet: bool


class Fanout:
    """Turns events into inbox items and deliveries."""

    def __init__(
        self,
        store: Store,
        channels: Channels,
        views: ViewChecker,
        publisher: EventPublisher | None,
        catalog: MessageCatalog,
        config: NotificationsConfig,
        clock: Clock,
        photos: PhotoSource | None = None,
    ) -> None:
        self._store = store
        self._channels = channels
        self._views = views
        self._publisher = publisher
        self._catalog = catalog
        self._config = config
        self._clock = clock
        self._photos = photos
        self._captures: set[asyncio.Task[None]] = set()
        self._photo_log: dict[UUID, list[datetime]] = {}
        """Per mower: when photos were requested in the last 24 hours (the notifications
        budget; per replica, reset by a restart)."""

    async def drain(self) -> None:
        """Wait for the photos being taken (tests, shutdown)."""
        while self._captures:
            await asyncio.gather(*list(self._captures), return_exceptions=True)

    async def aclose(self) -> None:
        """Cancel the photos being taken (shutdown); the pushes already left without them."""
        for task in list(self._captures):
            task.cancel()
        await self.drain()

    async def prune_images(self) -> int:
        """Delete the photos past their retention."""
        return await self._store.prune_images(self._clock())

    def _reserve_photo(self, mower_id: UUID) -> bool:
        """True (and counted) when the mower may have another photo now: not within
        ``image_min_interval`` of the last one and under ``image_daily_limit`` per 24 hours."""
        now = self._clock()
        window = [t for t in self._photo_log.get(mower_id, []) if now - t < timedelta(hours=24)]
        allowed = len(window) < self._config.image_daily_limit and (
            not window or now - window[-1] >= self._config.image_min_interval
        )
        if allowed:
            window.append(now)
        if window:
            self._photo_log[mower_id] = window
        else:
            self._photo_log.pop(mower_id, None)
        return allowed

    def _take_photo(self, image_id: UUID, mower_id: UUID, account_id: UUID) -> None:
        """Start taking the photo in the background; the push does not wait for it."""
        task = asyncio.create_task(
            self._capture(image_id, mower_id, account_id), name=f"notification-photo-{image_id}"
        )
        self._captures.add(task)
        task.add_done_callback(self._captures.discard)

    async def _capture(self, image_id: UUID, mower_id: UUID, account_id: UUID) -> None:
        """Take the photo and store it (or a failure marker, so the push image URL answers 410
        at once instead of letting the iOS extension wait out its budget)."""
        assert self._photos is not None  # noqa: S101 - only started with a photo source
        photo: CapturedPhoto | None = None
        try:
            photo = await self._photos.capture(mower_id, account_id)
        except Exception as exc:
            _log.warning("photo for a notification failed", mower_id=str(mower_id), error=repr(exc))
        if photo is not None and len(photo.data) > self._config.image_max_bytes:
            _log.warning("photo for a notification too large", mower_id=str(mower_id))
            photo = None
        now = self._clock()
        retention = self._config.image_retention if photo else timedelta(hours=1)
        image = NotificationImage(
            id=image_id,
            account_id=account_id,
            mower_id=mower_id,
            created_at=now,
            expires_at=now + retention,
            media_type=photo.media_type if photo else None,
            data=photo.data if photo else None,
        )
        try:
            await self._store.put_image(image, self._config.image_keep)
        except Exception as exc:
            _log.warning("storing a notification photo failed", error=repr(exc))
            return
        _log.info(
            "notification photo taken" if photo else "no photo for the notification",
            mower_id=str(mower_id),
            bytes=len(photo.data) if photo else 0,
        )

    async def dispatch(self, events: Sequence[NotificationEvent]) -> int:
        """Fan every event out; returns the number of new inbox items."""
        total = 0
        for event in events:
            total += await self._dispatch_one(event)
        return total

    async def recipients(self, account_id: UUID, mower_id: UUID) -> list[Preferences]:
        """The stored preferences that follow the mower, plus the defaults of every user of
        the account who registered a target but never saved preferences (once per user)."""
        found = await self._store.subscribers(account_id, mower_id)
        seen = {p.user_id for p in found}
        for user_id in await self._store.default_recipients(account_id):
            if user_id not in seen:
                seen.add(user_id)
                found.append(
                    default_preferences(user_id, account_id, language=self._config.default_language)
                )
        return found

    async def _dispatch_one(self, event: NotificationEvent) -> int:
        entry = await self._store.directory_entry(event.mower_id)
        name = entry.name if entry is not None and entry.account_id == event.account_id else None
        await self._raise(event, name)
        candidates = [
            p
            for p in await self.recipients(event.account_id, event.mower_id)
            if p.account_id == event.account_id and wants(p, event)
        ]
        if not candidates:
            return 0
        allowed = await self._views.can_view(
            [p.user_id for p in candidates], event.mower_id, relation=event.relation
        )
        alarm = event.kind in ALARM_KINDS
        silenced: set[UUID] = set()
        if alarm:
            silenced = await self._store.silenced(
                event.account_id,
                event.silence_key,
                [p.user_id for p, ok in zip(candidates, allowed, strict=True) if ok],
                self._clock(),
            )
        url = self.url_for(event.mower_id, event.link)
        image_id: UUID | None = None
        if (
            event.kind in IMAGE_KINDS
            and self._photos is not None
            and self._config.image_capture
            and self._reserve_photo(event.mower_id)
        ):
            image_id = uuid4()
        created = 0
        for preferences, ok in zip(candidates, allowed, strict=True):
            if not ok:
                _log.info(
                    "recipient dropped, relation denied",
                    relation=event.relation,
                    user_id=str(preferences.user_id),
                    mower_id=str(event.mower_id),
                    kind=event.kind.value,
                )
                continue
            if preferences.user_id in silenced:
                _log.info(
                    "recipient skipped, alarm acknowledged",
                    user_id=str(preferences.user_id),
                    mower_id=str(event.mower_id),
                    kind=event.kind.value,
                )
                continue
            params = {**event.params, "mower": name}
            notified = await self.notify(
                preferences,
                kind=event.kind,
                title_key=event.title_key,
                body_key=event.body_key,
                params=params,
                mower_id=event.mower_id,
                occurred_at=event.occurred_at,
                dedupe_key=event.dedupe_key,
                respect_quiet_hours=not alarm,
                url=url,
                image_id=image_id,
            )
            if notified is not None:
                created += 1
        if created and image_id is not None:
            self._take_photo(image_id, event.mower_id, event.account_id)
        if created:
            _log.info(
                "notification fanned out",
                kind=event.kind.value,
                mower_id=str(event.mower_id),
                recipients=created,
            )
        return created

    def render(
        self, language: str, title_key: str, body_key: str, params: dict[str, Any]
    ) -> tuple[str, str]:
        """Title and body in ``language``."""
        values = dict(params)
        if not values.get("mower"):
            values["mower"] = self._catalog.render(language, "mower.unnamed", {})
        return (
            self._catalog.render(language, title_key, values)[:200],
            self._catalog.render(language, body_key, values)[:1000],
        )

    def url_for(self, mower_id: UUID | None, link: str | None = None) -> str | None:
        """The app page of a mower, or ``link`` (an app path such as ``/mowers/<id>/map?...``),
        relative when no public app URL is configured."""
        base = (self._config.app_url or "").rstrip("/")
        if link is not None:
            return f"{base}{link}"
        return f"{base}/mowers/{mower_id}" if mower_id is not None else (base or "/")

    async def notify(
        self,
        preferences: Preferences,
        *,
        kind: NotificationKind,
        title_key: str,
        body_key: str,
        params: dict[str, Any],
        mower_id: UUID | None,
        occurred_at: datetime,
        dedupe_key: str,
        respect_quiet_hours: bool = True,
        url: str | None = None,
        image_id: UUID | None = None,
    ) -> Notified | None:
        """Store one rendered notification for one user and queue its deliveries.

        ``url`` defaults to the mower page. ``image_id`` reserves the camera photo of the
        event (it is stored under that id once taken). Returns None when the user already has
        this notification (same ``dedupe_key``).
        """
        now = self._clock()
        title, body = self.render(preferences.language, title_key, body_key, params)
        severity: NotificationSeverity = SEVERITY[kind]
        item = InboxItem(
            id=uuid4(),
            user_id=preferences.user_id,
            account_id=preferences.account_id,
            mower_id=mower_id,
            kind=kind,
            severity=severity,
            title=title,
            body=body,
            url=url if url is not None else self.url_for(mower_id),
            dedupe_key=dedupe_key,
            occurred_at=occurred_at,
            created_at=now,
            image_id=image_id,
        )
        message = Message(
            kind=kind,
            severity=severity,
            title=title,
            body=body,
            url=item.url,
            tag=f"{kind.value}:{mower_id}" if mower_id else kind.value,
            mower_id=mower_id,
            notification_id=item.id,
            image_id=image_id,
        )
        quiet = respect_quiet_hours and in_quiet_hours(
            preferences.quiet_hours, preferences.timezone, now
        )
        deliveries = [] if quiet else await self._deliveries(preferences, message)
        if not await self._store.record(item, deliveries, self._config.inbox_keep):
            return None
        channels = sorted({d.channel for d in deliveries})
        await self._publish(item, channels)
        return Notified(item=item, channels=channels, quiet=quiet)

    async def _deliveries(self, preferences: Preferences, message: Message) -> list[NewDelivery]:
        result: list[NewDelivery] = []
        user, account = preferences.user_id, preferences.account_id
        for name in sorted(preferences.channels):
            if self._channels.get(name) is None:
                continue
            if name == WEBPUSH:
                for subscription in await self._store.push_subscriptions(user, account):
                    result.append(
                        NewDelivery(
                            user,
                            account,
                            name,
                            message,
                            TargetKind.PUSH_SUBSCRIPTION,
                            subscription.id,
                        )
                    )
            elif name == APNS:
                for device in await self._store.apns_devices(user, account):
                    result.append(
                        NewDelivery(user, account, name, message, TargetKind.APNS_DEVICE, device.id)
                    )
            elif name == NTFY:
                if await self._store.ntfy_target(user, account) is not None:
                    result.append(NewDelivery(user, account, name, message, TargetKind.NTFY))
            else:
                result.append(NewDelivery(user, account, name, message))
        return result

    async def _raise(self, event: NotificationEvent, mower_name: str | None) -> None:
        """Publish the account-scoped ``notifications.raised`` (failures are logged only)."""
        if self._publisher is None:
            return
        try:
            params = {**event.params, "mower": mower_name}
            title, body = self.render(ACCOUNT_LANGUAGE, event.title_key, event.body_key, params)
            relation: Literal["can_view", "can_drive"] = (
                "can_drive" if event.relation == "can_drive" else "can_view"
            )
            await self._publisher.notification_raised(
                NotificationRaised(
                    notification_id=raised_id(event.account_id, event.dedupe_key),
                    account_id=event.account_id,
                    mower_id=event.mower_id,
                    kind=event.kind,
                    severity=event.severity,
                    title=title,
                    body=body,
                    url=self.url_for(event.mower_id, event.link),
                    occurred_at=event.occurred_at,
                    created_at=self._clock(),
                    dedupe_key=event.dedupe_key[:200],
                    required_relation=relation,
                )
            )
        except Exception as exc:
            _log.warning("publishing notifications.raised failed", error=repr(exc))

    async def _publish(self, item: InboxItem, channels: list[str]) -> None:
        if self._publisher is None:
            return
        try:
            await self._publisher.notification_sent(
                NotificationSent(
                    notification_id=item.id,
                    user_id=item.user_id,
                    account_id=item.account_id,
                    mower_id=item.mower_id,
                    kind=item.kind,
                    severity=item.severity,
                    title=item.title,
                    body=item.body,
                    url=item.url,
                    occurred_at=item.occurred_at,
                    channels=channels,
                    created_at=item.created_at,
                )
            )
        except Exception as exc:
            # The inbox item and the deliveries are stored; only the feed event is missing.
            _log.warning("publishing notifications.sent failed", error=repr(exc))
