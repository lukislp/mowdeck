"""Theft tracking mode (ADR 0058): the lease the Device-Gateway takes and the tracking events.

Telemetry owns the tracking sessions. While one is active it asks the Device-Gateway that holds
the mower, every few seconds, to report fresh positions at the session's interval
(:class:`TrackingLeaseRequest` on ``gateway.mower.<id>.tracking``, core NATS request/reply). The
request is a *lease*: the gateway keeps the fast reports only for ``lease_seconds`` and falls
back to its normal rhythm when no renewal arrives, so a crashed Telemetry can never leave a
mower polling the vendor cloud for ever. ``lease_seconds = 0`` releases at once.

Telemetry publishes the session's milestones as events (stream ``EVENTS``,
``event.tracking.<name>``, payload :class:`TrackingEvent`); the Notifications service turns them
into notifications.
"""

from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from pydantic import Field

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime

TRACKING_STARTED: Final = "tracking.started"
"""Event type: a tracking session started (payload :class:`TrackingEvent`)."""
TRACKING_ENDED: Final = "tracking.ended"
"""Event type: a tracking session ended (false alarm, stopped, maximum duration)."""
TRACKING_STALE: Final = "tracking.stale"
"""Event type: the tracked mower has not reported for a while (offline or out of coverage)."""
TRACKING_MOVED: Final = "tracking.moved"
"""Event type: the tracked mower moved a significant distance (rate limited)."""

TrackingTrigger = Literal["mower_lifted", "mower_left_dock_unexpectedly", "geofence_exit", "manual"]
"""What started a session: one of the theft alarms or a person."""
TrackingEndReason = Literal["false_alarm", "stopped", "max_duration"]
"""Why a session ended."""


class TrackingEvent(ContractModel):
    """Payload of ``tracking.started``, ``.ended``, ``.stale`` and ``.moved``."""

    session_id: UUID
    mower_id: UUID
    account_id: UUID
    at: UtcDateTime = Field(description="When the event happened.")
    trigger: TrackingTrigger = Field(description="What started the session.")
    end_reason: TrackingEndReason | None = Field(
        default=None, description="Only in `tracking.ended`."
    )
    lat: float | None = Field(default=None, ge=-90, le=90, description="Last known position.")
    lon: float | None = Field(default=None, ge=-180, le=180)
    distance_m: float | None = Field(
        default=None, ge=0, description="`tracking.moved`: metres moved since the last notice."
    )
    stale_minutes: int | None = Field(
        default=None, ge=0, description="`tracking.stale`: minutes without a report."
    )
    track_points: int | None = Field(
        default=None, ge=0, description="`tracking.ended`: positions recorded in the session."
    )


class TrackingLeaseCode(StrEnum):
    """Why a gateway did not take a lease."""

    OK = "ok"
    NOT_FOUND = "not_found"
    """This gateway holds no such mower of the account."""
    NOT_CONNECTED = "not_connected"
    """The mower is known but not connected right now (the lease is kept for when it is)."""


class TrackingLeaseRequest(ContractModel):
    """Request on ``gateway.mower.<id>.tracking`` (internal, Telemetry to Device-Gateway)."""

    mower_id: UUID
    account_id: UUID = Field(description="The mower's account as Telemetry knows it.")
    interval_seconds: float = Field(
        default=10.0, ge=1, le=3600, description="Ask the mower for a report this often."
    )
    lease_seconds: float = Field(
        default=60.0,
        ge=0,
        le=3600,
        description="Keep the fast reports this long unless renewed; 0 releases the lease.",
    )


class TrackingLeaseReply(ContractModel):
    """Reply to :class:`TrackingLeaseRequest`."""

    code: TrackingLeaseCode
    interval_seconds: float | None = Field(
        default=None, description="The interval the adapter applies (it may clamp the request)."
    )
    detail: str | None = Field(default=None, max_length=500)
