"""Request and response models of the notifications API."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from mowdeck_contracts.v1 import NotificationKind, NotificationSeverity
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from notifications.application.users import PreferencesInput, parse_time
from notifications.domain.live_activity import ActivityDevice, LiveActivity
from notifications.domain.models import (
    RULE_KINDS,
    ApnsDevice,
    InboxItem,
    NtfyTarget,
    Preferences,
    PushSubscription,
    QuietHours,
)

HhMm = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class QuietHoursBody(BaseModel):
    """A daily window (local time of ``timezone``) without pushes; may cross midnight."""

    start: HhMm = Field(examples=["22:00"])
    end: HhMm = Field(examples=["07:00"])


class PreferencesBody(BaseModel):
    """What the user wants to be notified about (``PUT`` replaces everything)."""

    model_config = ConfigDict(extra="forbid")

    enabled_kinds: list[NotificationKind] = Field(
        max_length=20, description="Kinds to notify; job_started is off by default."
    )
    all_mowers: bool = Field(
        default=False, description="Follow every mower the user can view, now and later."
    )
    mower_ids: list[UUID] = Field(
        default_factory=list,
        max_length=1000,
        description="Followed mowers when all_mowers is false (each must be viewable, else 404).",
    )
    quiet_hours: QuietHoursBody | None = None
    timezone: str = Field(default="UTC", max_length=64, examples=["Europe/Berlin"])
    language: str = Field(default="en", max_length=8, examples=["de"])
    channels: list[str] = Field(
        default_factory=lambda: ["webpush", "ntfy", "apns"],
        max_length=10,
        description="Channels to use when a target is configured (webpush, ntfy, apns, ...).",
    )

    def to_input(self) -> PreferencesInput:
        """The use case input."""
        quiet = None
        if self.quiet_hours is not None:
            quiet = QuietHours(parse_time(self.quiet_hours.start), parse_time(self.quiet_hours.end))
        return PreferencesInput(
            enabled_kinds=frozenset(self.enabled_kinds),
            all_mowers=self.all_mowers,
            mower_ids=frozenset(self.mower_ids),
            quiet_hours=quiet,
            timezone=self.timezone,
            language=self.language,
            channels=frozenset(self.channels),
        )


class PreferencesOut(BaseModel):
    """Stored preferences (or the defaults) plus what can be chosen."""

    enabled_kinds: list[NotificationKind]
    all_mowers: bool
    mower_ids: list[UUID]
    quiet_hours: QuietHoursBody | None
    timezone: str
    language: str
    channels: list[str]
    stored: bool = Field(description="False until the user saved preferences once.")
    available_kinds: list[NotificationKind]
    available_channels: list[str] = Field(description="Channels configured on this server.")
    languages: list[str]

    @classmethod
    def of(
        cls, preferences: Preferences, *, channels: list[str], languages: list[str]
    ) -> "PreferencesOut":
        """Build from the domain model."""
        quiet = preferences.quiet_hours
        return cls(
            enabled_kinds=sorted(preferences.enabled_kinds),
            all_mowers=preferences.all_mowers,
            mower_ids=sorted(preferences.mower_ids),
            quiet_hours=QuietHoursBody(
                start=quiet.start.strftime("%H:%M"), end=quiet.end.strftime("%H:%M")
            )
            if quiet
            else None,
            timezone=preferences.timezone,
            language=preferences.language,
            channels=sorted(preferences.channels),
            stored=preferences.stored,
            available_kinds=sorted(RULE_KINDS),
            available_channels=channels,
            languages=languages,
        )


class PushKeys(BaseModel):
    """``PushSubscription.toJSON().keys``."""

    p256dh: str = Field(min_length=1, max_length=128)
    auth: str = Field(min_length=1, max_length=64)


class PushSubscriptionIn(BaseModel):
    """The browser's ``PushSubscription.toJSON()`` (``expirationTime`` is ignored)."""

    endpoint: str = Field(min_length=10, max_length=2000)
    keys: PushKeys


class PushSubscriptionOut(BaseModel):
    """A stored subscription; the endpoint itself (a capability URL) is not echoed."""

    id: UUID
    endpoint_host: str
    user_agent: str | None
    created_at: datetime

    @classmethod
    def of(cls, subscription: PushSubscription) -> "PushSubscriptionOut":
        """Build from the domain model."""
        host = subscription.endpoint.split("//", 1)[-1].split("/", 1)[0]
        return cls(
            id=subscription.id,
            endpoint_host=host,
            user_agent=subscription.user_agent,
            created_at=subscription.created_at,
        )


class PushSubscriptionList(BaseModel):
    """The user's subscriptions."""

    items: list[PushSubscriptionOut]


class ApnsDeviceIn(BaseModel):
    """The iOS app's APNs device token (from ``didRegisterForRemoteNotificationsWithDeviceToken``,
    hex encoded)."""

    token: str = Field(
        min_length=64,
        max_length=200,
        pattern=r"^[0-9a-fA-F]+$",
        description="Device token in hexadecimal (case-insensitive, stored in lower case).",
    )
    device_name: str | None = Field(
        default=None, max_length=100, examples=["Lukas' iPhone"], description="Shown in the list."
    )
    installation_id: UUID | None = Field(
        default=None,
        description=(
            "Stable id of the app installation (a UUID the iOS shell keeps in the Keychain). A"
            " new token of the same installation replaces the earlier one, so a device never"
            " holds two registrations. Without it (older app builds) each token is its own"
            " device."
        ),
    )


class ApnsDeviceOut(BaseModel):
    """A registered iOS device; the token itself (a capability) is not echoed."""

    id: UUID
    device_name: str | None
    token_suffix: str = Field(description="The last 8 hex characters of the token.")
    created_at: datetime

    @classmethod
    def of(cls, device: ApnsDevice) -> "ApnsDeviceOut":
        """Build from the domain model."""
        return cls(
            id=device.id,
            device_name=device.device_name,
            token_suffix=device.token[-8:],
            created_at=device.created_at,
        )


class ApnsDeviceList(BaseModel):
    """The user's iOS devices, oldest first."""

    items: list[ApnsDeviceOut]


class VapidPublicKey(BaseModel):
    """The ``applicationServerKey`` for ``pushManager.subscribe``."""

    public_key: str


class NtfyIn(BaseModel):
    """The user's ntfy target."""

    model_config = ConfigDict(extra="forbid")

    server_url: str = Field(max_length=300, examples=["https://ntfy.sh"])
    topic: str = Field(min_length=1, max_length=64, examples=["mowdeck-lukas-7f3a"])
    token: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Access token (tk_...), stored, never returned. Omitted or null keeps the stored "
            "token; an empty string removes it."
        ),
    )


class NtfyOut(BaseModel):
    """The stored ntfy target (without the token)."""

    server_url: str
    topic: str
    has_token: bool
    updated_at: datetime

    @classmethod
    def of(cls, target: NtfyTarget) -> "NtfyOut":
        """Build from the domain model."""
        return cls(
            server_url=target.server_url,
            topic=target.topic,
            has_token=bool(target.token),
            updated_at=target.updated_at,
        )


class InboxItemOut(BaseModel):
    """One notification of the user."""

    id: UUID
    mower_id: UUID | None
    kind: NotificationKind
    severity: NotificationSeverity
    title: str
    body: str
    url: str | None
    occurred_at: datetime
    created_at: datetime
    read: bool
    read_at: datetime | None
    acked: bool = Field(default=False, description="Acknowledged by the user (alarms).")
    acked_at: datetime | None = None
    has_image: bool = Field(
        default=False,
        description="A camera photo was taken for this error or theft event and is still kept "
        "(a day): `GET /api/v1/notifications/inbox/{id}/image` returns it.",
    )

    @classmethod
    def of(cls, item: InboxItem) -> "InboxItemOut":
        """Build from the domain model."""
        return cls(
            id=item.id,
            mower_id=item.mower_id,
            kind=item.kind,
            severity=item.severity,
            title=item.title,
            body=item.body,
            url=item.url,
            occurred_at=item.occurred_at,
            created_at=item.created_at,
            read=item.read_at is not None,
            read_at=item.read_at,
            acked=item.acked_at is not None,
            acked_at=item.acked_at,
            has_image=item.has_image,
        )


class Inbox(BaseModel):
    """The newest notifications, newest first."""

    items: list[InboxItemOut]
    unread: int


class TestSent(BaseModel):
    """Answer of ``POST /test``."""

    notification_id: UUID
    channels: list[str] = Field(description="Channels a delivery was queued for.")
    title: str
    body: str


class LiveActivityDeviceIn(BaseModel):
    """What one app installation offers for Live Activities and widgets (ADR 0059)."""

    installation_id: UUID = Field(
        description="The installation id the iOS shell keeps in the Keychain "
        "(the same as for `POST /me/push/apns`)."
    )
    push_to_start_token: str | None = Field(
        default=None,
        min_length=64,
        max_length=200,
        pattern=r"^[0-9a-fA-F]+$",
        description="ActivityKit push-to-start token (hex); null when the device has none "
        "(before iOS 17.2) or the user turned Live Activities off.",
    )
    widgets_enabled: bool = Field(
        default=False, description="The installation has MowDeck widgets: reload them by push."
    )


class LiveActivityDeviceOut(BaseModel):
    """The stored registration (the token, a capability, is not echoed)."""

    installation_id: UUID
    has_push_to_start_token: bool
    widgets_enabled: bool
    updated_at: datetime

    @classmethod
    def of(cls, device: ActivityDevice) -> "LiveActivityDeviceOut":
        """Build from the domain model."""
        return cls(
            installation_id=device.installation_id,
            has_push_to_start_token=device.push_to_start_token is not None,
            widgets_enabled=device.widgets_enabled,
            updated_at=device.updated_at,
        )


class LiveActivityIn(BaseModel):
    """A Live Activity the app started for a mower, with its update token."""

    installation_id: UUID
    activity_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9-]+$")
    push_token: str = Field(
        min_length=64,
        max_length=200,
        pattern=r"^[0-9a-fA-F]+$",
        description="The activity's push token (hex) the server updates it with.",
    )


class LiveActivityOut(BaseModel):
    """The stored activity (tokens are not echoed)."""

    mower_id: UUID
    installation_id: UUID
    activity_id: str | None
    state: str
    started_at: datetime

    @classmethod
    def of(cls, activity: LiveActivity) -> "LiveActivityOut":
        """Build from the domain model."""
        return cls(
            mower_id=activity.mower_id,
            installation_id=activity.installation_id,
            activity_id=activity.activity_id,
            state=activity.state.value,
            started_at=activity.started_at,
        )
