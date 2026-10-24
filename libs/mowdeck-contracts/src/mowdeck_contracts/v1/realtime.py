"""The WebSocket protocol of the Realtime service (``GET /api/v1/realtime/ws``).

One JSON object per text frame, discriminated by ``type``. The client authenticates with its
access token in the **first** message (never in the URL), then subscribes to mowers:

==================  ========================  ==================================================
Direction           ``type``                  Meaning
==================  ========================  ==================================================
client -> server    ``auth``                  first message; later again with a fresh token
client -> server    ``subscribe``             live updates of these mowers
client -> server    ``unsubscribe``           stop updates of these mowers
client -> server    ``ping``                  application level liveness check
client -> server    ``drive_start``           open a manual drive session (joystick)
client -> server    ``drive_frame``           one joystick sample of an open session
client -> server    ``drive_stop``            end a drive session (the mower stops)
server -> client    ``ready``                 the token was accepted (again)
server -> client    ``subscribed``            allowed ids are subscribed, the rest is ``denied``
server -> client    ``unsubscribed``          answer to ``unsubscribe``
server -> client    ``state``                 the newest :class:`MowerState` of a mower
server -> client    ``command_result``        a :class:`CommandResult` of a mower
server -> client    ``revoked``               access to these mowers ended; no more updates
server -> client    ``error``                 a request failed; the socket stays open
server -> client    ``pong``                  answer to ``ping``
server -> client    ``drive_started``         the drive session is open; send frames now
server -> client    ``drive_frame_ack``       a frame with ``seq`` was published to the mower
server -> client    ``drive_ended``           the drive session ended (with the reason)
server -> client    ``notification``          a notification for the principal (no subscribe)
server -> client    ``vision_proposal``       an AI assist proposal of a subscribed mower
==================  ========================  ==================================================

Close codes are listed in :class:`RealtimeCloseCode`. Manual driving is described in
:mod:`mowdeck_contracts.v1.drive` and ADR 0026: frames are rate limited, every frame becomes a
``ManualDrive`` command with the dead-man ``ttl_ms`` of ``drive_started``, and the mower stops
when frames stop (``drive_stop``, a closed socket, a lost network).
"""

from enum import IntEnum, StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, RootModel, StringConstraints

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime
from mowdeck_contracts.v1.drive import DriveEndReason
from mowdeck_contracts.v1.envelopes import CommandResult
from mowdeck_contracts.v1.mower import MowerState
from mowdeck_contracts.v1.notifications import NotificationKind, NotificationSeverity
from mowdeck_contracts.v1.principal import PrincipalKind
from mowdeck_contracts.v1.vision import VisionProposalState, VisionTask, VisionTrigger

RequestId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,64}$")]
"""Chosen by the client and echoed in the answer, so it can match answers to requests."""

MAX_MOWER_IDS_PER_MESSAGE = 1000
"""Upper bound of ``mower_ids`` in one message (services may allow fewer)."""


class RealtimeCloseCode(IntEnum):
    """WebSocket close codes used by Realtime."""

    GOING_AWAY = 1001
    """The instance shuts down; reconnect (another instance takes over)."""
    MESSAGE_TOO_BIG = 1009
    """A frame exceeded the maximum message size."""
    PROTOCOL_ERROR = 4400
    """A binary frame (only JSON text frames are allowed)."""
    UNAUTHORIZED = 4401
    """No ``auth`` in time, an invalid token, an expired token, or a token of another user."""
    LIMIT_EXCEEDED = 4429
    """Too many connections of the user or account, or too many subscriptions."""
    SLOW_CONSUMER = 4430
    """The client did not read fast enough; reconnect and subscribe again."""


# --- client -> server ---------------------------------------------------------------------------


class AuthMessage(ContractModel):
    """The access token; the first message of every connection, later to refresh it."""

    type: Literal["auth"] = "auth"
    token: str = Field(min_length=1, max_length=8192)


class SubscribeMessage(ContractModel):
    """Subscribe to mowers (each id is checked with ``can_view``)."""

    type: Literal["subscribe"] = "subscribe"
    request_id: RequestId | None = None
    mower_ids: list[UUID] = Field(min_length=1, max_length=MAX_MOWER_IDS_PER_MESSAGE)


class UnsubscribeMessage(ContractModel):
    """Stop the updates of mowers."""

    type: Literal["unsubscribe"] = "unsubscribe"
    request_id: RequestId | None = None
    mower_ids: list[UUID] = Field(min_length=1, max_length=MAX_MOWER_IDS_PER_MESSAGE)


class PingMessage(ContractModel):
    """Ask for a ``pong`` (browsers cannot see WebSocket protocol pings)."""

    type: Literal["ping"] = "ping"
    request_id: RequestId | None = None


class DriveStartMessage(ContractModel):
    """Open a manual drive session (``can_drive`` on the mower, at most one per mower)."""

    type: Literal["drive_start"] = "drive_start"
    request_id: RequestId | None = None
    mower_id: UUID
    blade_on_allowed: bool = Field(
        default=False,
        description="Allow frames with `blade_on` in this session (the mower must support it).",
    )


class DriveFrameMessage(ContractModel):
    """One joystick sample; send about ten per second while the stick is held."""

    type: Literal["drive_frame"] = "drive_frame"
    session_id: UUID
    seq: int | None = Field(
        default=None, ge=0, description="Echoed in `drive_frame_ack` when the frame went out."
    )
    linear: float = Field(ge=-1, le=1, description="Forward (+) / backward (-), normalised.")
    angular: float = Field(ge=-1, le=1, description="Turn left (+) / right (-), normalised.")
    blade_on: bool = False


class DriveStopMessage(ContractModel):
    """End a drive session; a zero frame is sent and the mower stops."""

    type: Literal["drive_stop"] = "drive_stop"
    request_id: RequestId | None = None
    session_id: UUID


ClientMessage = Annotated[
    AuthMessage
    | SubscribeMessage
    | UnsubscribeMessage
    | PingMessage
    | DriveStartMessage
    | DriveFrameMessage
    | DriveStopMessage,
    Field(discriminator="type"),
]
"""Any message a client may send."""


class RealtimeClientMessage(RootModel[ClientMessage]):
    """Parses (and documents) one client message: ``RealtimeClientMessage.model_validate_json``."""


# --- server -> client ---------------------------------------------------------------------------


class PrincipalSummary(ContractModel):
    """Who the connection acts for (from the verified token)."""

    subject: UUID
    account_id: UUID
    kind: PrincipalKind
    display_name: str | None = Field(default=None, max_length=200)


class ReadyMessage(ContractModel):
    """The token was accepted; sent after every ``auth``."""

    type: Literal["ready"] = "ready"
    connection_id: UUID
    principal: PrincipalSummary
    token_expires_at: UtcDateTime = Field(
        description="The socket closes with 4401 at this time unless a fresh `auth` arrives."
    )
    server_time: UtcDateTime


class SubscribedMessage(ContractModel):
    """Answer to ``subscribe``: ``mower_ids`` are subscribed, ``denied`` are not.

    A denied id is either unknown or not visible to the caller; both look the same.
    """

    type: Literal["subscribed"] = "subscribed"
    request_id: RequestId | None = None
    mower_ids: list[UUID] = Field(default_factory=list)
    denied: list[UUID] = Field(default_factory=list)


class UnsubscribedMessage(ContractModel):
    """Answer to ``unsubscribe``."""

    type: Literal["unsubscribed"] = "unsubscribed"
    request_id: RequestId | None = None
    mower_ids: list[UUID] = Field(default_factory=list)


class StateMessage(ContractModel):
    """The newest state of a subscribed mower (older unsent states are replaced)."""

    type: Literal["state"] = "state"
    mower_id: UUID
    state: MowerState


class CommandResultMessage(ContractModel):
    """A command result of a subscribed mower."""

    type: Literal["command_result"] = "command_result"
    mower_id: UUID
    result: CommandResult


class RevokeReason(StrEnum):
    """Why subscriptions ended without an ``unsubscribe``."""

    ACCESS_REVOKED = "access_revoked"
    """The re-check found that the principal may no longer view the mower."""


class RevokedMessage(ContractModel):
    """The subscriptions of these mowers ended; no further updates follow for them."""

    type: Literal["revoked"] = "revoked"
    mower_ids: list[UUID]
    reason: RevokeReason


class RealtimeErrorCode(StrEnum):
    """Codes of ``error`` messages."""

    INVALID_MESSAGE = "invalid_message"
    """Not JSON, an unknown ``type`` or invalid fields."""
    UNAUTHORIZED = "unauthorized"
    """Authentication failed (the socket closes with 4401 right after)."""
    LIMIT_EXCEEDED = "limit_exceeded"
    """A connection or subscription limit (the socket closes with 4429 right after)."""
    INTERNAL = "internal"
    """An unexpected server error."""
    DRIVE_NOT_ALLOWED = "drive_not_allowed"
    """No ``can_drive`` on the mower (or the mower is not visible)."""
    DRIVE_UNSUPPORTED = "drive_unsupported"
    """The mower's connection offers no manual driving (capability ``manual_drive``)."""
    DRIVE_BUSY = "drive_busy"
    """Another drive session of the mower is open."""
    DRIVE_UNAVAILABLE = "drive_unavailable"
    """Fleet could not be reached to open the session; try again."""
    DRIVE_NOT_ACTIVE = "drive_not_active"
    """A frame or stop names a session this socket does not have (any more)."""


class ErrorMessage(ContractModel):
    """A request failed."""

    type: Literal["error"] = "error"
    request_id: RequestId | None = None
    code: RealtimeErrorCode
    message: str = Field(max_length=500)


class PongMessage(ContractModel):
    """Answer to ``ping``."""

    type: Literal["pong"] = "pong"
    request_id: RequestId | None = None


class DriveCapabilities(ContractModel):
    """What the mower offers for manual driving."""

    manual_drive: bool
    manual_drive_blades: bool


class DriveStartedMessage(ContractModel):
    """Answer to ``drive_start``: the session is open."""

    type: Literal["drive_started"] = "drive_started"
    request_id: RequestId | None = None
    session_id: UUID
    mower_id: UUID
    max_rate_hz: int = Field(description="Frames above this rate are dropped.")
    ttl_ms: int = Field(description="Dead-man timeout of every frame in milliseconds.")
    max_seconds: int = Field(description="The session ends after this many seconds.")
    blade_on_allowed: bool = Field(description="Frames may switch the blades on.")
    capabilities: DriveCapabilities


class DriveFrameAckMessage(ContractModel):
    """A frame with ``seq`` was published to the mower (not yet executed)."""

    type: Literal["drive_frame_ack"] = "drive_frame_ack"
    session_id: UUID
    seq: int


class DriveEndedMessage(ContractModel):
    """A drive session ended; the mower stops (dead-man timer at the latest)."""

    type: Literal["drive_ended"] = "drive_ended"
    session_id: UUID
    mower_id: UUID
    reason: DriveEndReason
    detail: str | None = Field(default=None, max_length=500)


class NotificationScope(StrEnum):
    """Whose notification a ``notification`` message carries."""

    USER = "user"
    """An item of the person's own inbox (`id` is the inbox item id: mark it read with
    `POST /api/v1/notifications/inbox/{id}/read`)."""
    ACCOUNT = "account"
    """A notification of the account for an API token (no inbox item; rendered in English)."""


class RealtimeNotification(ContractModel):
    """A notification as the inbox API returns it (``InboxItemOut``), plus ``scope``."""

    id: UUID
    scope: NotificationScope
    mower_id: UUID | None = None
    kind: NotificationKind
    severity: NotificationSeverity
    title: str = Field(max_length=200)
    body: str = Field(max_length=1000)
    url: str | None = Field(default=None, max_length=500)
    occurred_at: UtcDateTime
    created_at: UtcDateTime
    read: bool = False
    read_at: UtcDateTime | None = None
    acked: bool = False
    acked_at: UtcDateTime | None = None


class NotificationMessage(ContractModel):
    """A new notification for the principal, without a subscription.

    People get the items of their own inbox (``scope`` ``user``, after Notifications checked
    their preferences and access). API tokens get the account's notifications (``scope``
    ``account``) when the token holds ``notifications.view`` and may view the mower (``can_drive``
    for the AI assist kinds); a notification about a mower needs a subscription to it.
    """

    type: Literal["notification"] = "notification"
    notification: RealtimeNotification


class VisionProposalEvent(StrEnum):
    """What happened to a proposal."""

    CREATED = "created"
    """A maneuver waits for approval (state ``proposed``)."""
    UPDATED = "updated"
    """The state changed (approved, rejected, expired, executed, ...)."""


class VisionProposalSummary(ContractModel):
    """An AI assist proposal without images (details: ``GET /api/v1/vision/proposals/{id}``)."""

    id: UUID
    mower_id: UUID
    task: VisionTask
    round: int = Field(ge=1, le=10)
    state: VisionProposalState
    final: bool
    action: Literal["drive", "return_to_dock", "resume"] | None = Field(
        default=None,
        description="The default option, what an approval without a choice runs (after an "
        "approval: what runs): `drive`, `return_to_dock`, or `resume` (optional drive steps, "
        "then the paused job continues); null before the analysis decided one.",
    )
    steps: int = Field(default=0, ge=0, le=6)
    fallback: bool = Field(
        default=False, description="`action` is the rule based return to dock fallback."
    )
    low_confidence: bool = Field(
        default=False,
        description="The model's recommendation is offered below the confidence threshold.",
    )
    alternative_action: Literal["drive", "return_to_dock", "resume"] | None = Field(
        default=None,
        description="The model's low-confidence recommendation next to the fallback `action`; "
        "the choice is made in MowDeck.",
    )
    confidence: float | None = Field(default=None, ge=0, le=1)
    situation: str | None = Field(default=None, max_length=64)
    trigger: VisionTrigger = VisionTrigger.MANUAL
    error_code: str | None = Field(default=None, max_length=64)
    expires_at: UtcDateTime | None = Field(
        default=None, description="Approval deadline while `proposed`; null otherwise."
    )
    link: str = Field(max_length=300, description="App path of the proposal.")
    updated_at: UtcDateTime


class VisionProposalMessage(ContractModel):
    """An AI assist proposal of a subscribed mower was created or changed its state."""

    type: Literal["vision_proposal"] = "vision_proposal"
    event: VisionProposalEvent
    mower_id: UUID
    proposal: VisionProposalSummary


ServerMessage = Annotated[
    ReadyMessage
    | SubscribedMessage
    | UnsubscribedMessage
    | StateMessage
    | CommandResultMessage
    | RevokedMessage
    | ErrorMessage
    | PongMessage
    | DriveStartedMessage
    | DriveFrameAckMessage
    | DriveEndedMessage
    | NotificationMessage
    | VisionProposalMessage,
    Field(discriminator="type"),
]
"""Any message the server sends."""


class RealtimeServerMessage(RootModel[ServerMessage]):
    """Parses (and documents) one server message: ``RealtimeServerMessage.model_validate_json``."""
