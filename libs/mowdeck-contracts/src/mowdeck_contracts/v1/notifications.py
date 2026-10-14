"""Notifications: the event of the Notifications service and the Web Push payload.

The Notifications service derives notifications from the mower states and the connection
events (rules in ``services/notifications``), keeps each user's inbox and delivers them over
channels such as Web Push and ntfy. For every notification it creates for a user it publishes
``notifications.sent`` on ``event.notifications.sent`` (stream ``EVENTS``) with a
:class:`NotificationSent` payload, so an app can show an in-app feed.

:class:`WebPushPayload` is the JSON a browser's service worker receives in the ``push`` event
(encrypted with ``aes128gcm``); its size stays well below the 4 KiB Web Push limit.
"""

from enum import StrEnum
from typing import Final
from uuid import UUID

from pydantic import Field

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime

NOTIFICATION_SENT: Final = "notifications.sent"
"""Event type: a notification was created for one user (payload :class:`NotificationSent`)."""


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
