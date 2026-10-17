"""Effects: what the domain asks the outside world to do, as data.

The application layer carries them out in order: the source port for the vendor session, the
ticket signer, the media plane and the event publisher. The source port is reached through the
Device-Gateway in a later step; nothing here names a manufacturer.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from mowdeck_contracts.v1.relay import (
    CameraRelayLeaseClosed,
    CameraRelayLeaseOpened,
    CameraRelayStarted,
    CameraRelayStopped,
)
from mowdeck_contracts.v1.subjects import CameraRelayEventKind

from camera_relay.domain.models import RelayKey
from camera_relay.domain.tickets import TicketClaims


@dataclass(frozen=True, slots=True)
class StartVendorSession:
    """Start the source of ``key`` with these cameras; media goes to ``r/<relay_id>/...``."""

    key: RelayKey
    session_id: UUID
    relay_id: str
    camera_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UpdateCameras:
    """The active camera set (union of the open leases' cameras) changed."""

    key: RelayKey
    session_id: UUID
    relay_id: str
    camera_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StopVendorSession:
    """Leave the source; with ``release`` the vendor session is released as well, so the mower
    stops publishing (it does not stop by itself)."""

    key: RelayKey
    session_id: UUID
    relay_id: str
    release: bool = True


@dataclass(frozen=True, slots=True)
class IssueTicket:
    """Sign a ticket with this scope for the reply."""

    claims: TicketClaims


@dataclass(frozen=True, slots=True)
class KickViewer:
    """End the media plane's reader session of a closed viewer lease."""

    key: RelayKey
    lease_id: UUID
    whep_session: str


RelayEventPayload = (
    CameraRelayStarted | CameraRelayStopped | CameraRelayLeaseOpened | CameraRelayLeaseClosed
)


@dataclass(frozen=True, slots=True)
class EmitEvent:
    """Publish a lifecycle event on ``camera.mower.<id>.relay.<kind>`` (stream ``CAMERA``)."""

    key: RelayKey
    kind: CameraRelayEventKind
    event_type: str
    payload: RelayEventPayload
    occurred_at: datetime


Effect = (
    StartVendorSession | UpdateCameras | StopVendorSession | IssueTicket | KickViewer | EmitEvent
)
