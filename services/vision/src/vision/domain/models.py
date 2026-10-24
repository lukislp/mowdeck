"""Domain models of vision: plain data and the model's answer schema, no I/O.

A *proposal* is one round of AI assist for one mower: the scene went to a vision model, the
model's :class:`AssessmentOut` came back, and a user approves or rejects the maneuver. Rounds of
one assist session share ``chain_id``; a round that did not free the mower may be followed by
another round (``retry``) up to ``MOWDECK_VISION_MAX_ROUNDS``.

Blades are always off: the answer schema has no blade field at all, and every drive frame the
service publishes sets ``blade_on = False``.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal
from uuid import UUID

from mowdeck_contracts.v1 import PrincipalKind, PrincipalRef, VisionTask, VisionTrigger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_STEPS: Final = 6
MAX_STEP_SECONDS: Final = 3.0
RATIONALE_MAX: Final = 1000
ABORT_REASON_MAX: Final = 200


class Situation(StrEnum):
    """What the model thinks is going on."""

    STUCK_ON_OBSTACLE = "stuck_on_obstacle"
    WHEEL_SLIP = "wheel_slip"
    EDGE = "edge"
    TRAPPED = "trapped"
    AWAY_FROM_DOCK = "away_from_dock"
    NEAR_DOCK = "near_dock"
    CLEAR = "clear"
    UNKNOWN = "unknown"


class RiskFlag(StrEnum):
    """Hazards the model saw; any flag means the maneuver is never executed."""

    PEOPLE_VISIBLE = "people_visible"
    ANIMALS_VISIBLE = "animals_visible"
    DROP_OFF = "drop_off"
    WATER = "water"
    ROAD = "road"


class ProposalAction(StrEnum):
    """What a proposal does once a human approves it."""

    DRIVE = "drive"
    """The drive steps of ``proposal``, blades off."""
    RETURN_TO_DOCK = "return_to_dock"
    """The generic dock command: the mower drives home on its mapped paths by itself."""
    RESUME = "resume"
    """Continue the paused mowing job (``resume_mow``, the app's "continue"), optionally after
    the short drive steps of ``proposal`` (blades off) that free the mower first."""
    NONE = "none"
    """Nothing (the model found nothing safe to do)."""


class ProposalState(StrEnum):
    """Lifecycle of a proposal."""

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
    """The mower is free (recover), on its way to the dock (find_dock, return to dock) or
    mowing again (resume)."""
    NOT_RECOVERED = "not_recovered"
    """The steps were driven but did not help; another round may be requested."""
    ABORTED = "aborted"
    """Execution stopped early (emergency stop, sensor stop, geofence, no state, cancelled)."""


FINAL_STATES: Final = frozenset(
    {
        ProposalState.REJECTED_BY_AI,
        ProposalState.FAILED,
        ProposalState.REJECTED,
        ProposalState.EXPIRED,
        ProposalState.RECOVERED,
        ProposalState.NOT_RECOVERED,
        ProposalState.ABORTED,
    }
)
"""States after which nothing changes any more."""

RETRY_STATES: Final = frozenset(
    {ProposalState.NOT_RECOVERED, ProposalState.ABORTED, ProposalState.REJECTED_BY_AI}
)
"""Final states from which another round may be requested (within the round limit)."""


class StepOutcome(StrEnum):
    """What happened to one drive step."""

    PENDING = "pending"
    DONE = "done"
    ABORTED = "aborted"
    SKIPPED = "skipped"


class ProposedStep(BaseModel):
    """One drive step of the model's answer (normalised like a joystick, blades always off)."""

    model_config = ConfigDict(extra="forbid")

    linear: float = Field(ge=-1, le=1, description="Backward (-1) to forward (1).")
    angular: float = Field(ge=-1, le=1, description="Turn right (-1) to left (1).")
    seconds: float = Field(gt=0, le=MAX_STEP_SECONDS)


class AssessmentOut(BaseModel):
    """The model's answer, validated before anything is shown or driven."""

    model_config = ConfigDict(extra="forbid")

    situation: Situation
    confidence: float = Field(ge=0, le=1)
    action: ProposalAction = ProposalAction.DRIVE
    """``drive`` (the steps), ``return_to_dock`` (no steps), ``resume`` (optional steps, then
    the paused job continues) or ``none``. Answers stored before the field existed get
    ``drive`` with steps and ``none`` without."""
    proposal: list[ProposedStep] = Field(default_factory=list, max_length=MAX_STEPS)
    rationale: str = ""
    risk: list[RiskFlag] = Field(default_factory=list)
    abort_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _legacy_action(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "action" not in value:
            steps = value.get("proposal")
            action = ProposalAction.DRIVE if steps else ProposalAction.NONE
            return {**value, "action": action}
        return value

    @field_validator("rationale", mode="before")
    @classmethod
    def _cap_rationale(cls, value: Any) -> Any:
        return value[:RATIONALE_MAX] if isinstance(value, str) else value

    @field_validator("abort_reasons", mode="before")
    @classmethod
    def _cap_reasons(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [v[:ABORT_REASON_MAX] if isinstance(v, str) else v for v in value[:MAX_STEPS]]
        return value

    @field_validator("risk", mode="after")
    @classmethod
    def _unique_risks(cls, value: list[RiskFlag]) -> list[RiskFlag]:
        return list(dict.fromkeys(value))


ImageSource = Literal["upload", "mower", "map"]
"""``upload`` (sent by the app), ``mower`` (the gateway's photo) or ``map`` (the top-down map
rendering of :mod:`vision.domain.mapimage`)."""
CameraSide = Literal["left", "right"]
"""Which camera of a stereo pair took a frame (the mower's front left or front right camera)."""


@dataclass(frozen=True, slots=True)
class Image:
    """A JPEG or PNG for the model. Never logged; stored only for the proposal's lifetime."""

    data: bytes
    media_type: str
    source: ImageSource = "upload"
    camera: str | None = None
    """The camera's display name, e.g. ``Front left`` (frames from the camera panel)."""
    side: CameraSide | None = None
    """``left`` or ``right`` for a frame of a stereo pair."""
    captured_at: datetime | None = None
    """When the browser grabbed the frame (UTC)."""

    def info(self, index: int) -> "FrameInfo":
        """What is kept about the frame next to the proposal (no image bytes)."""
        return FrameInfo(
            index=index,
            source=self.source,
            camera=self.camera,
            side=self.side,
            captured_at=self.captured_at,
        )


@dataclass(frozen=True, slots=True)
class FrameInfo:
    """The description of one image a proposal's model saw (stored with the proposal; the
    bytes live in ``proposal_images`` until they are purged)."""

    index: int
    source: ImageSource = "upload"
    camera: str | None = None
    side: CameraSide | None = None
    captured_at: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        """A JSON-ready dict (the ``proposals.frames`` column)."""
        return {
            "index": self.index,
            "source": self.source,
            "camera": self.camera,
            "side": self.side,
            "captured_at": self.captured_at.isoformat() if self.captured_at else None,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "FrameInfo":
        """From :meth:`to_json` (unknown values fall back to none)."""
        source = value.get("source")
        side = value.get("side")
        at = value.get("captured_at")
        camera = value.get("camera")
        return cls(
            index=int(value.get("index", 0)),
            source=source if source in ("mower", "map") else "upload",
            camera=camera if isinstance(camera, str) else None,
            side=side if side in ("left", "right") else None,
            captured_at=datetime.fromisoformat(at) if isinstance(at, str) else None,
        )


@dataclass(frozen=True, slots=True)
class Usage:
    """What one analysis cost."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    """LiteLLM's estimate; None when the model has no price in its map."""
    latency_ms: int = 0
    attempts: int = 1
    mode: str = "json_schema"
    """``json_schema`` (structured output), ``prompt`` (JSON asked in the prompt) or ``fake``."""


@dataclass(frozen=True, slots=True)
class Assessment:
    """The validated answer plus who gave it and what it cost (the provider port's result)."""

    result: AssessmentOut
    model: str
    family: str
    usage: Usage


@dataclass(slots=True)
class Proposal:
    """One round of AI assist for one mower (the ``proposals`` row)."""

    id: UUID
    chain_id: UUID
    round: int
    account_id: UUID
    mower_id: UUID
    task: VisionTask
    state: ProposalState
    requested_by: PrincipalRef
    created_at: datetime
    updated_at: datetime
    parent_id: UUID | None = None
    provider_model: str | None = None
    provider_family: str | None = None
    hosted: bool = False
    assessment: AssessmentOut | None = None
    context: dict[str, Any] = field(default_factory=dict)
    image_count: int = 0
    light_used: bool = False
    """The photo the model saw was taken with the mower's light switched on (dark frame)."""
    stereo: bool = False
    """The model saw a stereo pair: the left and right front camera of the same moment."""
    frames: list[FrameInfo] = field(default_factory=list)
    """What each image was (camera, side, capture time), in the order the model saw them."""
    trigger: VisionTrigger = VisionTrigger.MANUAL
    """``manual`` (a user asked) or ``error`` (automatic analysis on a recoverable error)."""
    trigger_error_code: str | None = None
    """With trigger ``error``: the error code that started it."""
    detail: str | None = None
    analysed_at: datetime | None = None
    expires_at: datetime | None = None
    approved_by: PrincipalRef | None = None
    approved_at: datetime | None = None
    decided_by: PrincipalRef | None = None
    """Who rejected (or cancelled) it."""
    decided_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    drive_session_id: UUID | None = None
    start_pose: dict[str, float] | None = None
    end_pose: dict[str, float] | None = None
    return_command_id: UUID | None = None
    """The command issued after approval: ``return_to_dock`` or, for ``resume``,
    ``resume_mow``."""
    action: ProposalAction = ProposalAction.DRIVE
    """What approval executes: the drive steps, the generic return to dock or resuming the
    paused job (after optional drive steps)."""
    fallback: bool = False
    """``action`` is the rule based return to dock fallback, not the model's recommendation
    (labelled as such; it still needs a human approval)."""
    low_confidence: bool = False
    """The model's recommendation is offered below the confidence threshold: as ``action``
    itself, or as ``alternative_action`` next to the fallback."""
    alternative_action: ProposalAction | None = None
    """The model's low-confidence recommendation offered next to the default fallback
    ``action``; an approval with option ``model`` executes it instead."""
    model_refused: str | None = None
    """Why the model's low-confidence recommendation is not offered (a failed safety check)."""
    approved_option: str | None = None
    """Which option the approval chose: ``model`` or ``fallback``."""

    @property
    def final(self) -> bool:
        """True once nothing changes any more."""
        return self.state in FINAL_STATES


@dataclass(slots=True)
class ProposalStep:
    """The audit of one drive step (the ``proposal_steps`` row)."""

    proposal_id: UUID
    index: int
    linear: float
    angular: float
    seconds: float
    outcome: StepOutcome = StepOutcome.PENDING
    started_at: datetime | None = None
    ended_at: datetime | None = None
    frames: int = 0
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class StoredImage:
    """An image kept with its proposal until ``delete_after``."""

    proposal_id: UUID
    index: int
    media_type: str
    data: bytes
    source: ImageSource
    delete_after: datetime


RequestKind = Literal["analysis", "test"]
"""``analysis``: AI assist rounds; ``test``: provider tests."""


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """Cost accounting of one model call (the ``provider_requests`` row)."""

    id: UUID
    account_id: UUID
    user_id: UUID
    kind: RequestKind
    model: str
    family: str
    hosted: bool
    outcome: str
    created_at: datetime
    proposal_id: UUID | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    detail: str | None = None


@dataclass(slots=True)
class UserPreferences:
    """A user's AI assist preferences (the ``user_preferences`` row; defaults without one)."""

    user_id: UUID
    account_id: UUID
    stereo_frames: bool = True
    """Send both front camera frames when both views are live (twice the image cost)."""
    updated_at: datetime | None = None


@dataclass(slots=True)
class UserProvider:
    """A user's own vision model (the ``user_providers`` row); secrets stay encrypted here."""

    user_id: UUID
    account_id: UUID
    model: str
    enabled: bool
    created_at: datetime
    updated_at: datetime
    api_base: str | None = None
    api_key_ciphertext: bytes | None = None
    headers_ciphertext: bytes | None = None
    header_names: list[str] = field(default_factory=list)
    last_test: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class AutoSetting:
    """A user's opt-in to automatic analyses on errors (``user_preferences.auto_analysis``)."""

    user_id: UUID
    account_id: UUID
    enabled: bool
    updated_at: datetime
    kind: PrincipalKind = PrincipalKind.USER
    """The principal kind of the user (``user`` or ``subuser``): the automatic analysis acts as
    this user (provider, quotas, the light) and records it as the requester."""


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """A resolved provider for one call; ``api_key`` and ``headers`` exist only in memory."""

    model: str
    family: str
    hosted: bool
    api_base: str | None = None
    api_key: str | None = field(default=None, repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
