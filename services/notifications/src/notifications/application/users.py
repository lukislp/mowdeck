"""What a user manages through the API: preferences, targets, inbox and test notifications.

Everything is keyed by the user and the account of the verified token. A mower can only be
selected when it belongs to that account and the user may view it (404 otherwise, so mowers of
other accounts stay invisible). Targets are validated before they are stored: a push endpoint
must be HTTPS on an allowed push service host, an ntfy server must be on the allow list, an APNs
device token must be hexadecimal (it only ever goes to Apple's push host); the service never sends
to an address a user typed freely.
"""

import base64
import binascii
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import NotificationKind

from notifications.application.context import NotificationsConfig
from notifications.application.fanout import Fanout, Notified
from notifications.application.ports import Channels, Clock, Store, ViewChecker
from notifications.domain.errors import InvalidInputError, NotFoundError, RateLimitedError
from notifications.domain.messages import MessageCatalog
from notifications.domain.models import (
    ALARM_KINDS,
    RULE_KINDS,
    ApnsDevice,
    InboxItem,
    NtfyTarget,
    Preferences,
    PushSubscription,
    QuietHours,
    silence_key,
)
from notifications.domain.preferences import valid_timezone

_TOPIC = re.compile(r"^[-_A-Za-z0-9]{1,64}$")
_APNS_TOKEN = re.compile(r"^[0-9a-f]{64,200}$")


@dataclass(frozen=True, slots=True)
class PreferencesInput:
    """The editable part of :class:`Preferences`."""

    enabled_kinds: frozenset[NotificationKind]
    all_mowers: bool
    mower_ids: frozenset[UUID]
    quiet_hours: QuietHours | None
    timezone: str
    language: str
    channels: frozenset[str]


def _b64url(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        msg = "not base64url"
        raise InvalidInputError(msg) from exc


def host_allowed(host: str, suffixes: Iterable[str]) -> bool:
    """True when ``host`` equals one of ``suffixes`` or ends with ``.<suffix>``."""
    allowed = [s.lower().lstrip(".") for s in suffixes if s]
    if not allowed:
        return True
    host = host.lower()
    return any(host == s or host.endswith(f".{s}") for s in allowed)


def normalize_origin(url: str) -> str:
    """``scheme://host[:port]`` of ``url`` in lower case, without a trailing slash."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        msg = "expected an http(s) URL"
        raise InvalidInputError(msg)
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        msg = "the server URL must not contain a path, query or credentials"
        raise InvalidInputError(msg)
    return f"{parts.scheme}://{parts.netloc}".lower()


class UserNotifications:
    """The use cases behind ``/api/v1/notifications``."""

    def __init__(
        self,
        store: Store,
        fanout: Fanout,
        channels: Channels,
        views: ViewChecker,
        catalog: MessageCatalog,
        config: NotificationsConfig,
        clock: Clock,
    ) -> None:
        self._store = store
        self._fanout = fanout
        self._channels = channels
        self._views = views
        self._catalog = catalog
        self._config = config
        self._clock = clock

    # --- preferences ---------------------------------------------------------------------

    async def preferences(self, user_id: UUID, account_id: UUID) -> Preferences:
        """The stored preferences, or the defaults (subscribed to nothing)."""
        stored = await self._store.preferences(user_id, account_id)
        return stored or Preferences(user_id=user_id, account_id=account_id)

    async def put_preferences(
        self, user_id: UUID, account_id: UUID, data: PreferencesInput
    ) -> Preferences:
        """Validate and store the preferences."""
        unknown_kinds = set(data.enabled_kinds) - RULE_KINDS
        if unknown_kinds:
            msg = f"kinds that cannot be enabled: {sorted(k.value for k in unknown_kinds)}"
            raise InvalidInputError(msg)
        if not valid_timezone(data.timezone):
            msg = f"unknown time zone {data.timezone!r}"
            raise InvalidInputError(msg)
        if data.language not in self._catalog.languages:
            msg = f"language must be one of {self._catalog.languages}"
            raise InvalidInputError(msg)
        known_channels = {"webpush", "ntfy", "apns", "log", *self._channels.names()}
        unknown_channels = set(data.channels) - known_channels
        if unknown_channels:
            msg = f"unknown channels: {sorted(unknown_channels)}"
            raise InvalidInputError(msg)
        await self._require_visible(user_id, account_id, data.mower_ids)
        current = await self.preferences(user_id, account_id)
        updated = current.evolve(
            enabled_kinds=frozenset(data.enabled_kinds),
            all_mowers=data.all_mowers,
            mower_ids=frozenset(data.mower_ids),
            quiet_hours=data.quiet_hours,
            timezone=data.timezone,
            language=data.language,
            channels=frozenset(data.channels),
            stored=True,
        )
        await self._store.put_preferences(updated)
        return updated

    async def _require_visible(
        self, user_id: UUID, account_id: UUID, mower_ids: frozenset[UUID]
    ) -> None:
        ordered = sorted(mower_ids)
        for mower_id in ordered:
            entry = await self._store.directory_entry(mower_id)
            if entry is None or entry.account_id != account_id:
                msg = f"mower {mower_id} not found"
                raise NotFoundError(msg)
        for mower_id in ordered:
            (allowed,) = await self._views.can_view([user_id], mower_id)
            if not allowed:
                msg = f"mower {mower_id} not found"
                raise NotFoundError(msg)

    # --- Web Push subscriptions ----------------------------------------------------------

    async def add_push_subscription(
        self,
        user_id: UUID,
        account_id: UUID,
        *,
        endpoint: str,
        p256dh: str,
        auth: str,
        user_agent: str | None,
    ) -> PushSubscription:
        """Store the browser's subscription (validated)."""
        parts = urlsplit(endpoint)
        if parts.scheme != "https" or not parts.hostname:
            msg = "the push endpoint must be an https URL"
            raise InvalidInputError(msg)
        if not host_allowed(parts.hostname, self._config.webpush_allowed_hosts):
            msg = "the push endpoint is not on an allowed push service"
            raise InvalidInputError(msg)
        if len(_b64url(p256dh)) != 65 or len(_b64url(auth)) != 16:
            msg = "keys.p256dh must be a P-256 public key and keys.auth 16 bytes (base64url)"
            raise InvalidInputError(msg)
        return await self._store.add_push_subscription(
            PushSubscription(
                id=uuid4(),
                user_id=user_id,
                account_id=account_id,
                endpoint=endpoint,
                p256dh=p256dh,
                auth=auth,
                created_at=self._clock(),
                user_agent=user_agent[:300] if user_agent else None,
            )
        )

    async def push_subscriptions(self, user_id: UUID, account_id: UUID) -> list[PushSubscription]:
        """The user's subscriptions."""
        return await self._store.push_subscriptions(user_id, account_id)

    async def delete_push_subscription(
        self, user_id: UUID, account_id: UUID, subscription_id: UUID
    ) -> None:
        """Delete one subscription of the user (404 for anything else)."""
        if not await self._store.delete_push_subscription(user_id, account_id, subscription_id):
            msg = "push subscription not found"
            raise NotFoundError(msg)

    # --- APNs devices --------------------------------------------------------------------

    async def add_apns_device(
        self, user_id: UUID, account_id: UUID, *, token: str, device_name: str | None
    ) -> ApnsDevice:
        """Store the iOS app's device token (hex, kept in lower case) for the user."""
        normalized = token.strip().lower()
        if not _APNS_TOKEN.match(normalized):
            msg = "token: 64 to 200 hexadecimal characters"
            raise InvalidInputError(msg)
        name = (device_name or "").strip()[:100] or None
        return await self._store.add_apns_device(
            ApnsDevice(
                id=uuid4(),
                user_id=user_id,
                account_id=account_id,
                token=normalized,
                device_name=name,
                created_at=self._clock(),
            )
        )

    async def apns_devices(self, user_id: UUID, account_id: UUID) -> list[ApnsDevice]:
        """The user's iOS devices."""
        return await self._store.apns_devices(user_id, account_id)

    async def delete_apns_device(self, user_id: UUID, account_id: UUID, device_id: UUID) -> None:
        """Delete one device of the user (404 for anything else)."""
        if not await self._store.delete_apns_device(user_id, account_id, device_id):
            msg = "apns device not found"
            raise NotFoundError(msg)

    # --- ntfy ----------------------------------------------------------------------------

    async def ntfy_target(self, user_id: UUID, account_id: UUID) -> NtfyTarget:
        """The user's ntfy target (404 when none is configured)."""
        target = await self._store.ntfy_target(user_id, account_id)
        if target is None:
            msg = "no ntfy target configured"
            raise NotFoundError(msg)
        return target

    async def put_ntfy_target(
        self, user_id: UUID, account_id: UUID, *, server_url: str, topic: str, token: str | None
    ) -> NtfyTarget:
        """Validate and store the user's ntfy target.

        ``token`` None keeps the stored token (it is write-only, so a client cannot send it
        back); an empty string removes it.
        """
        origin = normalize_origin(server_url)
        allowed = {normalize_origin(s) for s in self._config.ntfy_allowed_servers if s}
        if origin not in allowed:
            msg = f"ntfy server not allowed; allowed: {sorted(allowed)}"
            raise InvalidInputError(msg)
        if not _TOPIC.match(topic):
            msg = "topic: 1 to 64 letters, digits, '-' or '_'"
            raise InvalidInputError(msg)
        if token is None:
            stored = await self._store.ntfy_target(user_id, account_id)
            token = stored.token if stored else None
        target = NtfyTarget(
            user_id=user_id,
            account_id=account_id,
            server_url=origin,
            topic=topic,
            token=token or None,
            updated_at=self._clock(),
        )
        await self._store.put_ntfy_target(target)
        return target

    async def delete_ntfy_target(self, user_id: UUID, account_id: UUID) -> None:
        """Delete the user's ntfy target (404 when none is configured)."""
        if not await self._store.delete_ntfy_target(user_id, account_id):
            msg = "no ntfy target configured"
            raise NotFoundError(msg)

    # --- inbox ---------------------------------------------------------------------------

    async def inbox(
        self, user_id: UUID, account_id: UUID, limit: int, *, unread_only: bool
    ) -> tuple[list[InboxItem], int]:
        """The newest items and the number of unread items."""
        items = await self._store.inbox(user_id, account_id, limit, unread_only=unread_only)
        return items, await self._store.unread_count(user_id, account_id)

    async def mark_read(self, user_id: UUID, account_id: UUID, item_id: UUID) -> InboxItem:
        """Mark one of the user's items read (404 for anything else)."""
        item = await self._store.mark_read(user_id, account_id, item_id, self._clock())
        if item is None:
            msg = "notification not found"
            raise NotFoundError(msg)
        return item

    async def acknowledge(self, user_id: UUID, account_id: UUID, item_id: UUID) -> InboxItem:
        """Mark one of the user's items read and acknowledged (404 for anything else).

        Acknowledging a theft alarm also silences repeats of it (same kind and mower) for the
        user for ``ack_silence``.
        """
        now = self._clock()
        item = await self._store.acknowledge(user_id, account_id, item_id, now)
        if item is None:
            msg = "notification not found"
            raise NotFoundError(msg)
        if item.kind in ALARM_KINDS and item.mower_id is not None:
            await self._store.silence(
                user_id,
                account_id,
                silence_key(item.kind, item.mower_id),
                now + self._config.ack_silence,
            )
        return item

    # --- test ----------------------------------------------------------------------------

    async def send_test(self, user_id: UUID, account_id: UUID) -> Notified:
        """A test notification to every configured channel of the user (rate limited)."""
        now = self._clock()
        if not await self._store.try_test(user_id, account_id, now, self._config.test_every):
            msg = "a test notification was sent recently"
            raise RateLimitedError(msg, self._config.test_every)
        preferences = await self.preferences(user_id, account_id)
        notified = await self._fanout.notify(
            preferences,
            kind=NotificationKind.TEST,
            title_key="test.title",
            body_key="test.body",
            params={},
            mower_id=None,
            occurred_at=now,
            dedupe_key=f"test:{uuid4()}",
            respect_quiet_hours=False,
        )
        if notified is None:  # pragma: no cover - the dedupe key is fresh
            msg = "test notification was not stored"
            raise InvalidInputError(msg)
        return notified


def parse_time(value: str) -> time:
    """``HH:MM`` to a time (422 otherwise)."""
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        msg = f"expected HH:MM, got {value!r}"
        raise InvalidInputError(msg) from exc
    return parsed.replace(second=0, microsecond=0)
