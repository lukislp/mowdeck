"""Ports: what the use cases need from the outside world, as protocols."""

from collections.abc import Collection, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from mowdeck_contracts.v1 import NotificationRaised, NotificationSent

from notifications.domain.geofence import MowerFence
from notifications.domain.models import (
    ApnsDevice,
    Delivery,
    DeliveryResult,
    DeliveryStatus,
    DirectoryEntry,
    InboxItem,
    MowerWatch,
    NewDelivery,
    NotificationImage,
    NtfyTarget,
    Preferences,
    PushSubscription,
)


class Clock(Protocol):
    """Returns the current time (timezone-aware)."""

    def __call__(self) -> datetime:
        """The current time."""
        ...


class NotificationChannel(Protocol):
    """A way to reach a user (extension point ``mowdeck.notification_channels``).

    ``send`` must not raise for delivery problems; it reports them as a
    :class:`~notifications.domain.models.DeliveryResult` (``retry``, ``gone``, ``failed``).
    An exception counts as ``retry``.
    """

    name: str
    """The channel name stored with each delivery (``webpush``, ``ntfy``, ``log``, ...)."""

    async def send(self, delivery: Delivery) -> DeliveryResult:
        """Deliver one message to the delivery's target."""
        ...


class Channels(Protocol):
    """The configured channels (see ``notifications.adapters.channels``)."""

    def names(self) -> list[str]:
        """Names of the configured channels."""
        ...

    def get(self, name: str) -> NotificationChannel | None:
        """The channel called ``name``, None when it is not configured."""
        ...


class ViewChecker(Protocol):
    """Answers ``can_view`` (or another relation) on a mower for several users at once (fails
    closed)."""

    async def can_view(
        self, user_ids: Sequence[UUID], mower_id: UUID, relation: str = "can_view"
    ) -> list[bool]:
        """One answer per user, in order; errors answer False."""
        ...


@dataclass(frozen=True, slots=True)
class CapturedPhoto:
    """One still image of a mower's camera."""

    data: bytes
    media_type: str


class PhotoSource(Protocol):
    """Takes a still image of a mower's camera (through the Device-Gateway, ADR 0045)."""

    async def capture(self, mower_id: UUID, account_id: UUID) -> CapturedPhoto | None:
        """The image, None when none can be had (no camera, mower asleep, timeout, over the
        size limit); never raises for those."""
        ...


class EventPublisher(Protocol):
    """Publishes the service's events."""

    async def notification_sent(self, payload: NotificationSent) -> None:
        """Publish ``notifications.sent``."""
        ...

    async def notification_raised(self, payload: NotificationRaised) -> None:
        """Publish ``notifications.raised`` (the account-scoped event, for API tokens)."""
        ...


class Tx(Protocol):
    """Writes of one transaction (rules and directory)."""

    async def watch(self, mower_id: UUID) -> MowerWatch | None:
        """The watch of a mower, locked against concurrent writers."""
        ...

    async def put_watch(self, watch: MowerWatch) -> None:
        """Insert or replace the watch."""
        ...

    async def offline_candidates(
        self, no_state_before: datetime, disconnected_before: datetime, limit: int
    ) -> list[MowerWatch]:
        """Watches due for the offline rule, locked; rows locked elsewhere are skipped."""
        ...

    async def put_directory(self, entry: DirectoryEntry, *, replace: bool) -> None:
        """Insert a directory entry; with ``replace`` also overwrite an existing one."""
        ...

    async def drop_mower(self, mower_id: UUID) -> None:
        """Forget a mower (directory entry, watch and geofence)."""
        ...

    async def put_geofence(self, fence: MowerFence) -> bool:
        """Insert or replace the mower's geofence unless the stored one is newer; True when
        stored."""
        ...


class Store(Protocol):
    """Persistence of notifications."""

    def transaction(self) -> AbstractAsyncContextManager[Tx]:
        """A write transaction (all or nothing)."""
        ...

    # --- directory -----------------------------------------------------------------------

    async def directory_entry(self, mower_id: UUID) -> DirectoryEntry | None:
        """The directory entry of a mower."""
        ...

    async def geofence(self, mower_id: UUID) -> MowerFence | None:
        """The stored geofence of a mower."""
        ...

    # --- preferences ---------------------------------------------------------------------

    async def preferences(self, user_id: UUID, account_id: UUID) -> Preferences | None:
        """The stored preferences of a user in an account."""
        ...

    async def put_preferences(self, preferences: Preferences) -> None:
        """Insert or replace preferences."""
        ...

    async def subscribers(self, account_id: UUID, mower_id: UUID) -> list[Preferences]:
        """Stored preferences of every user of the account who follows the mower."""
        ...

    async def default_recipients(self, account_id: UUID) -> list[UUID]:
        """Users of the account who registered a delivery target (push subscription, APNs
        device or ntfy target) but never saved preferences, sorted; they get the defaults."""
        ...

    async def try_test(
        self, user_id: UUID, account_id: UUID, now: datetime, every: timedelta
    ) -> bool:
        """Record a test request; False when the last one is less than ``every`` ago."""
        ...

    # --- targets -------------------------------------------------------------------------

    async def add_push_subscription(self, subscription: PushSubscription) -> PushSubscription:
        """Store a subscription; the same endpoint replaces the earlier owner and keys."""
        ...

    async def push_subscriptions(self, user_id: UUID, account_id: UUID) -> list[PushSubscription]:
        """The user's subscriptions in the account, oldest first."""
        ...

    async def delete_push_subscription(
        self, user_id: UUID, account_id: UUID, subscription_id: UUID
    ) -> bool:
        """Delete one of the user's subscriptions; False when there is none with this id."""
        ...

    async def remove_push_subscription(self, subscription_id: UUID) -> None:
        """Delete a subscription the push service reported as gone."""
        ...

    async def add_apns_device(self, device: ApnsDevice) -> ApnsDevice:
        """Store an iOS device, one row per app installation and per token (the id is kept).

        With an ``installation_id``, the user's row of that installation gets the new token,
        account and name (a token rotated by iOS replaces the old one; a row that already held
        the new token is deleted). Otherwise, and for an installation not yet known, the same
        token replaces the earlier owner, account and name (and the installation, when given).
        """
        ...

    async def apns_devices(self, user_id: UUID, account_id: UUID) -> list[ApnsDevice]:
        """The user's iOS devices in the account, oldest first."""
        ...

    async def delete_apns_device(self, user_id: UUID, account_id: UUID, device_id: UUID) -> bool:
        """Delete one of the user's devices; False when there is none with this id."""
        ...

    async def remove_apns_device(self, device_id: UUID) -> None:
        """Delete a device APNs reported as unregistered."""
        ...

    async def ntfy_target(self, user_id: UUID, account_id: UUID) -> NtfyTarget | None:
        """The user's ntfy target in the account."""
        ...

    async def put_ntfy_target(self, target: NtfyTarget) -> None:
        """Insert or replace the user's ntfy target."""
        ...

    async def delete_ntfy_target(self, user_id: UUID, account_id: UUID) -> bool:
        """Delete the user's ntfy target; False when there is none."""
        ...

    # --- inbox and outbox ----------------------------------------------------------------

    async def record(self, item: InboxItem, deliveries: Sequence[NewDelivery], keep: int) -> bool:
        """Store an inbox item and its deliveries in one transaction.

        False (and nothing stored) when the user already has an item with the same
        ``dedupe_key``. Keeps the newest ``keep`` items of the user in the account.
        """
        ...

    async def inbox(
        self, user_id: UUID, account_id: UUID, limit: int, *, unread_only: bool
    ) -> list[InboxItem]:
        """The newest items, newest first."""
        ...

    async def unread_count(self, user_id: UUID, account_id: UUID) -> int:
        """Number of unread items."""
        ...

    async def mark_read(
        self, user_id: UUID, account_id: UUID, item_id: UUID, now: datetime
    ) -> InboxItem | None:
        """Mark one item read (idempotent); None when the user has no such item."""
        ...

    async def acknowledge(
        self, user_id: UUID, account_id: UUID, item_id: UUID, now: datetime
    ) -> InboxItem | None:
        """Mark one item read and acknowledged (idempotent, the first times are kept); None
        when the user has no such item."""
        ...

    async def silence(self, user_id: UUID, account_id: UUID, key: str, until: datetime) -> None:
        """Silence ``key`` (``<kind>:<mower_id>``) for the user until ``until`` (insert or
        extend)."""
        ...

    async def silenced(
        self, account_id: UUID, key: str, user_ids: Sequence[UUID], now: datetime
    ) -> set[UUID]:
        """The users among ``user_ids`` with a silence of ``key`` lasting beyond ``now``."""
        ...

    # --- camera images of error and theft events (ADR 0056) -------------------------------

    async def put_image(self, image: NotificationImage, keep: int) -> None:
        """Store the image (or the failure marker) of an event, replacing the same id; expired
        images are deleted and only the newest ``keep`` are kept."""
        ...

    async def notification_image(
        self, notification_id: UUID, now: datetime
    ) -> NotificationImage | None:
        """The unexpired image of the inbox item ``notification_id`` (any user: the caller
        proves possession with a signed URL), None when there is none yet."""
        ...

    async def inbox_image(
        self, user_id: UUID, account_id: UUID, item_id: UUID, now: datetime
    ) -> NotificationImage | None:
        """The unexpired image of one of the user's own inbox items."""
        ...

    async def prune_images(self, now: datetime) -> int:
        """Delete expired images."""
        ...

    async def claim_deliveries(
        self,
        now: datetime,
        lease: timedelta,
        limit: int,
        *,
        channel: str | None = None,
        exclude: Collection[str] = (),
    ) -> list[Delivery]:
        """Pending deliveries due at ``now``, with their targets resolved.

        Each claimed row gets ``attempts + 1`` and ``next_attempt_at = now + lease`` in one
        short transaction (``FOR UPDATE SKIP LOCKED``), so other replicas skip it and a crashed
        or hung worker's claim expires after the lease (a restart picks it up again).
        ``channel`` claims only that channel's deliveries, ``exclude`` skips those channels.
        """
        ...

    async def finish_delivery(
        self,
        delivery_id: UUID,
        status: DeliveryStatus,
        *,
        now: datetime,
        error: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> None:
        """Record the result of an attempt (``pending`` with ``next_attempt_at``: retry)."""
        ...

    async def prune_deliveries(self, before: datetime) -> int:
        """Delete finished deliveries last updated before ``before``."""
        ...

    async def drop_user(self, user_id: UUID, account_id: UUID | None) -> None:
        """Delete everything of a user (in one account, or in all when ``account_id`` is None),
        silences included."""
        ...
