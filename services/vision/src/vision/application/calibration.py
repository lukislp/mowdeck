"""Use case of the AI-assisted panorama calibration (ADR 0037): corresponding points only.

The web app calibrates the panorama of the two front cameras itself: it fits a homography from
point correspondences with RANSAC. With the AI-assisted method the configured vision model finds
those correspondences in a left and a right frame of the same moment; this use case sends both
frames, validates the answer (coordinates on the images, no duplicates) and returns it. There is
no proposal, no approval and nothing is driven, so ``can_view`` on the mower is enough (checked by
the API).

Same rules as AI assist: the plan feature ``ai_recover``, the caller's own provider or the
installation's, and one ``provider_requests`` row per model call (``kind = calibration``, the
cost estimate included). Calibrations have their own daily cap per user (and per account with
the installation's model), so they cannot use up the analyses a stuck mower needs.

The model call has a total time budget (``timeout_seconds``, all attempts and retries of the
provider included): beyond it the call is cancelled and answered with ``ProviderTimeout`` (504),
so the browser, whose own deadline sits a little above the budget, always gets a verdict instead
of a dropped connection.
"""

import asyncio
import time
from dataclasses import dataclass
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import FEATURE_AI_RECOVER, Principal
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.entitlements import Entitlements, require_feature

from vision.application.ports import Clock, ProviderFactory, Store
from vision.application.providers import ProviderService
from vision.domain.calibration import (
    MIN_USABLE_POINTS,
    FrameSize,
    PointPair,
    image_size,
    usable_points,
)
from vision.domain.errors import InvalidInput, ProviderFailed, ProviderTimeout, QuotaExceeded
from vision.domain.frames import STEREO_MAX_SKEW_MS, capture_skew_ms
from vision.domain.models import Image, ProviderConfig, ProviderRequest, Usage
from vision.domain.rules import day_start

_log = get_logger("vision.calibration")

MEDIA_TYPES = frozenset({"image/jpeg", "image/png"})


@dataclass(frozen=True, slots=True)
class CalibrationConfig:
    """Limits of the calibration calls (from the settings)."""

    user_daily_requests: int = 10
    """Calibration calls per user and UTC day (own and hosted providers)."""
    hosted_account_daily_requests: int = 20
    """Calibration calls with the installation's model per account and UTC day."""
    timeout_seconds: float = 150.0
    """Total time budget of the model call (every attempt and retry of the provider)."""
    max_image_bytes: int = 2 * 1024 * 1024
    max_skew_ms: float = STEREO_MAX_SKEW_MS
    """Two frames captured further apart than this are not the same moment."""


@dataclass(frozen=True, slots=True)
class CalibrationAnswer:
    """The validated points with the frame sizes they refer to, the model and the cost."""

    points: list[PointPair]
    left_size: FrameSize
    right_size: FrameSize
    model: str
    family: str
    hosted: bool
    usage: Usage
    dropped: int
    """Points the model returned that were off the images or duplicates."""


class CalibrationService:
    """Corresponding points for the panorama calibration."""

    def __init__(
        self,
        *,
        store: Store,
        providers: ProviderService,
        factory: ProviderFactory,
        entitlements: Entitlements,
        clock: Clock,
        config: CalibrationConfig,
    ) -> None:
        self.store = store
        self.providers = providers
        self.factory = factory
        self.entitlements = entitlements
        self.clock = clock
        self.config = config

    async def correspondences(
        self, principal: Principal, mower_id: UUID, left: Image, right: Image
    ) -> CalibrationAnswer:
        """Ask the caller's provider for corresponding points of ``left`` and ``right``
        (``can_view`` on the mower checked by the API). 403 without ``ai_recover``, 409 without
        a provider, 422 for unusable images, 429 beyond the daily cap, 502 when the model gives
        fewer than four usable points, 504 when the call exceeds ``timeout_seconds``."""
        if principal.account_id is None:
            raise InvalidInput("this operation needs an account-scoped principal")
        account = principal.account_id
        await require_feature(self.entitlements, account, FEATURE_AI_RECOVER)
        left_size = self._checked(left)
        right_size = self._checked(right)
        skew = capture_skew_ms(left, right)
        if skew is not None and skew > self.config.max_skew_ms:
            raise InvalidInput(
                f"the frames were captured {skew:.0f} ms apart; at most "
                f"{self.config.max_skew_ms:.0f} ms make a pair"
            )
        provider = await self.providers.resolve(principal)
        await self._check_quota(principal.subject, account, provider)
        started = time.monotonic()
        budget = asyncio.timeout(self.config.timeout_seconds)
        try:
            async with budget:
                answer = await self.factory.create(provider).correspond(
                    left, right, left_size, right_size
                )
        except TimeoutError as exc:
            if not budget.expired():
                # The provider's own timeout surfaced raw: a model failure, not the budget.
                await self._record(principal, provider, "error", started, detail="TimeoutError")
                raise ProviderFailed("the model failed: TimeoutError") from exc
            _log.warning(
                "calibration call timed out",
                mower_id=str(mower_id),
                budget_seconds=self.config.timeout_seconds,
            )
            await self._record(principal, provider, "timeout", started, detail="time budget")
            raise ProviderTimeout(
                f"the model did not answer within {self.config.timeout_seconds:.0f} s; "
                "try again, or configure a faster model"
            ) from exc
        except ProviderFailed as exc:
            await self._record(principal, provider, "error", started, detail=exc.message)
            raise
        except Exception as exc:
            _log.exception("calibration call crashed", mower_id=str(mower_id))
            await self._record(principal, provider, "error", started, detail=type(exc).__name__)
            raise ProviderFailed(f"the model failed: {type(exc).__name__}") from exc
        points = usable_points(answer.result.points, left_size, right_size)
        dropped = len(answer.result.points) - len(points)
        outcome = "ok" if len(points) >= MIN_USABLE_POINTS else "too_few_points"
        await self._record(
            principal,
            provider,
            outcome,
            started,
            usage=answer.usage,
            model=answer.model,
            family=answer.family,
            detail=f"mode={answer.usage.mode} points={len(points)} dropped={dropped}",
        )
        _log.info(
            "calibration points",
            mower_id=str(mower_id),
            model=answer.model,
            hosted=provider.hosted,
            points=len(points),
            dropped=dropped,
            cost_usd=answer.usage.cost_usd,
            latency_ms=answer.usage.latency_ms,
        )
        if outcome != "ok":
            raise ProviderFailed(
                f"the model found only {len(points)} usable corresponding points "
                f"(at least {MIN_USABLE_POINTS} are needed); try a scene with more structure"
            )
        return CalibrationAnswer(
            points=points,
            left_size=left_size,
            right_size=right_size,
            model=answer.model,
            family=answer.family,
            hosted=provider.hosted,
            usage=answer.usage,
            dropped=dropped,
        )

    def _checked(self, image: Image) -> FrameSize:
        if image.media_type not in MEDIA_TYPES:
            raise InvalidInput("images must be JPEG or PNG")
        if not image.data or len(image.data) > self.config.max_image_bytes:
            raise InvalidInput(f"an image must have 1 to {self.config.max_image_bytes} bytes")
        size = image_size(image.data)
        if size is None:
            raise InvalidInput("an image cannot be decoded")
        return size

    async def _check_quota(self, user_id: UUID, account_id: UUID, provider: ProviderConfig) -> None:
        since = day_start(self.clock())
        used = await self.store.count_requests(since=since, kind="calibration", user_id=user_id)
        if used >= self.config.user_daily_requests:
            raise QuotaExceeded(f"at most {self.config.user_daily_requests} calibrations per day")
        if provider.hosted:
            account_used = await self.store.count_requests(
                since=since, kind="calibration", account_id=account_id, hosted=True
            )
            if account_used >= self.config.hosted_account_daily_requests:
                msg = (
                    "the installation's model allows "
                    f"{self.config.hosted_account_daily_requests} calibrations per account and "
                    "day; configure your own provider for more"
                )
                raise QuotaExceeded(msg)

    async def _record(
        self,
        principal: Principal,
        provider: ProviderConfig,
        outcome: str,
        started: float,
        *,
        usage: Usage | None = None,
        model: str | None = None,
        family: str | None = None,
        detail: str | None = None,
    ) -> None:
        assert principal.account_id is not None  # noqa: S101 - checked by the caller
        latency = usage.latency_ms if usage and usage.latency_ms else None
        await self.store.record_request(
            ProviderRequest(
                id=uuid4(),
                account_id=principal.account_id,
                user_id=principal.subject,
                kind="calibration",
                model=model or provider.model,
                family=family or provider.family,
                hosted=provider.hosted,
                outcome=outcome,
                created_at=self.clock(),
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                cost_usd=usage.cost_usd if usage else None,
                latency_ms=(
                    latency if latency is not None else round((time.monotonic() - started) * 1000)
                ),
                detail=detail[:500] if detail else None,
            )
        )


__all__ = ["CalibrationAnswer", "CalibrationConfig", "CalibrationService"]
