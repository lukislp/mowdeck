"""Camera relay (ADR 0045): leases on one shared video session per account and mower.

The camera relay holds at most one vendor camera session per ``(account_id, mower_id)`` and
serves it to any number of viewers of that account over WebRTC (WHEP). Fleet stays the front
door: it checks ``can_view`` and the plan feature ``camera``, writes the audit row and then asks
the relay for a lease (core NATS request/reply, queue group ``camera-relay``):

==========================  =======================  =====================
``relay.camera.attach``     :class:`RelayAttachRequest`  :class:`RelayAttachReply`
``relay.camera.renew``      :class:`RelayRenewRequest`   :class:`RelayRenewReply`
``relay.camera.detach``     :class:`RelayDetachRequest`  :class:`RelayDetachReply`
==========================  =======================  =====================

A lease counts as presence only while its media is confirmed and its heartbeat arrives
(``relay.camera.renew`` every :data:`RELAY_HEARTBEAT_SECONDS`); a lease without a heartbeat for
:data:`RELAY_LEASE_TTL_SECONDS` is closed. When the last lease closes the session drains for
:data:`RELAY_GRACE_SECONDS`, then the relay leaves the vendor channel and asks the gateway to
stop the stream.

The relay publishes its lifecycle as events (wrapped in ``EventEnvelope``) on the stream
``CAMERA``, subjects ``camera.mower.<id>.relay.<kind>``. Nothing here names a manufacturer: the
source of a session is a :class:`~mowdeck_contracts.v1.camera.CameraProvider`.
"""

from enum import StrEnum
from typing import Annotated, Final, Self
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime
from mowdeck_contracts.v1.camera import (
    CameraProvider,
    CameraSession,
)
from mowdeck_contracts.v1.principal import PrincipalRef

CameraId = Annotated[str, StringConstraints(min_length=1, max_length=64)]
"""A ``CameraInfo.id``."""

# --- Defaults (ADR 0045) ----------------------------------------------------------------------

RELAY_GRACE_SECONDS: Final = 15
"""How long a session stays up after its last lease closed (switching camera and map)."""
RELAY_LEASE_TTL_SECONDS: Final = 45
"""A lease without a heartbeat for this long is closed."""
RELAY_TICKET_SECONDS: Final = 60
"""Lifetime of a relay ticket (the WHEP bearer token)."""
VISION_SNAPSHOT_HOLD_SECONDS: Final = 30
"""A single frame for Vision holds the session at most this long."""
VISION_STREAM_LEASE_SECONDS: Final = 300
"""A Vision stream lease lasts at most this long; pulling frames renews it."""
VISION_DAILY_MINUTES: Final = 20
"""Vision-only streaming per mower and UTC day."""
VISION_MIN_QUOTA_PERCENT: Final = 10
"""Vision-only streaming is refused below this share of the vendor's video budget."""
VISION_MIN_BATTERY_PERCENT: Final = 15
"""Vision-only streaming is refused below this battery level unless the mower charges."""

# --- Event types ------------------------------------------------------------------------------

CAMERA_RELAY_STARTED: Final = "camera.relay.started"
CAMERA_RELAY_STOPPED: Final = "camera.relay.stopped"
CAMERA_RELAY_LEASE_OPENED: Final = "camera.relay.lease_opened"
CAMERA_RELAY_LEASE_CLOSED: Final = "camera.relay.lease_closed"
CAMERA_RELAY_EVENT_TYPES: Final = (
    CAMERA_RELAY_STARTED,
    CAMERA_RELAY_STOPPED,
    CAMERA_RELAY_LEASE_OPENED,
    CAMERA_RELAY_LEASE_CLOSED,
)
"""``EventEnvelope.type`` of the relay events; the last token is the subject kind."""


class RelayPurpose(StrEnum):
    """Why a lease holds the session."""

    VIEWER = "viewer"
    """A person watches (browser or app). Counts only with confirmed media and a heartbeat."""
    VISION = "vision"
    """Vision pulls frames. Bound by the vision limits; never wakes an offline mower."""


class RelayErrorCode(StrEnum):
    """Why the relay gave no lease."""

    UNSUPPORTED_PROVIDER = "unsupported_provider"
    """No source driver for the mower's camera provider (e.g. the bridge is not installed).
    The only code on which a client in ``auto`` falls back to the direct path."""
    UNAVAILABLE = "unavailable"
    """The mower is offline or asleep, the vendor or the source failed, or the relay is full."""
    BUDGET = "budget"
    """A limit refused it: the daily Vision minutes, the video budget or the battery."""
    FORBIDDEN = "forbidden"
    """The lease or session belongs to another account, or the lease to another principal
    (strict leases: only the principal that opened a lease renews or detaches it)."""
    NOT_FOUND = "not_found"
    """No such lease (expired or detached) or no such mower."""


class RelayError(ContractModel):
    """The error of a relay reply."""

    code: RelayErrorCode
    detail: str = Field(max_length=500)


class RelayAttachRequest(ContractModel):
    """Request on ``relay.camera.attach`` (internal, Fleet or Vision to camera-relay).

    The sender has already authorized the principal; the relay keys the session by
    ``(account_id, mower_id)`` and never shares it across accounts.
    """

    account_id: UUID = Field(description="The mower's account as Fleet knows it.")
    mower_id: UUID
    camera_ids: list[CameraId] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "`CameraInfo.id` of each wanted camera; one tile per camera. Empty: every camera "
            "the mower offers (the sender does not know the camera list, e.g. Fleet)."
        ),
    )
    requested_by: PrincipalRef
    purpose: RelayPurpose = RelayPurpose.VIEWER

    @model_validator(mode="after")
    def _unique_camera_ids(self) -> Self:
        if len(self.camera_ids) != len(set(self.camera_ids)):
            msg = "camera ids must be unique"
            raise ValueError(msg)
        return self


class _SessionReply(ContractModel):
    """Exactly one of ``session`` (provider ``relay``) or ``error``."""

    session: CameraSession | None = None
    error: RelayError | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.session is None) == (self.error is None):
            msg = "exactly one of session or error must be given"
            raise ValueError(msg)
        if self.session is not None and self.session.provider is not CameraProvider.RELAY:
            msg = "a relay reply carries a session with provider relay"
            raise ValueError(msg)
        return self


class RelayAttachReply(_SessionReply):
    """Reply to :class:`RelayAttachRequest`: a ``relay`` camera session with a new lease, or an
    error. Fleet hands the session to the client unchanged (``Cache-Control: no-store``)."""


class RelayRenewRequest(ContractModel):
    """Request on ``relay.camera.renew``: the heartbeat of one lease.

    It extends the lease by :data:`RELAY_LEASE_TTL_SECONDS` and issues a fresh ticket for
    reconnects. A lease of another account or of another principal than ``requested_by`` is
    ``forbidden``, an unknown one ``not_found``.
    """

    account_id: UUID
    mower_id: UUID
    lease_id: UUID
    requested_by: PrincipalRef


class RelayRenewReply(_SessionReply):
    """Reply to :class:`RelayRenewRequest`: the same lease with a fresh ticket, or an error."""


class RelayDetachRequest(ContractModel):
    """Request on ``relay.camera.detach``: the client is gone (page left, tab hidden). Only the
    principal that opened the lease ends it; another principal's lease is ``forbidden``."""

    account_id: UUID
    mower_id: UUID
    lease_id: UUID
    requested_by: PrincipalRef


class RelayDetachReply(ContractModel):
    """Reply to :class:`RelayDetachRequest`. Idempotent: ``detached`` is false when the lease
    was already closed; ``error`` only for ``forbidden`` (another account's or another
    principal's lease, which stays open)."""

    detached: bool
    error: RelayError | None = None

    @model_validator(mode="after")
    def _error_means_not_detached(self) -> Self:
        if self.error is not None and self.detached:
            msg = "a detach with an error cannot have detached the lease"
            raise ValueError(msg)
        return self


# --- Events (stream CAMERA) -------------------------------------------------------------------


class RelayStopReason(StrEnum):
    """Why a relay session ended."""

    IDLE = "idle"
    """The grace period passed without a lease."""
    FAILED = "failed"
    """The source failed and gave up after its retries."""
    BUDGET = "budget"
    """A limit ended it (Vision minutes, video budget, battery)."""
    MOWER_OFFLINE = "mower_offline"
    """The mower went offline or to sleep."""
    SHUTDOWN = "shutdown"
    """The relay instance stopped."""


class RelayLeaseCloseReason(StrEnum):
    """Why a lease ended."""

    DETACHED = "detached"
    """The client detached (page left, tab hidden, app closed)."""
    EXPIRED = "expired"
    """No heartbeat within the lease TTL, or the Vision lease reached its cap."""
    MEDIA_LOST = "media_lost"
    """The viewer's WebRTC connection ended or stopped receiving media."""
    SESSION_STOPPED = "session_stopped"
    """The session ended under the lease (failure, budget, offline, shutdown)."""


class CameraRelayStarted(ContractModel):
    """Payload of ``camera.relay.started`` on ``camera.mower.<id>.relay.started``: the relay
    joined the source and has media."""

    relay_session_id: UUID
    account_id: UUID
    mower_id: UUID
    source: CameraProvider = Field(description="The provider of the source (not `relay`).")
    camera_ids: list[CameraId] = Field(max_length=8, description="The cameras that publish.")
    started_at: UtcDateTime
    trigger: RelayPurpose | None = Field(
        default=None, description="Purpose of the lease that started the session."
    )
    started_by: PrincipalRef | None = Field(
        default=None, description="Who opened the lease that started the session."
    )

    @model_validator(mode="after")
    def _source_is_not_the_relay(self) -> Self:
        if self.source is CameraProvider.RELAY:
            msg = "the source of a relay session is a vendor or test provider, not relay"
            raise ValueError(msg)
        return self


class CameraRelayStopped(ContractModel):
    """Payload of ``camera.relay.stopped`` on ``camera.mower.<id>.relay.stopped``: the relay
    left the source and asked the gateway to stop the stream."""

    relay_session_id: UUID
    account_id: UUID
    mower_id: UUID
    reason: RelayStopReason
    started_at: UtcDateTime | None = Field(
        default=None, description="When media began; null when the session never went live."
    )
    stopped_at: UtcDateTime
    vision_seconds: float = Field(
        default=0, ge=0, description="Seconds held by Vision leases alone (daily limit)."
    )
    trigger: RelayPurpose | None = Field(
        default=None, description="Purpose of the lease that started the session."
    )
    peak_viewers: int = Field(
        default=0, ge=0, description="Most viewer leases open at the same time."
    )


class CameraRelayLeaseOpened(ContractModel):
    """Payload of ``camera.relay.lease_opened`` on ``camera.mower.<id>.relay.lease_opened``."""

    lease_id: UUID
    relay_session_id: UUID
    account_id: UUID
    mower_id: UUID
    purpose: RelayPurpose
    requested_by: PrincipalRef
    camera_ids: list[CameraId] = Field(min_length=1, max_length=8)
    opened_at: UtcDateTime


class CameraRelayLeaseClosed(ContractModel):
    """Payload of ``camera.relay.lease_closed`` on ``camera.mower.<id>.relay.lease_closed``."""

    lease_id: UUID
    relay_session_id: UUID
    account_id: UUID
    mower_id: UUID
    purpose: RelayPurpose
    reason: RelayLeaseCloseReason
    opened_at: UtcDateTime
    closed_at: UtcDateTime
