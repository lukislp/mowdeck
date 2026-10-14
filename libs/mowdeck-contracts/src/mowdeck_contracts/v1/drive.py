"""Manual driving (joystick) over the Realtime WebSocket, recorded by Fleet (ADR 0026).

A joystick needs about ten frames per second and a dead-man switch of about one second, so the
frames bypass Fleet's HTTP API and database:

1. The client sends ``drive_start`` on its Realtime socket. Realtime asks Fleet once with a
   :class:`DriveStartRequest` on :data:`~mowdeck_contracts.v1.subjects.FLEET_DRIVE_START`
   (core NATS request/reply, internal). Fleet checks ``can_drive`` on the mower for the subject
   (404 semantics for invisible mowers), refuses a second open session of the same mower,
   records a drive session and answers with a :class:`DriveStartReply`.
2. Realtime publishes one :class:`~mowdeck_contracts.v1.CommandEnvelope` with a
   :class:`~mowdeck_contracts.v1.ManualDrive` command per client frame directly on
   ``command.mower.<id>`` (rate limited). The envelope's ``correlation_id`` is
   :func:`drive_correlation_id` of the session, so results and logs name the session.
3. ``drive_stop`` (or a closed socket, a revocation, the token expiry, the maximum duration)
   publishes a zero frame and sends a :class:`DriveStopRequest` on
   :data:`~mowdeck_contracts.v1.subjects.FLEET_DRIVE_STOP`, which closes the record.

The Device-Gateway's controller stays the only safety authority: every frame re-arms its
dead-man timer and the blade rules apply to every frame. When frames stop for any reason the
mower stops within ``ttl_ms``.
"""

from enum import StrEnum
from typing import Final, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime
from mowdeck_contracts.v1.principal import PrincipalRef

DRIVE_CORRELATION_PREFIX: Final = "drive:"
"""Prefix of the ``correlation_id`` of every drive frame (followed by the session id)."""


DriveOrigin = Literal["vision"]
"""Services allowed to drive on behalf of a subject (automated drive sessions)."""


def drive_correlation_id(session_id: UUID) -> str:
    """The ``correlation_id`` of the frames of a drive session: ``drive:<session_id>``."""
    return f"{DRIVE_CORRELATION_PREFIX}{session_id}"


def drive_session_of(correlation_id: str | None) -> UUID | None:
    """The drive session named by a ``correlation_id``, None for any other id."""
    if correlation_id is None or not correlation_id.startswith(DRIVE_CORRELATION_PREFIX):
        return None
    try:
        return UUID(correlation_id.removeprefix(DRIVE_CORRELATION_PREFIX))
    except ValueError:
        return None


class DriveEndReason(StrEnum):
    """Why a drive session ended."""

    CLIENT_STOP = "client_stop"
    """The client sent ``drive_stop``."""
    SOCKET_CLOSED = "socket_closed"
    """The WebSocket closed (client gone, network loss, server shutdown)."""
    REVOKED = "revoked"
    """The principal lost ``can_view`` or ``can_drive`` on the mower."""
    TOKEN_EXPIRED = "token_expired"  # noqa: S105 - enum label, not a secret
    """The access token expired without a refresh."""
    MAX_DURATION = "max_duration"
    """The session reached the configured maximum duration."""
    MOWER_REFUSED = "mower_refused"
    """The mower refused a frame (sensor stop, unsupported, not connected, ...)."""
    MOWER_BUSY = "mower_busy"
    """The mower runs a job; it must be paused or ended before driving manually."""
    REPLACED = "replaced"
    """The same socket started a new session for the mower."""
    EXPIRED = "expired"
    """Fleet closed a session that was never stopped (for example after a crash)."""
    COMPLETED = "completed"
    """An automated session (``origin`` set) finished its planned steps."""
    ABORTED = "aborted"
    """An automated session was stopped by its safety watch (state, geofence, emergency stop,
    cancellation)."""


class DriveErrorCode(StrEnum):
    """Why Fleet refused to start a drive session."""

    NOT_FOUND = "not_found"
    """The mower does not exist in the account, or the subject cannot see it."""
    FORBIDDEN = "forbidden"
    """The subject sees the mower but lacks ``can_drive``."""
    BUSY = "busy"
    """Another drive session of the mower is open."""
    INVALID = "invalid"
    """The request is not acceptable (for example a service principal)."""


class DriveError(ContractModel):
    """The error of a :class:`DriveStartReply`."""

    code: DriveErrorCode
    detail: str = Field(max_length=500)


class DriveStartRequest(ContractModel):
    """Request on ``fleet.drive.start``: open a drive session on behalf of a subject.

    ``account_id`` is the account of the subject's token; it must own the mower (otherwise the
    answer is ``not_found``). Service principals hold no object rights and are refused.
    """

    on_behalf_of: PrincipalRef
    account_id: UUID
    mower_id: UUID
    blade_on_allowed: bool = Field(
        default=False, description="The client asked to run the blades during this session."
    )
    max_seconds: int = Field(ge=1, le=3600, description="Longest duration of the session.")
    connection_id: UUID | None = Field(
        default=None, description="The Realtime connection that drives (for the audit)."
    )
    origin: DriveOrigin | None = Field(
        default=None,
        description="Service that drives on behalf of the subject (e.g. `vision`, ADR 0033); "
        "None for the joystick over Realtime.",
    )


class DriveSessionStarted(ContractModel):
    """A drive session Fleet recorded."""

    session_id: UUID
    mower_id: UUID
    account_id: UUID = Field(description="The mower's account (the envelopes' account_id).")
    started_at: UtcDateTime
    expires_at: UtcDateTime = Field(description="started_at plus max_seconds.")


class DriveStartReply(ContractModel):
    """Reply to :class:`DriveStartRequest`: exactly one of ``started`` or ``error``."""

    started: DriveSessionStarted | None = None
    error: DriveError | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.started is None) == (self.error is None):
            msg = "exactly one of started or error must be given"
            raise ValueError(msg)
        return self


class DriveStopRequest(ContractModel):
    """Request on ``fleet.drive.stop``: close the record of a drive session."""

    session_id: UUID
    account_id: UUID
    mower_id: UUID
    reason: DriveEndReason
    ended_at: UtcDateTime
    frames: int = Field(ge=0, description="Frames published to the mower.")
    frames_dropped: int = Field(ge=0, description="Frames dropped by the rate limit.")


class DriveStopReply(ContractModel):
    """Reply to :class:`DriveStopRequest`; ``found`` is False for an unknown or closed session."""

    found: bool


class DriveSessionView(ContractModel):
    """A drive session as the audit API shows it (``GET /mowers/{id}/drive-sessions``)."""

    session_id: UUID
    mower_id: UUID
    started_by: PrincipalRef
    started_at: UtcDateTime
    expires_at: UtcDateTime
    blade_on_allowed: bool
    ended_at: UtcDateTime | None = None
    end_reason: DriveEndReason | None = None
    frames: int | None = Field(default=None, ge=0)
    frames_dropped: int | None = Field(default=None, ge=0)
    origin: str | None = Field(
        default=None,
        max_length=32,
        description="Service that drove on behalf of `started_by` (e.g. `vision`); None for the "
        "joystick.",
    )
