"""Automatic AI analysis on mower errors (ADR 0035): the opt-in and the trigger.

**Opt-in.** Every user decides for themselves (``user_preferences.auto_analysis``, default off,
``GET``/``PUT /vision/me/preferences``) on the AI provider page; enabling needs the plan feature
``ai_recover``. The installation can switch the feature off
(``VISION_AUTO_ENABLED``).

**Trigger.** The durable consumer ``vision-auto`` (queue group, every state is handled by one
replica) passes every ``MowerState`` to :meth:`AutoAnalysis.on_state`:

1. :func:`~vision.domain.auto.recoverable_error` gives the state's recoverable error code or
   None. The shared watch (``auto_watch``, one row per mower, swapped atomically) remembers the
   previous one, so only *entering* an error (or changing to another recoverable code) counts,
   not every state while the error lasts. A replica skips the database for non-error states
   while it believes the mower is fine (refreshed every ``watch_refresh_seconds``).
2. Then, in a background task (the consumer is not held up by the photo or the model): states
   older than ``max_state_age_seconds`` are ignored; no analysis when any proposal of the mower
   was created within ``cooldown_minutes`` or one is open; at most ``daily_per_mower`` automatic
   analyses per mower and UTC day; the account's plan needs ``ai_recover``.
3. The users of the account who opted in, longest opted in first: the first who holds
   ``can_drive`` on the mower and has a usable provider (own, or the installation default) and
   daily quota left becomes the requester. :meth:`VisionService.request_auto` takes the gateway's
   photo (the camera relay for a real camera, the light for a dark frame), asking again within a
   bounded time when none comes or only one camera of a stereo pair, creates the proposal with
   ``trigger = error`` and analyses it like a manual request. Without any photo it records a
   ``failed`` proposal saying why (weak Wi-Fi with its dBm, offline, no frame), which
   Notifications sends to the owner as ``vision_auto_proposal`` ("no photo"), so the skip is
   never silent. Notifications turns the
   ``vision.proposal.created`` event into ``vision_auto_proposal``. Nothing moves without a human
   approval.
"""

import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from mowdeck_contracts.v1 import (
    FEATURE_AI_RECOVER,
    MowerState,
    Principal,
    PrincipalKind,
    VisionTrigger,
)
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.entitlements import Entitlements, FeatureNotInPlan, require_feature

from vision.application.assist import VisionService
from vision.application.ports import Clock, DriveChecker, Store
from vision.domain.auto import AutoConfig, Skip, recoverable_error
from vision.domain.errors import (
    ConflictError,
    InvalidInput,
    PhotoUnavailable,
    ProviderMissing,
    QuotaExceeded,
)
from vision.domain.models import AutoSetting, Proposal
from vision.domain.rules import day_start

_log = get_logger("vision.auto")

_UNSET = object()
_PEOPLE = frozenset({PrincipalKind.USER, PrincipalKind.SUBUSER})


@dataclass(frozen=True, slots=True)
class AutoView:
    """A user's opt-in with the installation's limits (the settings page)."""

    enabled: bool
    available: bool
    """False when the installation switched automatic analyses off."""
    cooldown_minutes: int
    daily_per_mower: int


class AutoSettings:
    """The users' opt-in to automatic analyses."""

    def __init__(
        self, *, store: Store, entitlements: Entitlements, clock: Clock, config: AutoConfig
    ) -> None:
        self.store = store
        self.entitlements = entitlements
        self.clock = clock
        self.config = config

    def _view(self, enabled: bool) -> AutoView:
        return AutoView(
            enabled=enabled,
            available=self.config.enabled,
            cooldown_minutes=self.config.cooldown_minutes,
            daily_per_mower=self.config.daily_per_mower,
        )

    async def get(self, principal: Principal) -> AutoView:
        """The caller's opt-in (off until saved)."""
        user, account = _person(principal)
        setting = await self.store.get_auto_setting(user, account)
        return self._view(setting.enabled if setting else False)

    async def put(self, principal: Principal, *, enabled: bool) -> AutoView:
        """Save the caller's opt-in; enabling needs the plan feature ``ai_recover``."""
        user, account = _person(principal)
        if enabled:
            await require_feature(self.entitlements, account, FEATURE_AI_RECOVER)
        await self.store.put_auto_setting(
            AutoSetting(
                user_id=user,
                account_id=account,
                enabled=enabled,
                updated_at=self.clock(),
                kind=principal.kind,
            )
        )
        _log.info("automatic analysis opt-in saved", user_id=str(user), enabled=enabled)
        return self._view(enabled)


class AutoAnalysis:
    """Starts an analysis when a mower enters a recoverable error (see the module docstring)."""

    def __init__(
        self,
        *,
        service: VisionService,
        store: Store,
        drivers: DriveChecker,
        entitlements: Entitlements,
        clock: Clock,
        config: AutoConfig,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.service = service
        self.store = store
        self.drivers = drivers
        self.entitlements = entitlements
        self.clock = clock
        self.config = config
        self._monotonic = monotonic
        self._known: dict[UUID, tuple[str | None, float]] = {}
        self._running: set[UUID] = set()

    def forget_mowers(self, mower_ids: Collection[UUID]) -> None:
        """Drop the remembered error of these mowers (demo sandbox purge)."""
        for mower_id in mower_ids:
            self._known.pop(mower_id, None)

    async def on_state(self, state: MowerState) -> bool:
        """Record the state's recoverable error; True when it starts an automatic analysis
        (which then runs in the background)."""
        code = recoverable_error(state, self.config.codes)
        mower_id = state.mower_id
        now = self._monotonic()
        known = self._known.get(mower_id, (_UNSET, 0.0))
        if code is None and known[0] is None and now - known[1] < self.config.watch_refresh_seconds:
            return False  # fine before, fine now: no database round trip
        self._known[mower_id] = (code, now)
        previous = await self.store.swap_auto_watch(mower_id, state.account_id, code, self.clock())
        if code is None or previous == code:
            return False
        _log.info(
            "mower entered a recoverable error",
            mower_id=str(mower_id),
            error_code=code,
            previous=previous,
        )
        self.service.spawn(self._run(state, code), f"vision-auto-{mower_id}")
        return True

    async def _run(self, state: MowerState, code: str) -> None:
        mower_id = state.mower_id
        if mower_id in self._running:
            _log.info("automatic analysis already running", mower_id=str(mower_id))
            return
        self._running.add(mower_id)
        try:
            outcome = await self.trigger(state, code)
        except Exception:
            _log.exception("automatic analysis crashed", mower_id=str(mower_id))
            return
        finally:
            self._running.discard(mower_id)
        if isinstance(outcome, Skip):
            _log.info(
                "automatic analysis skipped",
                mower_id=str(state.mower_id),
                error_code=code,
                reason=outcome.value,
            )

    async def trigger(self, state: MowerState, code: str) -> Proposal | Skip:
        """Check the limits and start the analysis for the first eligible user."""
        mower_id, account_id = state.mower_id, state.account_id
        config = self.config
        if not config.enabled:
            return Skip.DISABLED
        now = self.clock()
        if (now - state.observed_at).total_seconds() > config.max_state_age_seconds:
            return Skip.STALE
        cooldown = now - timedelta(minutes=config.cooldown_minutes)
        if await self.store.count_proposals(mower_id, since=cooldown) > 0:
            return Skip.COOLDOWN
        if await self.store.open_proposal(mower_id) is not None:
            return Skip.OPEN_PROPOSAL
        today = await self.store.count_proposals(
            mower_id, since=day_start(now), trigger=VisionTrigger.ERROR
        )
        if today >= config.daily_per_mower:
            return Skip.DAILY_BUDGET
        try:
            await require_feature(self.entitlements, account_id, FEATURE_AI_RECOVER)
        except FeatureNotInPlan:
            return Skip.NOT_IN_PLAN
        for setting in await self.store.auto_subscribers(account_id):
            if setting.kind not in _PEOPLE:
                continue
            if not await self.drivers.can_drive(setting.user_id, mower_id):
                _log.info("opted-in user may not drive the mower", user_id=str(setting.user_id))
                continue
            principal = Principal(subject=setting.user_id, account_id=account_id, kind=setting.kind)
            try:
                proposal = await self.service.request_auto(principal, mower_id, code)
            except (ProviderMissing, QuotaExceeded, FeatureNotInPlan) as exc:
                _log.info(
                    "opted-in user cannot run an analysis",
                    user_id=str(setting.user_id),
                    reason=type(exc).__name__,
                )
                continue
            except PhotoUnavailable as exc:
                # The failed proposal is recorded and announced: the owner learns why.
                _log.info(
                    "automatic analysis recorded without photo",
                    mower_id=str(mower_id),
                    error_code=code,
                    detail=exc.message,
                )
                return Skip.NO_PHOTO
            except ConflictError:
                return Skip.OPEN_PROPOSAL
            _log.info(
                "automatic analysis started",
                proposal_id=str(proposal.id),
                mower_id=str(mower_id),
                error_code=code,
                user_id=str(setting.user_id),
            )
            return proposal
        return Skip.NO_SUBSCRIBER


def _person(principal: Principal) -> tuple[UUID, UUID]:
    if principal.account_id is None or principal.kind not in _PEOPLE:
        raise InvalidInput("automatic analyses are a setting of a person in an account")
    return principal.subject, principal.account_id


__all__ = ["AutoAnalysis", "AutoSettings", "AutoView"]
