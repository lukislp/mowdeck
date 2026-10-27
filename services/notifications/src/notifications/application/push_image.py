"""Serving the push notification image to the iOS Notification Service Extension (ADR 0056).

The request carries no login, only the signed URL (:mod:`notifications.domain.push_image`), so
every answer is decided here:

* signature wrong (or the notification unknown): 404, indistinguishable from "no such page";
* signature genuine but expired: 410;
* the photo is not there yet: 404 (the extension tries again within its time budget);
* taking the photo failed or the photo expired: 410 (the extension stops waiting);
* otherwise the photo.

Requests are rate limited, per notification and in total, in memory (per replica): the extension
asks about once a second for a few seconds, so a handful of requests per notification is normal.
"""

from collections import deque
from datetime import datetime, timedelta
from uuid import UUID

from notifications.application.ports import Clock, Store
from notifications.domain.errors import GoneError, NotFoundError, RateLimitedError
from notifications.domain.models import NotificationImage
from notifications.domain.push_image import ImageLinks

PER_NOTIFICATION = 40
"""Requests per notification within :data:`WINDOW` (the extension polls for <= 30 s)."""
IN_TOTAL = 600
"""Requests per :data:`WINDOW` over all notifications."""
WINDOW = timedelta(minutes=1)


class SlidingWindow:
    """Counts events within a window."""

    def __init__(self, limit: int, window: timedelta) -> None:
        self._limit = limit
        self._window = window
        self._events: deque[datetime] = deque()

    def hit(self, now: datetime) -> bool:
        """Count one event; False when the limit is exceeded (the event is not counted)."""
        while self._events and now - self._events[0] >= self._window:
            self._events.popleft()
        if len(self._events) >= self._limit:
            return False
        self._events.append(now)
        return True

    def idle(self, now: datetime) -> bool:
        """True when nothing happened within the window."""
        return not self._events or now - self._events[-1] >= self._window


class PushImages:
    """Checks a signed push image URL and hands out the photo."""

    def __init__(self, store: Store, links: ImageLinks, clock: Clock) -> None:
        self._store = store
        self._links = links
        self._clock = clock
        self._total = SlidingWindow(IN_TOTAL, WINDOW)
        self._each: dict[UUID, SlidingWindow] = {}

    def _limit(self, notification_id: UUID, now: datetime) -> None:
        for key in [k for k, w in self._each.items() if w.idle(now)]:
            del self._each[key]
        window = self._each.setdefault(notification_id, SlidingWindow(PER_NOTIFICATION, WINDOW))
        if not self._total.hit(now) or not window.hit(now):
            msg = "too many requests"
            raise RateLimitedError(msg, WINDOW)

    async def fetch(self, notification_id: UUID, expires: int, signature: str) -> NotificationImage:
        """The photo for a signed URL; raises :class:`NotFoundError`, :class:`GoneError` or
        :class:`RateLimitedError`."""
        now = self._clock()
        if not self._links.genuine(notification_id, expires, signature):
            msg = "not found"
            raise NotFoundError(msg)
        self._limit(notification_id, now)
        if not self._links.verify(notification_id, expires, signature, now):
            msg = "this link has expired"
            raise GoneError(msg)
        image = await self._store.notification_image(notification_id, now)
        if image is None:
            msg = "the image is not ready"
            raise NotFoundError(msg)
        if not image.ready:
            msg = "no image was taken"
            raise GoneError(msg)
        return image
