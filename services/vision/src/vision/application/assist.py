"""Use cases of AI assist: request an analysis, approve or reject, retry, read, sweep.

Every proposal belongs to the account of the mower; reads and decisions are only possible for
principals of that account (the API checks ``can_drive`` on the mower on top). Analyses and
executions run as background tasks; clients poll ``GET /proposals/{id}``.

Dark frames: when the gateway's photo is darker than ``dark_frame_threshold`` (mean luminance,
0..255) and the mower has a switchable light (capability ``light``) that is off, the service
issues ``set_light on`` through ``fleet.commands.issue`` on the requester's behalf, waits up to
``light_wait_seconds`` for the state to report the light on, takes the photo again and, after
the analysis, issues ``set_light off``. The proposal records ``light_used``. A light that
cannot be switched never stops the analysis: it goes on with the dark frame.

Stereo frames: two uploaded frames marked ``left`` and ``right`` (the camera panel's front
cameras, captured at most ``stereo_max_skew_ms`` apart) form a stereo pair. When the requester
keeps the preference ``stereo_frames`` (default on), the model gets both, left first, with an
explicit hint (``SceneContext.stereo``) and the proposal records ``stereo``; with the preference
off only the left frame is analysed. Every proposal keeps what each image was (``frames``).

Automatic analyses (ADR 0035, :meth:`VisionService.request_auto`, started by
:class:`~vision.application.auto.AutoAnalysis`): the same round as a manual request, on behalf
of the user who opted in, always with the gateway's photo (none: no analysis), recorded with
``trigger = error`` and the error code; the approval stays with a human.
"""

import asyncio
import contextlib
import time
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import (
    FEATURE_AI_RECOVER,
    CommandRequest,
    IssueCommandRequest,
    Principal,
    PrincipalRef,
    SetLight,
    VisionProposalCreated,
    VisionTask,
    VisionTrigger,
)
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.entitlements import Entitlements, require_feature

from vision.application.executor import Executor, RunOutcome
from vision.application.ports import (
    Clock,
    EventPublisher,
    FleetGateway,
    MowerView,
    PhotoSource,
    ProviderFactory,
    Store,
)
from vision.application.providers import ProviderService
from vision.domain.context import Motion, PreviousRound, SceneContext, build_context
from vision.domain.errors import (
    ConflictError,
    InvalidInput,
    NotFoundError,
    PhotoUnavailable,
    ProposalExpired,
    ProviderFailed,
    QuotaExceeded,
)
from vision.domain.frames import STEREO_MAX_SKEW_MS, capture_skew_ms, stereo_pair, too_dark
from vision.domain.models import (
    FINAL_STATES,
    RETRY_STATES,
    Image,
    Proposal,
    ProposalState,
    ProposalStep,
    ProviderConfig,
    ProviderRequest,
    StoredImage,
)
from vision.domain.rules import day_start, evaluate

_log = get_logger("vision.assist")

MEDIA_TYPES = frozenset({"image/jpeg", "image/png"})
OPEN_STATES = frozenset(
    {
        ProposalState.ANALYSING,
        ProposalState.PROPOSED,
        ProposalState.EXECUTING,
        ProposalState.VERIFYING,
    }
)


@dataclass(frozen=True, slots=True)
class AssistConfig:
    """Limits and timing of AI assist (from the settings)."""

    motion: Motion = field(
        default_factory=lambda: Motion(linear_mps_at_1=0.5, turn_deg_per_s_at_1=57.0)
    )
    proposal_ttl_seconds: int = 300
    max_rounds: int = 3
    min_confidence: float = 0.6
    user_daily_requests: int = 20
    hosted_account_daily_requests: int = 50
    keep_images_days: int = 0
    max_images: int = 4
    max_image_bytes: int = 2 * 1024 * 1024
    context_radius_m: float = 8.0
    stale_seconds: int = 120
    """Analyses and executions without progress for this long are given up (restarts)."""
    dark_frame_threshold: float = 40.0
    """Mean luminance (0..255) below which a gateway photo is retaken with the light on."""
    light_wait_seconds: float = 10.0
    """How long to wait for the state to report the light on before the second photo."""
    light_poll_seconds: float = 0.2
    stereo_max_skew_ms: float = STEREO_MAX_SKEW_MS
    """Two frames captured further apart than this are not a stereo pair."""


@dataclass(frozen=True, slots=True)
class ProposalDetails:
    """A proposal with its step audit and whether another round may be requested."""

    proposal: Proposal
    steps: list[ProposalStep]
    can_retry: bool
    rounds_left: int


class VisionService:
    """The AI assist use cases."""

    def __init__(
        self,
        *,
        store: Store,
        providers: ProviderService,
        factory: ProviderFactory,
        view: MowerView,
        photos: PhotoSource,
        executor: Executor,
        events: EventPublisher,
        entitlements: Entitlements,
        clock: Clock,
        config: AssistConfig,
        fleet: FleetGateway | None = None,
    ) -> None:
        self.store = store
        self.fleet = fleet if fleet is not None else executor.fleet
        self.providers = providers
        self.factory = factory
        self.view = view
        self.photos = photos
        self.executor = executor
        self.events = events
        self.entitlements = entitlements
        self.clock = clock
        self.config = config
        self._tasks: set[asyncio.Task[None]] = set()
        self._cancel: dict[UUID, asyncio.Event] = {}

    # --- background tasks -----------------------------------------------------------------

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def idle(self) -> None:
        """Wait until every background analysis and execution finished (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        """Cancel running work; executions stop their drive session first."""
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # --- request --------------------------------------------------------------------------

    async def request(
        self, principal: Principal, mower_id: UUID, task: VisionTask, images: list[Image]
    ) -> Proposal:
        """Start round 1 of an assist session for the mower (``can_drive`` checked by the API)."""
        account = _account(principal)
        await require_feature(self.entitlements, account, FEATURE_AI_RECOVER)
        self._check_images(images)
        open_proposal = await self.store.open_proposal(mower_id)
        if open_proposal is not None:
            msg = f"a proposal for this mower is {open_proposal.state}: decide it first"
            raise ConflictError(msg)
        return await self._start_round(
            principal, mower_id, task, images, chain_id=uuid4(), round_=1, parent=None
        )

    async def retry(self, principal: Principal, proposal_id: UUID, images: list[Image]) -> Proposal:
        """Another round after one that did not free the mower (up to ``max_rounds``)."""
        account = _account(principal)
        previous = await self._get(account, proposal_id)
        await require_feature(self.entitlements, account, FEATURE_AI_RECOVER)
        self._check_images(images)
        if previous.state not in RETRY_STATES:
            raise ConflictError(f"a proposal that is {previous.state} cannot be retried")
        if previous.round >= self.config.max_rounds:
            raise ConflictError(f"at most {self.config.max_rounds} rounds per assist session")
        chain = await self.store.chain(previous.chain_id)
        if chain and chain[-1].id != previous.id:
            raise ConflictError("only the newest round of an assist session can be retried")
        if await self.store.open_proposal(previous.mower_id) is not None:
            raise ConflictError("another proposal for this mower is open")
        return await self._start_round(
            principal,
            previous.mower_id,
            previous.task,
            images,
            chain_id=previous.chain_id,
            round_=previous.round + 1,
            parent=previous,
            history=chain,
        )

    async def request_auto(self, principal: Principal, mower_id: UUID, error_code: str) -> Proposal:
        """An automatic analysis on ``principal``'s behalf after the mower entered a recoverable
        error (``can_drive`` checked by the caller). Raises like :meth:`request` and
        :class:`~vision.domain.errors.PhotoUnavailable` when the gateway delivers no photo."""
        account = _account(principal)
        await require_feature(self.entitlements, account, FEATURE_AI_RECOVER)
        open_proposal = await self.store.open_proposal(mower_id)
        if open_proposal is not None:
            raise ConflictError(f"a proposal for this mower is {open_proposal.state}")
        return await self._start_round(
            principal,
            mower_id,
            VisionTask.RECOVER,
            [],
            chain_id=uuid4(),
            round_=1,
            parent=None,
            trigger=VisionTrigger.ERROR,
            error_code=error_code,
        )

    def spawn(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        """Run ``coro`` as a tracked background task (awaited by :meth:`idle`)."""
        self._spawn(coro, name)

    def _check_images(self, images: list[Image]) -> None:
        if len(images) > self.config.max_images:
            raise InvalidInput(f"at most {self.config.max_images} images")
        for image in images:
            if image.media_type not in MEDIA_TYPES:
                raise InvalidInput("images must be JPEG or PNG")
            if not image.data or len(image.data) > self.config.max_image_bytes:
                raise InvalidInput(f"an image must have 1 to {self.config.max_image_bytes} bytes")

    async def _start_round(
        self,
        principal: Principal,
        mower_id: UUID,
        task: VisionTask,
        images: list[Image],
        *,
        chain_id: UUID,
        round_: int,
        parent: Proposal | None,
        history: list[Proposal] | None = None,
        trigger: VisionTrigger = VisionTrigger.MANUAL,
        error_code: str | None = None,
    ) -> Proposal:
        account = _account(principal)
        provider = await self.providers.resolve(principal)
        await self._check_quota(principal.subject, account, provider)
        light_used = restore_light = False
        images, stereo = await self._stereo(principal, mower_id, images)
        if not images:
            photo = await self.photos.photo(mower_id, account)
            if photo is None and trigger is VisionTrigger.ERROR:
                raise PhotoUnavailable("the mower delivered no photo")
            if photo is not None:
                images = [photo]
                if self._light_helps(mower_id, account, photo):
                    restore_light, lit = await self._photo_with_light(principal, mower_id, account)
                    if lit is not None:
                        images, light_used = [lit], True
        now = self.clock()
        context = self._context(mower_id, account, task, len(images), history or [], stereo)
        proposal = Proposal(
            id=uuid4(),
            chain_id=chain_id,
            round=round_,
            account_id=account,
            mower_id=mower_id,
            task=task,
            state=ProposalState.ANALYSING,
            requested_by=principal.ref(),
            created_at=now,
            updated_at=now,
            parent_id=parent.id if parent else None,
            provider_model=provider.model,
            provider_family=provider.family,
            hosted=provider.hosted,
            context=context.model_dump(mode="json"),
            image_count=len(images),
            light_used=light_used,
            stereo=stereo,
            frames=[image.info(i) for i, image in enumerate(images)],
            trigger=trigger,
            trigger_error_code=error_code,
        )
        keep = timedelta(seconds=self.config.proposal_ttl_seconds) + timedelta(
            days=self.config.keep_images_days
        )
        stored = [
            StoredImage(proposal.id, i, img.media_type, img.data, img.source, now + keep)
            for i, img in enumerate(images)
        ]
        await self.store.add_proposal(proposal, stored)
        _log.info(
            "vision analysis requested",
            proposal_id=str(proposal.id),
            mower_id=str(mower_id),
            task=task.value,
            round=round_,
            model=provider.model,
            hosted=provider.hosted,
            images=len(images),
            light_used=light_used,
            stereo=stereo,
            trigger=trigger.value,
            error_code=error_code,
        )
        self._spawn(
            self._analyse_and_restore(
                proposal, provider, images, context, principal if restore_light else None
            ),
            f"vision-analyse-{proposal.id}",
        )
        return proposal

    # --- stereo frames --------------------------------------------------------------------

    async def _stereo(
        self, principal: Principal, mower_id: UUID, images: list[Image]
    ) -> tuple[list[Image], bool]:
        """The images to analyse and whether they are a stereo pair (left first).

        A pair is analysed as one when the requester keeps ``stereo_frames``; otherwise only its
        left frame. Anything else goes on unchanged.
        """
        pair = stereo_pair(images, self.config.stereo_max_skew_ms)
        if not pair:
            if len(images) == 2 and {i.side for i in images} == {"left", "right"}:
                _log.info(
                    "two camera frames are no stereo pair",
                    mower_id=str(mower_id),
                    skew_ms=capture_skew_ms(images[0], images[1]),
                )
            return images, False
        preferences = await self.providers.preferences(principal)
        if not preferences.stereo_frames:
            return pair[:1], False
        return pair, True

    # --- dark frames ----------------------------------------------------------------------

    def _light_helps(self, mower_id: UUID, account_id: UUID, photo: Image) -> bool:
        """The photo is too dark and the mower has a switchable light that is off."""
        if not too_dark(photo.data, self.config.dark_frame_threshold):
            return False
        caps = self.view.capabilities(mower_id)
        observed = self.view.state(mower_id)
        if caps is None or caps[0] != account_id or not caps[1].light:
            _log.info("dark photo, the mower has no switchable light", mower_id=str(mower_id))
            return False
        if observed is None or observed.state.light_on is not False:
            _log.info("dark photo, the light is not known to be off", mower_id=str(mower_id))
            return False
        return True

    async def _photo_with_light(
        self, principal: Principal, mower_id: UUID, account_id: UUID
    ) -> tuple[bool, Image | None]:
        """Switch the light on, wait for the mower to report it, take the photo again.

        Returns whether the light was switched (and must be switched off again) and the new
        photo (None: keep the dark one).
        """
        if not await self._set_light(principal.ref(), account_id, mower_id, on=True):
            return False, None
        deadline = self.view.monotonic() + self.config.light_wait_seconds
        while not self._light_reported_on(mower_id):
            if self.view.monotonic() >= deadline:
                _log.info("the mower did not report the light on in time", mower_id=str(mower_id))
                break
            await asyncio.sleep(self.config.light_poll_seconds)
        photo = await self.photos.photo(mower_id, account_id)
        if photo is None:
            _log.info("no second photo with the light on", mower_id=str(mower_id))
        return True, photo

    def _light_reported_on(self, mower_id: UUID) -> bool:
        observed = self.view.state(mower_id)
        return observed is not None and observed.state.light_on is True

    async def _set_light(
        self, on_behalf_of: PrincipalRef, account_id: UUID, mower_id: UUID, *, on: bool
    ) -> bool:
        """``set_light`` through Fleet on the user's behalf; False (logged) when refused."""
        request = IssueCommandRequest(
            on_behalf_of=on_behalf_of,
            account_id=account_id,
            mower_id=mower_id,
            command=CommandRequest(command=SetLight(on=on)),
            origin="vision",
        )
        try:
            reply = await self.fleet.issue(request)
        except Exception as exc:
            _log.warning("set_light failed", mower_id=str(mower_id), on=on, error=repr(exc))
            return False
        if reply.accepted is None:
            code = reply.error.code if reply.error else "unknown"
            _log.warning("set_light refused", mower_id=str(mower_id), on=on, code=str(code))
            return False
        _log.info("light switched for a photo", mower_id=str(mower_id), on=on)
        return True

    async def _analyse_and_restore(
        self,
        proposal: Proposal,
        provider: ProviderConfig,
        images: list[Image],
        context: SceneContext,
        light_owner: Principal | None,
    ) -> None:
        """The analysis; afterwards the light goes off again when it was switched on for it."""
        try:
            await self._analyse(proposal, provider, images, context)
        finally:
            if light_owner is not None:
                await asyncio.shield(
                    self._set_light(
                        light_owner.ref(), proposal.account_id, proposal.mower_id, on=False
                    )
                )

    async def _check_quota(self, user_id: UUID, account_id: UUID, provider: ProviderConfig) -> None:
        since = day_start(self.clock())
        used = await self.store.count_requests(since=since, kind="analysis", user_id=user_id)
        if used >= self.config.user_daily_requests:
            raise QuotaExceeded(f"at most {self.config.user_daily_requests} analyses per day")
        if provider.hosted:
            account_used = await self.store.count_requests(
                since=since, kind="analysis", account_id=account_id, hosted=True
            )
            if account_used >= self.config.hosted_account_daily_requests:
                msg = (
                    f"the installation's model allows {self.config.hosted_account_daily_requests} "
                    "analyses per account and day; configure your own provider for more"
                )
                raise QuotaExceeded(msg)

    def _context(
        self,
        mower_id: UUID,
        account_id: UUID,
        task: VisionTask,
        images: int,
        history: list[Proposal],
        stereo: bool = False,
    ) -> SceneContext:
        observed = self.view.state(mower_id)
        state = observed.state if observed and observed.state.account_id == account_id else None
        age = self.view.monotonic() - observed.received if observed and state else 0.0
        mapped = self.view.mower_map(mower_id)
        mower_map = mapped[1] if mapped and mapped[0] == account_id else None
        previous = [
            PreviousRound(
                round=p.round,
                situation=p.assessment.situation.value if p.assessment else None,
                steps=[s.model_dump() for s in p.assessment.proposal] if p.assessment else [],
                outcome=p.state.value,
                detail=p.detail,
            )
            for p in history
        ]
        last_drive = next(
            (f"AI assist round {p.round}: {p.state.value}" for p in reversed(history)), None
        )
        return build_context(
            task,
            state=state,
            state_age_seconds=age,
            mower_map=mower_map,
            commands=self.view.recent_commands(mower_id),
            now=self.clock(),
            last_drive=last_drive,
            previous_rounds=previous,
            motion=self.config.motion,
            images=images,
            radius_m=self.config.context_radius_m,
            stereo=stereo,
        )

    async def _analyse(
        self,
        proposal: Proposal,
        provider: ProviderConfig,
        images: list[Image],
        context: SceneContext,
    ) -> None:
        started = time.monotonic()
        try:
            assessment = await self.factory.create(provider).analyse(images, context, proposal.task)
        except ProviderFailed as exc:
            await self._analysis_failed(proposal, provider, exc.message, started)
            return
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.shield(
                    self._analysis_failed(proposal, provider, "the service stopped", started)
                )
            raise
        except Exception as exc:
            _log.exception("vision analysis crashed", proposal_id=str(proposal.id))
            await self._analysis_failed(
                proposal, provider, f"the analysis failed: {type(exc).__name__}", started
            )
            return
        usage = assessment.usage
        state, detail = evaluate(assessment.result, min_confidence=self.config.min_confidence)
        now = self.clock()
        await self.store.record_request(
            ProviderRequest(
                id=uuid4(),
                account_id=proposal.account_id,
                user_id=proposal.requested_by.subject,
                kind="analysis",
                model=assessment.model,
                family=assessment.family,
                hosted=provider.hosted,
                outcome="ok",
                created_at=now,
                proposal_id=proposal.id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=usage.cost_usd,
                latency_ms=usage.latency_ms,
                detail=(
                    f"mode={usage.mode} attempts={usage.attempts} images={len(images)}"
                    f" stereo={str(proposal.stereo).lower()}"
                ),
            )
        )
        expires = now + timedelta(seconds=self.config.proposal_ttl_seconds)
        if state is ProposalState.PROPOSED:
            await self.store.put_steps(
                [
                    ProposalStep(proposal.id, i, s.linear, s.angular, s.seconds)
                    for i, s in enumerate(assessment.result.proposal)
                ]
            )
        changed = await self.store.update_proposal(
            proposal.id,
            {
                "state": state,
                "assessment": assessment.result,
                "detail": detail,
                "analysed_at": now,
                "expires_at": expires if state is ProposalState.PROPOSED else None,
                "provider_model": assessment.model,
                "updated_at": now,
            },
            states={ProposalState.ANALYSING},
        )
        _log.info(
            "vision analysis done",
            proposal_id=str(proposal.id),
            state=state.value,
            situation=assessment.result.situation.value,
            confidence=assessment.result.confidence,
            steps=len(assessment.result.proposal),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
            latency_ms=usage.latency_ms,
        )
        if changed and state is ProposalState.PROPOSED:
            await self.events.proposal_created(
                VisionProposalCreated(
                    proposal_id=proposal.id,
                    mower_id=proposal.mower_id,
                    account_id=proposal.account_id,
                    task=proposal.task,
                    round=proposal.round,
                    requested_by=proposal.requested_by,
                    situation=assessment.result.situation.value,
                    confidence=assessment.result.confidence,
                    steps=len(assessment.result.proposal),
                    expires_at=expires,
                    link=f"/mowers/{proposal.mower_id}?assist={proposal.id}",
                    created_at=now,
                    trigger=proposal.trigger,
                    error_code=proposal.trigger_error_code,
                )
            )

    async def _analysis_failed(
        self, proposal: Proposal, provider: ProviderConfig, detail: str, started: float
    ) -> None:
        now = self.clock()
        await self.store.record_request(
            ProviderRequest(
                id=uuid4(),
                account_id=proposal.account_id,
                user_id=proposal.requested_by.subject,
                kind="analysis",
                model=provider.model,
                family=provider.family,
                hosted=provider.hosted,
                outcome="error",
                created_at=now,
                proposal_id=proposal.id,
                latency_ms=round((time.monotonic() - started) * 1000),
                detail=detail[:500],
            )
        )
        await self.store.update_proposal(
            proposal.id,
            {"state": ProposalState.FAILED, "detail": detail[:500], "updated_at": now},
            states={ProposalState.ANALYSING},
        )
        _log.warning("vision analysis failed", proposal_id=str(proposal.id), detail=detail)

    # --- decisions ------------------------------------------------------------------------

    async def approve(self, principal: Principal, proposal_id: UUID) -> Proposal:
        """Approve a proposal (``can_drive`` checked by the API): drive it on the approver's
        behalf. One approval per proposal; refused after ``expires_at``."""
        account = _account(principal)
        proposal = await self._get(account, proposal_id)
        await require_feature(self.entitlements, account, FEATURE_AI_RECOVER)
        if proposal.state is not ProposalState.PROPOSED:
            raise ConflictError(f"the proposal is {proposal.state}")
        now = self.clock()
        if proposal.expires_at is None or proposal.expires_at <= now:
            await self.store.update_proposal(
                proposal_id,
                {"state": ProposalState.EXPIRED, "updated_at": now},
                states={ProposalState.PROPOSED},
            )
            raise ProposalExpired("the proposal expired: request a new analysis")
        approver = principal.ref()
        changed = await self.store.update_proposal(
            proposal_id,
            {
                "state": ProposalState.EXECUTING,
                "approved_by": approver,
                "approved_at": now,
                "started_at": now,
                "updated_at": now,
            },
            states={ProposalState.PROPOSED},
        )
        if not changed:
            raise ConflictError("the proposal was decided by someone else")
        approved = await self._get(account, proposal_id)
        self._cancel[proposal_id] = asyncio.Event()
        _log.info(
            "vision proposal approved",
            proposal_id=str(proposal_id),
            approved_by=str(approver.subject),
        )
        self._spawn(self._execute(approved, approver, account), f"vision-execute-{proposal_id}")
        return approved

    async def _execute(self, proposal: Proposal, approver: PrincipalRef, account: UUID) -> None:
        event = self._cancel.setdefault(proposal.id, asyncio.Event())
        checked = {"at": time.monotonic()}

        async def cancelled() -> bool:
            if event.is_set():
                return True
            if time.monotonic() - checked["at"] >= 1.0:
                checked["at"] = time.monotonic()
                fresh = await self.store.get_proposal(proposal.id)
                if fresh is not None and fresh.cancel_requested_at is not None:
                    event.set()
            return event.is_set()

        async def on_verifying(session_id: UUID | None) -> None:
            await self.store.update_proposal(
                proposal.id,
                {
                    "state": ProposalState.VERIFYING,
                    "drive_session_id": session_id,
                    "updated_at": self.clock(),
                },
                states={ProposalState.EXECUTING},
            )

        try:
            outcome = await self.executor.run(
                proposal, approver, account, cancelled=cancelled, on_verifying=on_verifying
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._finish(proposal, RunOutcome(ProposalState.ABORTED, "the service stopped"))
            )
            raise
        except Exception as exc:
            _log.exception("vision execution crashed", proposal_id=str(proposal.id))
            outcome = RunOutcome(ProposalState.FAILED, f"execution failed: {type(exc).__name__}")
        finally:
            self._cancel.pop(proposal.id, None)
        await self._finish(proposal, outcome)

    async def _finish(self, proposal: Proposal, outcome: RunOutcome) -> None:
        now = self.clock()
        changes: dict[str, Any] = {
            "state": outcome.state,
            "detail": outcome.detail[:500],
            "finished_at": now,
            "updated_at": now,
            "start_pose": outcome.start_pose.model_dump() if outcome.start_pose else None,
            "end_pose": outcome.end_pose.model_dump() if outcome.end_pose else None,
            "return_command_id": outcome.return_command_id,
        }
        if outcome.session_id is not None:
            changes["drive_session_id"] = outcome.session_id
        await self.store.update_proposal(
            proposal.id, changes, states={ProposalState.EXECUTING, ProposalState.VERIFYING}
        )
        _log.info(
            "vision proposal finished",
            proposal_id=str(proposal.id),
            state=outcome.state.value,
            detail=outcome.detail,
        )

    async def reject(self, principal: Principal, proposal_id: UUID) -> Proposal:
        """Reject a waiting proposal, or cancel a running execution (it stops at once)."""
        account = _account(principal)
        proposal = await self._get(account, proposal_id)
        now = self.clock()
        who = principal.ref()
        if proposal.state is ProposalState.PROPOSED:
            changed = await self.store.update_proposal(
                proposal_id,
                {
                    "state": ProposalState.REJECTED,
                    "decided_by": who,
                    "decided_at": now,
                    "updated_at": now,
                },
                states={ProposalState.PROPOSED},
            )
            if not changed:
                raise ConflictError("the proposal was decided by someone else")
        elif proposal.state in (ProposalState.EXECUTING, ProposalState.VERIFYING):
            await self.store.update_proposal(
                proposal_id,
                {"cancel_requested_at": now, "decided_by": who, "decided_at": now},
            )
            event = self._cancel.get(proposal_id)
            if event is not None:
                event.set()
        else:
            raise ConflictError(f"the proposal is {proposal.state}")
        return await self._get(account, proposal_id)

    # --- reads ----------------------------------------------------------------------------

    async def _get(self, account_id: UUID, proposal_id: UUID) -> Proposal:
        proposal = await self.store.get_proposal(proposal_id)
        if proposal is None or proposal.account_id != account_id:
            raise NotFoundError("proposal not found")
        return proposal

    async def find(self, principal: Principal, proposal_id: UUID) -> Proposal:
        """A proposal of the caller's account (404 otherwise)."""
        return await self._get(_account(principal), proposal_id)

    async def details(self, principal: Principal, proposal_id: UUID) -> ProposalDetails:
        """A proposal with its steps and the retry option."""
        proposal = await self._get(_account(principal), proposal_id)
        steps = await self.store.steps(proposal_id)
        rounds_left = max(0, self.config.max_rounds - proposal.round)
        can_retry = proposal.state in RETRY_STATES and rounds_left > 0
        if can_retry:
            chain = await self.store.chain(proposal.chain_id)
            can_retry = not chain or chain[-1].id == proposal.id
        return ProposalDetails(proposal, steps, can_retry, rounds_left)

    async def list_for_mower(
        self, principal: Principal, mower_id: UUID, *, limit: int, before: datetime | None
    ) -> list[Proposal]:
        """Proposals of a mower of the caller's account, newest first."""
        return await self.store.list_proposals(
            _account(principal), mower_id, limit=limit, before=before
        )

    async def image(self, principal: Principal, proposal_id: UUID, index: int) -> StoredImage:
        """An image the model saw (until it is purged)."""
        proposal = await self._get(_account(principal), proposal_id)
        stored = await self.store.get_image(proposal.id, index)
        if stored is None or stored.delete_after <= self.clock():
            raise NotFoundError("image not found (images are kept only for a short time)")
        return stored

    # --- sweeps ---------------------------------------------------------------------------

    async def sweep(self) -> dict[str, int]:
        """Expire waiting proposals, give up stale work, purge old images."""
        now = self.clock()
        counts = {"expired": 0, "failed": 0, "aborted": 0, "images": 0}
        ttl = timedelta(seconds=self.config.proposal_ttl_seconds)
        for proposal in await self.store.proposals_in(
            {ProposalState.PROPOSED}, updated_before=now - ttl
        ):
            if proposal.expires_at is not None and proposal.expires_at > now:
                continue
            if await self.store.update_proposal(
                proposal.id,
                {"state": ProposalState.EXPIRED, "updated_at": now},
                states={ProposalState.PROPOSED},
            ):
                counts["expired"] += 1
        stale = now - timedelta(seconds=self.config.stale_seconds)
        for proposal in await self.store.proposals_in(
            {ProposalState.ANALYSING}, updated_before=stale
        ):
            if await self.store.update_proposal(
                proposal.id,
                {
                    "state": ProposalState.FAILED,
                    "detail": "the analysis was interrupted",
                    "updated_at": now,
                },
                states={ProposalState.ANALYSING},
            ):
                counts["failed"] += 1
        running = {ProposalState.EXECUTING, ProposalState.VERIFYING}
        for proposal in await self.store.proposals_in(running, updated_before=stale):
            if proposal.id in self._cancel:
                continue  # still running in this instance
            if await self.store.update_proposal(
                proposal.id,
                {
                    "state": ProposalState.ABORTED,
                    "detail": "the execution was interrupted",
                    "updated_at": now,
                },
                states=running,
            ):
                counts["aborted"] += 1
        counts["images"] = await self.store.purge_images(now)
        if any(counts.values()):
            _log.info("vision sweep", **counts)
        return counts


def _account(principal: Principal) -> UUID:
    if principal.account_id is None:
        raise InvalidInput("this operation needs an account-scoped principal")
    return principal.account_id


__all__ = ["FINAL_STATES", "OPEN_STATES", "AssistConfig", "ProposalDetails", "VisionService"]
