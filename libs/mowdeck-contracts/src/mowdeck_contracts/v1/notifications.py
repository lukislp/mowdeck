"""Notifications: the event of the Notifications service and the Web Push payload.

The Notifications service derives notifications from the mower states and the connection
events (rules in ``services/notifications``), keeps each user's inbox and delivers them over
channels such as Web Push and ntfy. For every notification it creates for a user it publishes
``notifications.sent`` on ``event.notifications.sent`` (stream ``EVENTS``) with a
:class:`NotificationSent` payload, so an app can show an in-app feed.

Inbox items belong to people (``user`` and ``subuser`` principals). An API token (Home
Assistant) has no inbox; for it the service publishes every notification it derives once per
account as ``notifications.raised`` (:class:`NotificationRaised`, rendered in English) before the
per-user fan-out, whether or not a user subscribed to it. Realtime forwards it to API-token
sockets of the account that hold :data:`NOTIFICATIONS_VIEW_PERMISSION`.

:class:`WebPushPayload` is the JSON a browser's service worker receives in the ``push`` event
(encrypted with ``aes128gcm``); its size stays well below the 4 KiB Web Push limit.
"""

from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from pydantic import Field

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime

NOTIFICATION_SENT: Final = "notifications.sent"
"""Event type: a notification was created for one user (payload :class:`NotificationSent`)."""
NOTIFICATION_RAISED: Final = "notifications.raised"
"""Event type: a notification was derived for an account, before the per-user fan-out
(payload :class:`NotificationRaised`)."""
NOTIFICATIONS_VIEW_PERMISSION: Final = "notifications.view"
"""Account permission key: receive the account's notifications (``notifications.raised``) over
the Realtime socket. Meant for API tokens; implied by ``mower.view`` on the whole account."""


class NotificationKind(StrEnum):
    """What a notification is about."""

    MOWING_FINISHED = "mowing_finished"
    """A mowing job ended at the dock."""
    MOWER_STUCK = "mower_stuck"
    """The mower reports an error or has not moved while mowing."""
    BATTERY_LOW = "battery_low"
    """The battery fell below the threshold (once per discharge)."""
    MOWER_OFFLINE = "mower_offline"
    """No state for a while, or the Device-Gateway lost the link."""
    MOWER_ONLINE = "mower_online"
    """The mower reports again after an offline notification."""
    JOB_STARTED = "job_started"
    """A mowing job started (off unless a user enables it)."""
    MOWER_LIFTED = "mower_lifted"
    """Theft alarm: the lift or tilt sensor stopped the mower."""
    MOWER_LEFT_DOCK_UNEXPECTEDLY = "mower_left_dock_unexpectedly"
    """Theft alarm: the mower left the dock although nobody sent it a command."""
    GEOFENCE_EXIT = "geofence_exit"
    """Theft alarm: the position is outside the mapped areas plus a margin."""
    BLADE_MAINTENANCE = "blade_maintenance"
    """The blade hours since the last blade change crossed the threshold."""
    VISION_PROPOSAL = "vision_proposal"
    """AI assist proposed a maneuver for a stuck mower; it waits for a user's approval."""
    VISION_AUTO_PROPOSAL = "vision_auto_proposal"
    """An automatic analysis (started by a recoverable error) proposed a maneuver; it waits for
    a user's approval like any other proposal."""
    FIRMWARE_UPDATE_AVAILABLE = "firmware_update_available"
    """The manufacturer offers a newer firmware for the mower (once per version, ADR 0038)."""
    FIRMWARE_UPDATE_FINISHED = "firmware_update_finished"
    """A firmware update ended with the new version installed."""
    FIRMWARE_UPDATE_FAILED = "firmware_update_failed"
    """A firmware update failed; the mower keeps its old version."""
    TRACKING_STARTED = "tracking_started"
    """Theft tracking started (by an alarm or by hand, ADR 0058)."""
    TRACKING_MOVED = "tracking_moved"
    """The tracked mower moved a significant distance (at most once per five minutes)."""
    TRACKING_STALE = "tracking_stale"
    """The tracked mower has not reported for a while; its last position stays on the map."""
    TRACKING_ENDED = "tracking_ended"
    """Theft tracking ended (false alarm, stopped, or the maximum duration)."""
    SECURITY_NEW_LOGIN = "security_new_login"
    """A login from a device the user never used before (ADR 0065); opens the security page."""
    TEST = "test"
    """A test notification requested by the user."""


class NotificationSeverity(StrEnum):
    """How urgent a notification is; channels map it to TTL, urgency or priority."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class NotificationSent(ContractModel):
    """Payload of ``notifications.sent``: one notification for one user, already rendered."""

    notification_id: UUID
    user_id: UUID
    account_id: UUID
    mower_id: UUID | None = None
    kind: NotificationKind
    severity: NotificationSeverity
    title: str = Field(max_length=200)
    body: str = Field(max_length=1000)
    url: str | None = Field(default=None, max_length=500)
    occurred_at: UtcDateTime
    channels: list[str] = Field(
        default_factory=list, description="Channels a delivery was queued for (may be empty)."
    )
    created_at: UtcDateTime | None = Field(
        default=None,
        description="When the inbox item was stored; null in events from before the field.",
    )


class NotificationRaised(ContractModel):
    """Payload of ``notifications.raised``: one notification derived for an account.

    Published once per derived notification (mower rules, theft alarms, AI assist, blade
    maintenance, firmware), independent of user preferences, quiet hours and channels, and
    rendered in English. ``notification_id`` is stable for the occurrence (derived from the
    account and the ``dedupe_key``), so a redelivered event carries the same id.
    """

    notification_id: UUID
    account_id: UUID
    mower_id: UUID | None = None
    kind: NotificationKind
    severity: NotificationSeverity
    title: str = Field(max_length=200)
    body: str = Field(max_length=1000)
    url: str | None = Field(default=None, max_length=500)
    occurred_at: UtcDateTime
    created_at: UtcDateTime
    dedupe_key: str = Field(max_length=200, description="Identifies the occurrence.")
    required_relation: Literal["can_view", "can_drive"] = Field(
        default="can_view",
        description="Relation a receiver needs on the mower: `can_drive` for the AI assist "
        "kinds (only those who may approve a maneuver hear about it), else `can_view`.",
    )


class WebPushPayload(ContractModel):
    """The decrypted body of a Web Push message for the app's service worker."""

    title: str = Field(max_length=200)
    body: str = Field(max_length=1000)
    url: str | None = Field(default=None, max_length=500, description="Page to open on click.")
    tag: str = Field(
        max_length=200, description="Replaces an earlier notification with the same tag."
    )
    kind: NotificationKind
    mower_id: UUID | None = None
    notification_id: UUID | None = None
