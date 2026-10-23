"""Domain models of notifications: plain data, no I/O.

``MowerWatch`` is what the rules remember about one mower between two messages; it is stored
per mower (JSON) and updated under a row lock, so every replica sees the same history.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import NotificationKind, NotificationSeverity

SEVERITY: dict[NotificationKind, NotificationSeverity] = {
    NotificationKind.MOWING_FINISHED: NotificationSeverity.INFO,
    NotificationKind.JOB_STARTED: NotificationSeverity.INFO,
    NotificationKind.MOWER_ONLINE: NotificationSeverity.INFO,
    NotificationKind.TEST: NotificationSeverity.INFO,
    NotificationKind.BATTERY_LOW: NotificationSeverity.WARNING,
    NotificationKind.MOWER_OFFLINE: NotificationSeverity.WARNING,
    NotificationKind.MOWER_STUCK: NotificationSeverity.CRITICAL,
    NotificationKind.MOWER_LIFTED: NotificationSeverity.CRITICAL,
    NotificationKind.MOWER_LEFT_DOCK_UNEXPECTEDLY: NotificationSeverity.CRITICAL,
    NotificationKind.GEOFENCE_EXIT: NotificationSeverity.CRITICAL,
    NotificationKind.BLADE_MAINTENANCE: NotificationSeverity.INFO,
    NotificationKind.VISION_PROPOSAL: NotificationSeverity.WARNING,
    NotificationKind.VISION_AUTO_PROPOSAL: NotificationSeverity.WARNING,
    NotificationKind.FIRMWARE_UPDATE_AVAILABLE: NotificationSeverity.INFO,
    NotificationKind.FIRMWARE_UPDATE_FINISHED: NotificationSeverity.INFO,
    NotificationKind.FIRMWARE_UPDATE_FAILED: NotificationSeverity.WARNING,
}
"""Severity of every kind (the contract has no "low" severity: the blade reminder is ``info``)."""

TTL_SECONDS: dict[NotificationSeverity, int] = {
    NotificationSeverity.CRITICAL: 24 * 3600,
    NotificationSeverity.WARNING: 6 * 3600,
    NotificationSeverity.INFO: 3600,
}
"""How long a notification is worth delivering, by severity (Web Push ``TTL``, APNs
``apns-expiration``); the outbox drops a delivery that is still pending after this."""

ALARM_KINDS: frozenset[NotificationKind] = frozenset(
    {
        NotificationKind.MOWER_LIFTED,
        NotificationKind.MOWER_LEFT_DOCK_UNEXPECTEDLY,
        NotificationKind.GEOFENCE_EXIT,
    }
)
"""The theft alarms: they bypass quiet hours, use ``alarm_cooldown_seconds`` and can be
acknowledged (which silences repeats for the user)."""

RULE_KINDS: frozenset[NotificationKind] = frozenset(SEVERITY) - {NotificationKind.TEST}
"""Kinds the rules derive (a user may enable each of them)."""

DEFAULT_KINDS: frozenset[NotificationKind] = RULE_KINDS - {NotificationKind.JOB_STARTED}
"""Kinds enabled for a user who has not chosen (``job_started`` is off by default)."""


@dataclass(frozen=True, slots=True)
class RuleConfig:
    """Thresholds of the rules (from the settings)."""

    mowing_min_seconds: float = 120
    """A mow shorter than this ends without ``mowing_finished``."""
    stuck_no_move_seconds: float = 300
    """``mower_stuck`` when the position has not changed for this long while mowing (0: off)."""
    stuck_move_meters: float = 0.5
    """A position change smaller than this counts as "not moved"."""
    battery_low_percent: int = 20
    """``battery_low`` when the battery falls below this while not charging."""
    battery_rearm_percent: int = 30
    """The battery rule fires again only after the battery was at or above this (hysteresis)."""
    offline_after_seconds: float = 120
    """``mower_offline`` when no state arrived for this long."""
    disconnect_grace_seconds: float = 30
    """``mower.disconnected`` turns into ``mower_offline`` when no reconnect follows within this
    time (0: at once), so a Device-Gateway restart notifies nobody."""
    cooldown_seconds: float = 300
    """At most one notification of one kind per mower within this time."""
    alarm_cooldown_seconds: float = 600
    """The cooldown of the theft alarms (:data:`ALARM_KINDS`), per kind and mower."""
    lift_error_codes: frozenset[str] = frozenset(
        {"lifted", "1004", "1030", "2502", "2801", "11131", "11133"}
    )
    """Error codes that mean "lifted" (besides ``raw["lifted"] is True``)."""
    dock_command_window_seconds: float = 600
    """A command seen this long before the mower leaves the dock explains the departure."""
    dock_command_grace_seconds: float = 30
    """A command issued up to this long after the state still explains it (clock skew)."""
    dock_confirm_seconds: float = 20
    """An unexplained departure alarms only when a state at least this much later still shows
    the mower away from the dock without a command (0: at once)."""
    geofence_margin_meters: float = 5
    """The allowed region is the mapped areas, paths and dock buffered by this margin."""
    geofence_confirm_states: int = 2
    """Consecutive states outside the fence before ``geofence_exit`` fires."""


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    """A notification derived for one mower, before it is rendered for recipients."""

    kind: NotificationKind
    mower_id: UUID
    account_id: UUID
    occurred_at: datetime
    title_key: str
    body_key: str
    params: dict[str, Any]
    severity: NotificationSeverity
    dedupe_key: str
    """Identifies this occurrence; a repeated event with the same key is stored once per user."""
    link: str | None = None
    """App path the notification opens (``/mowers/<id>/map?...``); None: the mower page."""
    relation: str = "can_view"
    """Relation a recipient needs on the mower: ``can_view``, or ``can_drive`` for
    ``vision_proposal`` and ``vision_auto_proposal`` (only users who may approve the maneuver
    hear about it)."""

    @property
    def silence_key(self) -> str:
        """The key an acknowledgement silences: ``<kind>:<mower_id>`` (all occurrences)."""
        return silence_key(self.kind, self.mower_id)

    @classmethod
    def of(
        cls,
        kind: NotificationKind,
        mower_id: UUID,
        account_id: UUID,
        occurred_at: datetime,
        discriminator: str,
        *,
        variant: str | None = None,
        link: str | None = None,
        relation: str = "can_view",
        **params: Any,
    ) -> "NotificationEvent":
        """Build an event with the message keys ``<kind>.title`` and ``<kind>.body`` (or
        ``<kind>.body_<variant>``)."""
        return cls(
            kind=kind,
            mower_id=mower_id,
            account_id=account_id,
            occurred_at=occurred_at,
            title_key=f"{kind.value}.title",
            body_key=f"{kind.value}.body_{variant}" if variant else f"{kind.value}.body",
            params=params,
            severity=SEVERITY[kind],
            dedupe_key=f"{kind.value}:{mower_id}:{discriminator}",
            link=link,
            relation=relation,
        )


def silence_key(kind: NotificationKind, mower_id: UUID) -> str:
    """``<kind>:<mower_id>``: what an acknowledgement silences for one user."""
    return f"{kind.value}:{mower_id}"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True, slots=True)
class MowerWatch:
    """What the rules remember about one mower."""

    mower_id: UUID
    account_id: UUID
    last_seen_at: datetime
    """When the last state or connect arrived (the service clock)."""
    last_observed_at: datetime | None = None
    """``observed_at`` of the newest state (older states are ignored)."""
    activity: str | None = None
    battery_percent: int | None = None
    job_started_at: datetime | None = None
    """Set while a mowing job runs (the first ``mowing`` state of the job)."""
    battery_armed: bool = True
    """False after ``battery_low`` until the battery was back at the re-arm level."""
    error_notified: str | None = None
    """The error that was notified; cleared when the mower leaves the error state."""
    anchor_lat: float | None = None
    anchor_lon: float | None = None
    anchor_since: datetime | None = None
    """Where and since when the mower stands still while mowing."""
    no_move_notified: bool = False
    disconnected_at: datetime | None = None
    """``mower.disconnected`` without a reconnect yet."""
    offline: bool = False
    offline_since: datetime | None = None
    offline_notified: bool = False
    """Whether ``mower_offline`` went out (``mower_online`` follows only then)."""
    last_sent: dict[str, datetime] = field(default_factory=dict)
    """Last notification per kind (cooldown)."""
    last_command_at: datetime | None = None
    """``issued_at`` of the newest command seen for the mower (any kind)."""
    last_drive_at: datetime | None = None
    """``issued_at`` of the newest manual drive frame seen for the mower."""
    left_dock_at: datetime | None = None
    """An unexplained departure from the dock, waiting for confirmation."""
    outside_count: int = 0
    """Consecutive states outside the geofence."""
    geofence_notified: bool = False
    """``geofence_exit`` went out; re-armed once the mower is inside again."""
    firmware_state: str | None = None
    """``firmware_info.update_state`` of the newest state (None: no firmware information)."""
    firmware_offered: str | None = None
    """The firmware version ``firmware_update_available`` was sent for (once per version)."""

    def evolve(self, **changes: Any) -> "MowerWatch":
        """A copy with ``changes``."""
        return replace(self, **changes)

    def to_json(self) -> dict[str, Any]:
        """JSON form for the store."""
        return {
            "last_seen_at": _iso(self.last_seen_at),
            "last_observed_at": _iso(self.last_observed_at),
            "activity": self.activity,
            "battery_percent": self.battery_percent,
            "job_started_at": _iso(self.job_started_at),
            "battery_armed": self.battery_armed,
            "error_notified": self.error_notified,
            "anchor_lat": self.anchor_lat,
            "anchor_lon": self.anchor_lon,
            "anchor_since": _iso(self.anchor_since),
            "no_move_notified": self.no_move_notified,
            "disconnected_at": _iso(self.disconnected_at),
            "offline": self.offline,
            "offline_since": _iso(self.offline_since),
            "offline_notified": self.offline_notified,
            "last_sent": {k: v.isoformat() for k, v in self.last_sent.items()},
            "last_command_at": _iso(self.last_command_at),
            "last_drive_at": _iso(self.last_drive_at),
            "left_dock_at": _iso(self.left_dock_at),
            "outside_count": self.outside_count,
            "geofence_notified": self.geofence_notified,
            "firmware_state": self.firmware_state,
            "firmware_offered": self.firmware_offered,
        }

    @classmethod
    def from_json(cls, mower_id: UUID, account_id: UUID, data: dict[str, Any]) -> "MowerWatch":
        """Inverse of :meth:`to_json`."""
        last_seen = _parse(data.get("last_seen_at"))
        if last_seen is None:
            msg = "a stored watch has no last_seen_at"
            raise ValueError(msg)
        return cls(
            mower_id=mower_id,
            account_id=account_id,
            last_seen_at=last_seen,
            last_observed_at=_parse(data.get("last_observed_at")),
            activity=data.get("activity"),
            battery_percent=data.get("battery_percent"),
            job_started_at=_parse(data.get("job_started_at")),
            battery_armed=bool(data.get("battery_armed", True)),
            error_notified=data.get("error_notified"),
            anchor_lat=data.get("anchor_lat"),
            anchor_lon=data.get("anchor_lon"),
            anchor_since=_parse(data.get("anchor_since")),
            no_move_notified=bool(data.get("no_move_notified", False)),
            disconnected_at=_parse(data.get("disconnected_at")),
            offline=bool(data.get("offline", False)),
            offline_since=_parse(data.get("offline_since")),
            offline_notified=bool(data.get("offline_notified", False)),
            last_sent={
                k: datetime.fromisoformat(v) for k, v in dict(data.get("last_sent", {})).items()
            },
            last_command_at=_parse(data.get("last_command_at")),
            last_drive_at=_parse(data.get("last_drive_at")),
            left_dock_at=_parse(data.get("left_dock_at")),
            outside_count=int(data.get("outside_count", 0)),
            geofence_notified=bool(data.get("geofence_notified", False)),
            firmware_state=data.get("firmware_state"),
            firmware_offered=data.get("firmware_offered"),
        )


@dataclass(frozen=True, slots=True)
class QuietHours:
    """A daily window in the user's time zone; ``start == end`` means never quiet."""

    start: time
    end: time


ALL_CHANNELS_DEFAULT: frozenset[str] = frozenset({"webpush", "ntfy", "apns", "log"})
"""Channels a user gets by default (only those the service has configured are used)."""


@dataclass(frozen=True, slots=True)
class Preferences:
    """What one user of one account wants to be notified about."""

    user_id: UUID
    account_id: UUID
    enabled_kinds: frozenset[NotificationKind] = DEFAULT_KINDS
    all_mowers: bool = False
    """Subscribed to every mower the user can view (now and later)."""
    mower_ids: frozenset[UUID] = frozenset()
    """Subscribed mowers when ``all_mowers`` is False."""
    quiet_hours: QuietHours | None = None
    timezone: str = "UTC"
    language: str = "en"
    channels: frozenset[str] = ALL_CHANNELS_DEFAULT
    """Channels the user wants (deliveries are queued only for configured targets)."""
    last_test_at: datetime | None = None
    stored: bool = False
    """False for the defaults of a user who never saved preferences."""

    def subscribed(self, mower_id: UUID) -> bool:
        """True when the user follows ``mower_id``."""
        return self.all_mowers or mower_id in self.mower_ids

    def evolve(self, **changes: Any) -> "Preferences":
        """A copy with ``changes``."""
        return replace(self, **changes)


class DeliveryStatus(StrEnum):
    """State of a delivery in the outbox."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    """Gave up (permanent error or the maximum number of attempts)."""
    DROPPED = "dropped"
    """The target is gone (expired push subscription, removed ntfy target)."""


class DeliveryOutcome(StrEnum):
    """What a channel reports for one attempt."""

    SENT = "sent"
    RETRY = "retry"
    """Temporary failure: try again later (network error, 429, 5xx)."""
    GONE = "gone"
    """The target no longer exists (Web Push 404 or 410, APNs ``Unregistered``): remove it, do
    not retry."""
    FAILED = "failed"
    """Permanent failure (for example 400 or 413): do not retry."""


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Result of one ``NotificationChannel.send``."""

    outcome: DeliveryOutcome
    error: str | None = None
    retry_after: timedelta | None = None
    """The server's ``Retry-After``, if any (used when longer than the backoff)."""

    @classmethod
    def sent(cls) -> "DeliveryResult":
        """Delivered."""
        return cls(DeliveryOutcome.SENT)


@dataclass(frozen=True, slots=True)
class Message:
    """A rendered notification (one language)."""

    kind: NotificationKind
    severity: NotificationSeverity
    title: str
    body: str
    url: str | None
    tag: str
    """Groups repeated notifications (Web Push ``tag``/``Topic``)."""
    mower_id: UUID | None
    notification_id: UUID | None = None

    def to_json(self) -> dict[str, Any]:
        """JSON form for the outbox."""
        return {
            "kind": self.kind.value,
            "severity": self.severity.value,
            "title": self.title,
            "body": self.body,
            "url": self.url,
            "tag": self.tag,
            "mower_id": str(self.mower_id) if self.mower_id else None,
            "notification_id": str(self.notification_id) if self.notification_id else None,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Message":
        """Inverse of :meth:`to_json`."""
        return cls(
            kind=NotificationKind(data["kind"]),
            severity=NotificationSeverity(data["severity"]),
            title=data["title"],
            body=data["body"],
            url=data.get("url"),
            tag=data["tag"],
            mower_id=UUID(data["mower_id"]) if data.get("mower_id") else None,
            notification_id=(
                UUID(data["notification_id"]) if data.get("notification_id") else None
            ),
        )


class TargetKind(StrEnum):
    """What a delivery's target refers to."""

    NONE = "none"
    PUSH_SUBSCRIPTION = "push_subscription"
    NTFY = "ntfy"
    APNS_DEVICE = "apns_device"


@dataclass(frozen=True, slots=True)
class NewDelivery:
    """A delivery to enqueue."""

    user_id: UUID
    account_id: UUID
    channel: str
    message: Message
    target_kind: TargetKind = TargetKind.NONE
    target_id: UUID | None = None
    """The push subscription for Web Push deliveries, the device for APNs deliveries."""


@dataclass(frozen=True, slots=True)
class Delivery:
    """One message to one target over one channel, as the worker hands it to the channel."""

    id: UUID
    user_id: UUID
    account_id: UUID
    channel: str
    message: Message
    attempts: int
    """Attempts including this one."""
    target_kind: TargetKind
    target_id: UUID | None
    target: dict[str, Any] | None
    """Channel specific target (push subscription, ntfy target, APNs device token), resolved
    when claimed; None when it no longer exists."""
    created_at: datetime | None = None
    """When the delivery was queued (None: unknown, never expires)."""

    def expires_at(self) -> datetime | None:
        """The end of the message's TTL (:data:`TTL_SECONDS` of its severity)."""
        if self.created_at is None:
            return None
        return self.created_at + timedelta(seconds=TTL_SECONDS[self.message.severity])


@dataclass(frozen=True, slots=True)
class InboxItem:
    """One notification in a user's inbox."""

    id: UUID
    user_id: UUID
    account_id: UUID
    mower_id: UUID | None
    kind: NotificationKind
    severity: NotificationSeverity
    title: str
    body: str
    url: str | None
    dedupe_key: str
    occurred_at: datetime
    created_at: datetime
    read_at: datetime | None = None
    acked_at: datetime | None = None
    """When the user acknowledged the notification (alarms)."""


@dataclass(frozen=True, slots=True)
class PushSubscription:
    """A browser's Web Push subscription of one user."""

    id: UUID
    user_id: UUID
    account_id: UUID
    endpoint: str
    p256dh: str
    auth: str
    created_at: datetime
    user_agent: str | None = None

    def subscription_info(self) -> dict[str, Any]:
        """The ``PushSubscription.toJSON()`` shape (``endpoint`` and ``keys``)."""
        return {"endpoint": self.endpoint, "keys": {"p256dh": self.p256dh, "auth": self.auth}}


@dataclass(frozen=True, slots=True)
class ApnsDevice:
    """An iOS device of one user that receives notifications over APNs."""

    id: UUID
    user_id: UUID
    account_id: UUID
    token: str
    """The APNs device token in lower case hex (a capability: never returned by the API)."""
    device_name: str | None
    created_at: datetime

    def target(self) -> dict[str, Any]:
        """The delivery target the APNs channel receives."""
        return {"token": self.token}


@dataclass(frozen=True, slots=True)
class NtfyTarget:
    """Where one user receives ntfy messages."""

    user_id: UUID
    account_id: UUID
    server_url: str
    topic: str
    token: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    """A mower, its account and its display name (from Fleet)."""

    mower_id: UUID
    account_id: UUID
    name: str
    updated_at: datetime


def backoff_delay(attempts: int, base: timedelta, maximum: timedelta) -> timedelta:
    """Exponential backoff after ``attempts`` failed attempts: base, 2 base, 4 base, ..."""
    exponent = min(max(attempts - 1, 0), 30)
    delay: timedelta = base * (2**exponent)
    return delay if delay < maximum else maximum
