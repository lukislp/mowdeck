"""AI assist (Vision service, ADR 0033): photo requests and the proposal event.

* :class:`PhotoRequest` / :class:`PhotoReply` on
  :func:`~mowdeck_contracts.v1.subjects.photo_subject` (core NATS request, internal): the
  Device-Gateway that holds the mower answers with a still image when the adapter can take one
  (``capture_photo``; the simulator renders its surroundings from the map). The image travels
  base64 encoded and is capped at ``max_bytes``, well below the NATS payload limit. With
  ``max_frames`` 2 the reply carries ``frames`` instead: one frame of each camera of the same
  moment (both front cameras through the camera relay, left first, each with its side and
  capture time), all together within ``max_bytes``; a request without ``max_frames`` gets the
  single image as before.
* ``vision.proposal.created`` (:class:`VisionProposalCreated`, stream ``EVENTS``): the Vision
  service proposed a maneuver for a mower; Notifications turns it into a ``vision_proposal``
  notification for the account's users who may drive the mower. Images and the model's raw
  answer are never part of the event. ``trigger`` says whether a user asked (``manual``) or the
  mower's error started the analysis automatically (``error``, with ``error_code``; opt-in per
  user, ADR 0035); Notifications sends the automatic ones as ``vision_auto_proposal``.
* :class:`CameraSnapshotRequest` (deprecated, no producer or consumer): the request of the
  capture worker (``vision-capture``, ADR 0035), removed with the camera relay's frame tap
  (ADR 0045). It stays in ``v1`` because ``v1`` removes nothing (versioning rules).

Feature keys of the plans (``infra/entitlements/plans.yaml``): :data:`FEATURE_AI_RECOVER` for
AI assist with the user's own provider, :data:`FEATURE_AI_RECOVER_HOSTED` for the provider of
the installation. The hosted key is a sibling, not a child (``ai_recover.hosted``), because
plan keys are hierarchical: a plan listing ``ai_recover`` would grant every child key.
"""

from enum import StrEnum
from typing import Final, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime
from mowdeck_contracts.v1.camera import CameraSession
from mowdeck_contracts.v1.principal import PrincipalRef
from mowdeck_contracts.v1.relay import (
    VISION_FRAME_SET_MAX_FRAMES,
    CameraId,
    CameraSide,
    VisionDemand,
)

VISION_PROPOSAL_CREATED: Final = "vision.proposal.created"
"""Event type: AI assist proposed a maneuver (payload :class:`VisionProposalCreated`)."""
VISION_PROPOSAL_UPDATED: Final = "vision.proposal.updated"
"""Event type: the state of a proposal changed (payload :class:`VisionProposalUpdated`); not
for the change to ``proposed``, which is ``vision.proposal.created``."""

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


class VisionPhotoProblem(StrEnum):
    """Why an analysis got no camera photo from the mower (the most likely cause, judged from
    the mower's state at the time and the gateway's answer)."""

    WEAK_WIFI = "weak_wifi"
    """The mower is on Wi-Fi with a weak signal (``signal_dbm``): the camera stream did not
    get through."""
    WEAK_MOBILE = "weak_mobile"
    """The mower is on the mobile network with a weak signal (``signal_dbm``)."""
    OFFLINE = "offline"
    """The mower is offline or its gateway holds no connection to it."""
    NO_FRAME = "no_frame"
    """The camera delivered no frame in time although the connection looked fine."""


class VisionProposalState(StrEnum):
    """Lifecycle of an AI assist proposal (one round)."""

    ANALYSING = "analysing"
    """The scene is with the vision model."""
    PROPOSED = "proposed"
    """A maneuver waits for approval until ``expires_at``."""
    REJECTED_BY_AI = "rejected_by_ai"
    """Risk flags, low confidence or no safe maneuver: nothing will be executed."""
    FAILED = "failed"
    """The analysis or the execution failed for a technical reason."""
    REJECTED = "rejected"
    """A user rejected the proposal."""
    EXPIRED = "expired"
    """Nobody approved it in time."""
    EXECUTING = "executing"
    """Approved: the steps are being driven."""
    VERIFYING = "verifying"
    """The steps were driven; the outcome is being checked."""
    RECOVERED = "recovered"
    """The mower is free (recover) or on its way to the dock (find_dock)."""
    NOT_RECOVERED = "not_recovered"
    """The steps were driven but did not help; another round may be requested."""
    ABORTED = "aborted"
    """Execution stopped early (emergency stop, sensor stop, geofence, no state, cancelled)."""


VISION_FINAL_STATES: Final = frozenset(
    {
        VisionProposalState.REJECTED_BY_AI,
        VisionProposalState.FAILED,
        VisionProposalState.REJECTED,
        VisionProposalState.EXPIRED,
        VisionProposalState.RECOVERED,
        VisionProposalState.NOT_RECOVERED,
        VisionProposalState.ABORTED,
    }
)
"""Proposal states after which nothing changes any more."""


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
    max_frames: int = Field(
        default=1,
        ge=1,
        le=VISION_FRAME_SET_MAX_FRAMES,
        description="Up to this many frames of the same moment, one per camera (2: both front "
        "cameras as a stereo pair); `max_bytes` caps them together. 1: the single `image_base64`.",
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


class PhotoFrame(ContractModel):
    """One camera's frame of a multi-frame :class:`PhotoReply`."""

    image_base64: str = Field(min_length=1, description="The image, base64 (standard).")
    media_type: Literal["image/jpeg", "image/png"]
    captured_at: UtcDateTime | None = Field(
        default=None, description="When the camera took it, as well as known."
    )
    camera_id: CameraId | None = None
    camera_name: str | None = Field(default=None, max_length=100)
    side: CameraSide | None = Field(
        default=None, description="`left` or `right` for a front camera of a stereo pair."
    )


class PhotoReply(ContractModel):
    """Reply to :class:`PhotoRequest`: exactly one of ``image_base64``, ``frames`` or
    ``error``. ``frames`` only answers a request with ``max_frames`` above 1 (a source with a
    single camera still answers that way, with one frame)."""

    image_base64: str | None = Field(default=None, description="The image, base64 (standard).")
    media_type: Literal["image/jpeg", "image/png"] | None = None
    taken_at: UtcDateTime | None = None
    frames: list[PhotoFrame] = Field(
        default_factory=list,
        max_length=VISION_FRAME_SET_MAX_FRAMES,
        description="Frames of the same moment, left before right (`max_frames` above 1).",
    )
    frames_missing: list[CameraId] = Field(
        default_factory=list,
        max_length=8,
        description="Cameras of the set that gave no frame (silent, too late): the frames are "
        "served without them.",
    )
    error: PhotoError | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        given = sum((self.image_base64 is not None, bool(self.frames), self.error is not None))
        if given != 1:
            msg = "exactly one of image_base64, frames or error must be given"
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
        ge=0,
        le=6,
        description="Number of proposed drive steps (0 for `return_to_dock`; `resume` may "
        "have some before the job continues).",
    )
    action: Literal["drive", "return_to_dock", "resume"] = Field(
        default="drive",
        description="The default option, what an approval without a choice executes. "
        "`drive`: the drive steps; `return_to_dock`: the normal dock command, the mower drives "
        "home on its mapped paths by itself; `resume`: the optional drive steps, then the "
        "paused mowing job continues (`resume_mow`). With `alternative_action` set this is the "
        "rule based fallback and the model's low-confidence recommendation is the alternative.",
    )
    fallback: bool = Field(
        default=False,
        description="`action` is the rule based return to dock fallback, not the model's "
        "recommendation (it applies: the mower is in a mapped area linked to the dock).",
    )
    low_confidence: bool = Field(
        default=False,
        description="The model's recommendation is offered although its confidence is below "
        "the threshold: as `action`, or as `alternative_action` next to the fallback.",
    )
    alternative_action: Literal["drive", "return_to_dock", "resume"] | None = Field(
        default=None,
        description="The model's low-confidence recommendation offered next to the fallback "
        "`action`; the user picks one in MowDeck. Null: only `action` is offered.",
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


class VisionProposalUpdated(ContractModel):
    """Payload of ``vision.proposal.updated``: a proposal changed its state.

    Published after every state change except the one to ``proposed`` (that is
    ``vision.proposal.created``). A summary only: the images and the model's full answer stay
    behind ``GET /api/v1/vision/proposals/{id}``.
    """

    proposal_id: UUID
    mower_id: UUID
    account_id: UUID
    task: VisionTask
    round: int = Field(ge=1, le=10)
    state: VisionProposalState
    final: bool = Field(description="True for a state after which nothing changes any more.")
    action: Literal["drive", "return_to_dock", "resume"] | None = Field(
        default=None,
        description="The default option (`drive`, `return_to_dock`, or `resume`: optional "
        "drive steps, then the paused job continues); once approved, the option that runs; "
        "null before the analysis decided one.",
    )
    steps: int = Field(
        default=0, ge=0, le=6, description="Number of proposed drive steps (also for `resume`)."
    )
    fallback: bool = False
    low_confidence: bool = Field(
        default=False,
        description="The model's recommendation is offered below the confidence threshold.",
    )
    alternative_action: Literal["drive", "return_to_dock", "resume"] | None = Field(
        default=None,
        description="The model's low-confidence recommendation next to the fallback `action` "
        "(after an approval of it, `action` names it and this is null).",
    )
    expires_at: UtcDateTime | None = Field(
        default=None, description="Approval deadline while `proposed`; null otherwise."
    )
    link: str = Field(max_length=300, description="App path of the proposal.")
    trigger: VisionTrigger = VisionTrigger.MANUAL
    error_code: str | None = Field(default=None, max_length=64)
    photo_problem: VisionPhotoProblem | None = Field(
        default=None,
        description="The analysis got no camera photo, and why. With state `failed` and trigger "
        "`error`: the automatic analysis could not run (after its retries); start it by hand "
        "once the connection is better.",
    )
    signal_dbm: int | None = Field(
        default=None,
        ge=-150,
        le=-1,
        description="With `photo_problem` `weak_wifi` or `weak_mobile`: the signal in dBm.",
    )
    updated_at: UtcDateTime


CAPTURE_MAX_BYTES: Final = 512 * 1024
"""Upper size cap of a captured frame (the same cap as a photo reply). Deprecated with
:class:`CameraSnapshotRequest`."""


class CameraSnapshotRequest(ContractModel):
    """Deprecated: nothing sends or answers it since the capture worker was removed (ADR 0045;
    Vision's frames come from the camera relay's frame tap, ``VisionFrameSetRequest``). Kept in
    ``v1`` only because ``v1`` removes nothing; it goes with ``v2``.

    Was ``POST /capture`` of the capture worker (internal HTTP, ADR 0035): one still frame of the
    camera ``camera_uid`` (Agora publisher uid; None: the first publisher) in ``session``. The
    session carries the secret token; neither side logs it. The reply was ``image/jpeg``.
    """

    model_config = ConfigDict(json_schema_extra={"deprecated": True})

    session: CameraSession
    camera_uid: int | None = Field(default=None, ge=0)
    timeout_seconds: float = Field(default=20.0, gt=0, le=60)
    max_bytes: int = Field(default=CAPTURE_MAX_BYTES, ge=1024, le=CAPTURE_MAX_BYTES)
    max_width: int = Field(default=1280, ge=160, le=3840)
