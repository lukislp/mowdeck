"""AI assist (Vision service, ADR 0033): photo requests and the proposal event.

* :class:`PhotoRequest` / :class:`PhotoReply` on
  :func:`~mowdeck_contracts.v1.subjects.photo_subject` (core NATS request, internal): the
  Device-Gateway that holds the mower answers with a still image when the adapter can take one
  (``capture_photo``; the simulator renders its surroundings from the map). The image travels
  base64 encoded and is capped at ``max_bytes``, well below the NATS payload limit.
* ``vision.proposal.created`` (:class:`VisionProposalCreated`, stream ``EVENTS``): the Vision
  service proposed a maneuver for a mower; Notifications turns it into a ``vision_proposal``
  notification for the account's users who may drive the mower. Images and the model's raw
  answer are never part of the event. ``trigger`` says whether a user asked (``manual``) or the
  mower's error started the analysis automatically (``error``, with ``error_code``; opt-in per
  user, ADR 0035); Notifications sends the automatic ones as ``vision_auto_proposal``.
* :class:`CameraSnapshotRequest` (HTTP, internal): the Device-Gateway asks the capture worker
  (``vision-capture``, ADR 0035) for one still frame of a live camera session; the answer is the
  JPEG itself.

Feature keys of the plans (``infra/entitlements/plans.yaml``): :data:`FEATURE_AI_RECOVER` for
AI assist with the user's own provider, :data:`FEATURE_AI_RECOVER_HOSTED` for the provider of
the installation. The hosted key is a sibling, not a child (``ai_recover.hosted``), because
plan keys are hierarchical: a plan listing ``ai_recover`` would grant every child key.
"""

from enum import StrEnum
from typing import Final, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime
from mowdeck_contracts.v1.camera import CameraSession
from mowdeck_contracts.v1.principal import PrincipalRef
from mowdeck_contracts.v1.relay import VisionDemand

VISION_PROPOSAL_CREATED: Final = "vision.proposal.created"
"""Event type: AI assist proposed a maneuver (payload :class:`VisionProposalCreated`)."""

FEATURE_AI_RECOVER: Final = "ai_recover"
"""Plan feature: AI assist (Ai Recover and Find Dock) with the user's own provider."""
FEATURE_AI_RECOVER_HOSTED: Final = "ai_recover_hosted"
"""Plan feature: AI assist with the vision model the installation provides."""

PHOTO_MAX_BYTES: Final = 512 * 1024
"""Default and upper size cap of a photo reply (raw bytes, before base64)."""


class VisionTask(StrEnum):
    """What AI assist is asked to do."""

    RECOVER = "recover"
    """Free a mower that reports an error or is stuck."""
    FIND_DOCK = "find_dock"
    """Bring a mower that is away from the dock close enough for a normal return to dock."""


class VisionTrigger(StrEnum):
    """Who started an analysis."""

    MANUAL = "manual"
    """A user asked for it (the app's AI assist button, or a retry)."""
    ERROR = "error"
    """The mower entered a recoverable error and a user of the account opted in to automatic
    analyses (ADR 0035). The proposal still waits for a human approval."""


class PhotoRequest(ContractModel):
    """Request on ``gateway.mower.<id>.photo``: a still image of the mower's surroundings.

    ``account_id`` must be the mower's account; the gateway refuses other accounts with
    ``not_found`` so a foreign mower never reveals itself.
    """

    mower_id: UUID
    account_id: UUID
    max_bytes: int = Field(default=PHOTO_MAX_BYTES, ge=1024, le=PHOTO_MAX_BYTES)
    demand: VisionDemand = Field(
        default=VisionDemand.ROUTINE,
        description="Why the photo is wanted: `error_analysis` (a mower with a problem: AI "
        "assist, the automatic analysis) may wake a sleeping mower through the camera relay "
        "and ignores the Vision budget; `routine` keeps every limit (ADR 0045).",
    )


class PhotoErrorCode(StrEnum):
    """Why a gateway could not deliver a photo."""

    NOT_FOUND = "not_found"
    """This gateway holds no such mower of the account."""
    NOT_CONNECTED = "not_connected"
    """The mower is known but not connected right now."""
    UNSUPPORTED = "unsupported"
    """The adapter cannot take photos."""
    TOO_LARGE = "too_large"
    """The image exceeds ``max_bytes``."""
    FAILED = "failed"
    """Taking the photo failed."""


class PhotoError(ContractModel):
    """The error of a :class:`PhotoReply`."""

    code: PhotoErrorCode
    detail: str = Field(max_length=500)


class PhotoReply(ContractModel):
    """Reply to :class:`PhotoRequest`: exactly one of ``image_base64`` or ``error``."""

    image_base64: str | None = Field(default=None, description="The image, base64 (standard).")
    media_type: Literal["image/jpeg", "image/png"] | None = None
    taken_at: UtcDateTime | None = None
    error: PhotoError | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.image_base64 is None) == (self.error is None):
            msg = "exactly one of image_base64 or error must be given"
            raise ValueError(msg)
        if self.image_base64 is not None and self.media_type is None:
            msg = "media_type is required with an image"
            raise ValueError(msg)
        return self


class VisionProposalCreated(ContractModel):
    """Payload of ``vision.proposal.created``: a maneuver waits for a user's approval."""

    proposal_id: UUID
    mower_id: UUID
    account_id: UUID
    task: VisionTask
    round: int = Field(ge=1, le=10, description="Attempt within one assist session.")
    requested_by: PrincipalRef
    situation: str = Field(max_length=64)
    confidence: float = Field(ge=0, le=1)
    steps: int = Field(
        ge=0, le=6, description="Number of proposed drive steps (0 for `return_to_dock`)."
    )
    action: Literal["drive", "return_to_dock"] = Field(
        default="drive",
        description="`drive`: the drive steps; `return_to_dock`: the normal dock command, the "
        "mower drives home on its mapped paths by itself.",
    )
    fallback: bool = Field(
        default=False,
        description="The model recommended nothing; the service offers return to dock as a "
        "rule based fallback (it applies: the mower is in a mapped area linked to the dock).",
    )
    expires_at: UtcDateTime = Field(description="Approval is refused after this time.")
    link: str = Field(
        max_length=300, description="App path of the proposal, e.g. `/mowers/<id>?assist=<id>`."
    )
    created_at: UtcDateTime
    trigger: VisionTrigger = Field(
        default=VisionTrigger.MANUAL,
        description="`manual` (a user asked) or `error` (started automatically by the error).",
    )
    error_code: str | None = Field(
        default=None,
        max_length=64,
        description="With trigger `error`: the mower's error code that started the analysis.",
    )


CAPTURE_MAX_BYTES: Final = 512 * 1024
"""Upper size cap of a captured frame (the same cap as a photo reply)."""


class CameraSnapshotRequest(ContractModel):
    """``POST /capture`` of the capture worker (internal HTTP, ADR 0035): one still frame of the
    camera ``camera_uid`` (Agora publisher uid; None: the first publisher) in ``session``.

    The session carries the secret token; neither side logs it. The reply is ``image/jpeg``.
    """

    session: CameraSession
    camera_uid: int | None = Field(default=None, ge=0)
    timeout_seconds: float = Field(default=20.0, gt=0, le=60)
    max_bytes: int = Field(default=CAPTURE_MAX_BYTES, ge=1024, le=CAPTURE_MAX_BYTES)
    max_width: int = Field(default=1280, ge=160, le=3840)
