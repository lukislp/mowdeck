"""Request and response bodies of the Vision API."""

import base64
import binascii
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from mowdeck_contracts.v1 import PrincipalRef, VisionTask, VisionTrigger
from pydantic import AwareDatetime, BaseModel, Field

from vision.application.assist import ProposalDetails
from vision.application.auto import AutoView
from vision.application.providers import ProviderView, TestResult
from vision.domain.errors import InvalidInput
from vision.domain.families import Family
from vision.domain.job import JobContext
from vision.domain.models import (
    RETRY_STATES,
    CameraSide,
    FrameInfo,
    Image,
    Proposal,
    ProposalAction,
    ProposalState,
    ProposalStep,
    RiskFlag,
    StepOutcome,
    UserPreferences,
)


class ImageIn(BaseModel):
    """A camera snapshot, base64 encoded (JPEG or PNG), optionally with the camera that took
    it. Two frames with ``side`` ``left`` and ``right`` captured at most
    ``MOWDECK_VISION_STEREO_MAX_SKEW_MS`` apart form a stereo pair."""

    media_type: Literal["image/jpeg", "image/png"]
    data: str = Field(max_length=14_000_000, description="Base64 (standard) of the image bytes.")
    camera: str | None = Field(
        default=None, max_length=64, description="The camera's display name, e.g. `Front left`."
    )
    side: CameraSide | None = Field(
        default=None,
        description="`left` or `right`: the frame is one of the two front cameras (stereo).",
    )
    captured_at: AwareDatetime | None = Field(
        default=None, description="When the frame was grabbed (with a time zone)."
    )

    def to_image(self) -> Image:
        """Decode (422 for invalid base64)."""
        try:
            raw = base64.b64decode(self.data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InvalidInput("an image is not valid base64") from exc
        camera = (self.camera or "").strip() or None
        at = self.captured_at.astimezone(UTC) if self.captured_at else None
        return Image(raw, self.media_type, "upload", camera, self.side, at)


class RecoverIn(BaseModel):
    """``POST /mowers/{id}/recover`` as JSON. Without images the service asks the mower's
    gateway for one (``capture_photo``) and otherwise analyses the context alone."""

    task: VisionTask = VisionTask.RECOVER
    images: list[ImageIn] = Field(default_factory=list, max_length=8)


class RetryIn(BaseModel):
    """``POST /proposals/{id}/retry``: optional fresh snapshots."""

    images: list[ImageIn] = Field(default_factory=list, max_length=8)


class AssessmentOut(BaseModel):
    """What the model thinks (without the steps, which are listed separately)."""

    situation: str
    confidence: float
    action: ProposalAction = Field(
        default=ProposalAction.DRIVE,
        description="What the model recommended: `drive`, `return_to_dock`, `resume` or `none`.",
    )
    rationale: str
    risk: list[RiskFlag]
    abort_reasons: list[str]


class StepOut(BaseModel):
    """One drive step (blades are always off)."""

    index: int
    linear: float = Field(description="Backward (-1) to forward (1).")
    angular: float = Field(description="Turn right (-1) to left (1).")
    seconds: float
    outcome: StepOutcome
    started_at: datetime | None = None
    ended_at: datetime | None = None
    frames: int = 0
    detail: str | None = None

    @classmethod
    def of(cls, step: ProposalStep) -> "StepOut":
        """From the audit row."""
        return cls(
            index=step.index,
            linear=step.linear,
            angular=step.angular,
            seconds=step.seconds,
            outcome=step.outcome,
            started_at=step.started_at,
            ended_at=step.ended_at,
            frames=step.frames,
            detail=step.detail,
        )


class FrameOut(BaseModel):
    """What one image of the proposal was (fetch it from ``images/{index}``)."""

    index: int
    source: Literal["upload", "mower", "map"] = Field(
        description="`upload`: sent by the app; `mower`: the mower's own photo; `map`: the "
        "top-down map rendering around the mower (heading up) that Vision adds."
    )
    camera: str | None = None
    side: CameraSide | None = None
    captured_at: datetime | None = None

    @classmethod
    def of(cls, frame: FrameInfo) -> "FrameOut":
        """From the domain record."""
        return cls(
            index=frame.index,
            source=frame.source,
            camera=frame.camera,
            side=frame.side,
            captured_at=frame.captured_at,
        )


class JobAreaOut(BaseModel):
    """One planned area of the job, in driving order."""

    id: str
    name: str | None = None
    status: Literal["done", "current", "next", "planned"]


class JobOut(BaseModel):
    """The mowing job the mower was running when the scene was analysed (what `resume`
    continues)."""

    status: Literal["running", "paused", "paused_by_error", "returning", "spot_run"]
    areas: list[JobAreaOut] = Field(description="Planned areas in driving order.")
    current_area: str | None = None
    next_area: str | None = None
    progress_percent: int | None = None
    resumable: bool
    entry_distance_m: float | None = Field(
        default=None, description="Distance to the entry of the way to the next area."
    )
    mower_at_entry: bool = Field(
        default=False, description="The mower stood at the entry of the way to the next area."
    )

    @classmethod
    def of(cls, context: dict[str, object]) -> "JobOut | None":
        """From the stored scene context (None without a job or for an unreadable one)."""
        raw = context.get("job")
        if not isinstance(raw, dict):
            return None
        try:
            job = JobContext.model_validate(raw)
        except ValueError:
            return None
        link = job.connection_to_next
        return cls(
            status=job.status,
            areas=[JobAreaOut(id=a.id, name=a.name, status=a.status) for a in job.areas],
            current_area=job.current_area,
            next_area=job.next_area,
            progress_percent=job.progress_percent,
            resumable=job.resumable,
            entry_distance_m=link.entry_distance_m if link else None,
            mower_at_entry=link.mower_at_entry if link else False,
        )


class ProposalOut(BaseModel):
    """A proposal of AI assist (poll it while ``final`` is false)."""

    id: UUID
    chain_id: UUID
    round: int
    mower_id: UUID
    task: VisionTask
    state: ProposalState
    final: bool
    requested_by: PrincipalRef
    created_at: datetime
    updated_at: datetime
    provider_model: str | None
    provider_family: str | None
    hosted: bool
    assessment: AssessmentOut | None
    action: ProposalAction = Field(
        default=ProposalAction.DRIVE,
        description="What approval executes: `drive` (the steps, blades off), "
        "`return_to_dock` (the normal dock command; no steps) or `resume` (the steps, if any, "
        "then the paused job continues with `resume_mow`). `none` only on rejected rows.",
    )
    fallback: bool = Field(
        default=False,
        description="The model recommended nothing; Vision offers return to dock as a rule "
        "based fallback because it applies (mower in a mapped area linked to the dock, no "
        "safety stop). It still needs an approval.",
    )
    job: JobOut | None = Field(
        default=None,
        description="The mowing job as the model saw it (with action `resume` the job that "
        "continues); null when no job was known.",
    )
    steps: list[StepOut]
    image_count: int
    light_used: bool = Field(
        description="The photo was taken with the mower's light switched on (it was too dark)."
    )
    stereo: bool = Field(
        description="The model saw a stereo pair: the left and right front camera, same moment."
    )
    frames: list[FrameOut] = Field(
        description="What each image was, in the order the model saw them (left first in stereo)."
    )
    trigger: VisionTrigger = Field(
        default=VisionTrigger.MANUAL,
        description="`manual` (a user asked) or `error` (started automatically when the mower "
        "entered a recoverable error; the requester is the user who opted in).",
    )
    trigger_error_code: str | None = Field(
        default=None, description="With trigger `error`: the error code that started it."
    )
    detail: str | None = Field(description="Why it was rejected, failed or how it ended.")
    analysed_at: datetime | None
    expires_at: datetime | None = Field(description="Approval is refused after this time.")
    approved_by: PrincipalRef | None
    approved_at: datetime | None
    decided_by: PrincipalRef | None = Field(description="Who rejected or cancelled it.")
    decided_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    drive_session_id: UUID | None
    return_command_id: UUID | None = Field(
        description="The command sent after approval: `return_to_dock`, or `resume_mow` for "
        "action `resume`."
    )
    can_retry: bool
    rounds_left: int

    @classmethod
    def of(
        cls,
        proposal: Proposal,
        steps: list[ProposalStep] | None = None,
        *,
        can_retry: bool | None = None,
        rounds_left: int = 0,
    ) -> "ProposalOut":
        """From the domain object; steps from the audit rows or the answer."""
        answer = proposal.assessment
        if steps:
            step_out = [StepOut.of(s) for s in steps]
        elif answer is not None and proposal.action in (
            ProposalAction.DRIVE,
            ProposalAction.RESUME,
        ):
            step_out = [
                StepOut(
                    index=i,
                    linear=s.linear,
                    angular=s.angular,
                    seconds=s.seconds,
                    outcome=StepOutcome.PENDING,
                )
                for i, s in enumerate(answer.proposal)
            ]
        else:
            step_out = []
        return cls(
            id=proposal.id,
            chain_id=proposal.chain_id,
            round=proposal.round,
            mower_id=proposal.mower_id,
            task=proposal.task,
            state=proposal.state,
            final=proposal.final,
            requested_by=proposal.requested_by,
            created_at=proposal.created_at,
            updated_at=proposal.updated_at,
            provider_model=proposal.provider_model,
            provider_family=proposal.provider_family,
            hosted=proposal.hosted,
            assessment=AssessmentOut(
                situation=answer.situation.value,
                confidence=answer.confidence,
                action=answer.action,
                rationale=answer.rationale,
                risk=answer.risk,
                abort_reasons=answer.abort_reasons,
            )
            if answer
            else None,
            action=proposal.action,
            fallback=proposal.fallback,
            job=JobOut.of(proposal.context),
            steps=step_out,
            image_count=proposal.image_count,
            light_used=proposal.light_used,
            stereo=proposal.stereo,
            frames=[FrameOut.of(f) for f in proposal.frames],
            trigger=proposal.trigger,
            trigger_error_code=proposal.trigger_error_code,
            detail=proposal.detail,
            analysed_at=proposal.analysed_at,
            expires_at=proposal.expires_at,
            approved_by=proposal.approved_by,
            approved_at=proposal.approved_at,
            decided_by=proposal.decided_by,
            decided_at=proposal.decided_at,
            started_at=proposal.started_at,
            finished_at=proposal.finished_at,
            drive_session_id=proposal.drive_session_id,
            return_command_id=proposal.return_command_id,
            can_retry=can_retry
            if can_retry is not None
            else (proposal.state in RETRY_STATES and rounds_left > 0),
            rounds_left=rounds_left,
        )

    @classmethod
    def of_details(cls, details: ProposalDetails) -> "ProposalOut":
        """From the details use case."""
        return cls.of(
            details.proposal,
            details.steps,
            can_retry=details.can_retry,
            rounds_left=details.rounds_left,
        )


class ProposalPageOut(BaseModel):
    """``GET /mowers/{id}/proposals``: newest first; pass ``next_before`` as ``before``."""

    items: list[ProposalOut]
    next_before: datetime | None


class FamilyOut(BaseModel):
    """A model family the settings page offers."""

    key: str
    label: str
    prefix: str = Field(description="Model strings look like `<prefix>/<model>`.")
    examples: list[str]
    key_env: str | None = Field(description="Variable LiteLLM reads for the operator's key.")
    needs_key: bool
    needs_api_base: bool
    default_api_base: str | None
    local: bool = Field(description="Runs on own hardware: images stay local.")

    @classmethod
    def of(cls, family: Family) -> "FamilyOut":
        """From the domain table."""
        return cls(
            key=family.key,
            label=family.label,
            prefix=family.prefix,
            examples=list(family.examples),
            key_env=family.key_env,
            needs_key=family.needs_key,
            needs_api_base=family.needs_api_base,
            default_api_base=family.default_api_base,
            local=family.local,
        )


class ProvidersOut(BaseModel):
    """``GET /providers``: families, and whether the installation's model may be used."""

    families: list[FamilyOut]
    hosted_available: bool
    hosted_model: str | None
    secrets_available: bool = Field(description="Whether API keys can be stored here.")


class LastTestOut(BaseModel):
    """The outcome of the last provider test."""

    at: datetime
    ok: bool
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    detail: str | None = None


class ProviderOut(BaseModel):
    """``GET /me/provider``: the caller's provider (never the key) and the effective choice."""

    configured: bool
    enabled: bool
    model: str | None
    family: str | None
    api_base: str | None
    api_key_set: bool
    header_names: list[str]
    last_test: LastTestOut | None
    updated_at: datetime | None
    hosted_available: bool
    hosted_model: str | None
    effective: Literal["own", "hosted", "none"] = Field(
        description="Which provider a request would use now."
    )
    secrets_available: bool

    @classmethod
    def of(cls, view: ProviderView, *, secrets_available: bool) -> "ProviderOut":
        """From the use case view."""
        return cls(
            configured=view.configured,
            enabled=view.enabled,
            model=view.model,
            family=view.family,
            api_base=view.api_base,
            api_key_set=view.api_key_set,
            header_names=view.header_names,
            last_test=LastTestOut.model_validate(view.last_test) if view.last_test else None,
            updated_at=view.updated_at,
            hosted_available=view.hosted_available,
            hosted_model=view.hosted_model,
            effective=view.effective,
            secrets_available=secrets_available,
        )


class ProviderIn(BaseModel):
    """``PUT /me/provider``. ``api_key`` and ``headers`` are write-only: omit them to keep the
    stored values, send an empty string (or object) to delete them."""

    model: str = Field(min_length=3, max_length=200, examples=["anthropic/claude-sonnet-5-5"])
    enabled: bool = True
    api_base: str | None = Field(default=None, max_length=300)
    api_key: str | None = Field(default=None, max_length=2000)
    headers: dict[str, str] | None = None


class ProviderTestOut(BaseModel):
    """``POST /me/provider/test``: one minimal vision call with a tiny generated image."""

    ok: bool
    model: str
    family: str
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None = Field(description="LiteLLM's estimate; null when unknown.")
    detail: str | None

    @classmethod
    def of(cls, result: TestResult) -> "ProviderTestOut":
        """From the use case result."""
        return cls(
            ok=result.ok,
            model=result.model,
            family=result.family,
            latency_ms=result.latency_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
            detail=result.detail,
        )


class StatusOut(BaseModel):
    """``GET /status``: what the operator configured (never a key)."""

    hosted_default_enabled: bool
    hosted_default_model: str | None = Field(description="Only when the default is enabled.")
    secrets_available: bool
    allow_fake_provider: bool
    user_api_base_policy: str


class PreferencesOut(BaseModel):
    """``GET /me/preferences``: the caller's AI assist preferences and the installation's limits
    of automatic analyses."""

    stereo_frames: bool = Field(
        description=(
            "Send both front camera frames when both views are live (a stereo pair: two images, "
            "about twice the image share of the cost)."
        )
    )
    updated_at: datetime | None = Field(description="Null while the defaults apply.")
    auto_analysis: bool = Field(
        description="Automatic AI analysis on errors for the caller (opt-in, default off)."
    )
    auto_analysis_available: bool = Field(
        description="False when the installation switched automatic analyses off."
    )
    auto_cooldown_minutes: int = Field(
        description="No automatic analysis for a mower within this time after the previous one."
    )
    auto_daily_per_mower: int = Field(description="Automatic analyses per mower and UTC day.")

    @classmethod
    def of(cls, preferences: UserPreferences, auto: AutoView) -> "PreferencesOut":
        """From the domain object and the opt-in use case."""
        return cls(
            stereo_frames=preferences.stereo_frames,
            updated_at=preferences.updated_at,
            auto_analysis=auto.enabled,
            auto_analysis_available=auto.available,
            auto_cooldown_minutes=auto.cooldown_minutes,
            auto_daily_per_mower=auto.daily_per_mower,
        )


class PreferencesIn(BaseModel):
    """``PUT /me/preferences``: a field left out (null) keeps its value."""

    stereo_frames: bool | None = Field(
        default=None, description="Send both front camera frames when both views are live."
    )
    auto_analysis: bool | None = Field(
        default=None, description="Opt in to (or out of) automatic AI analysis on errors."
    )
