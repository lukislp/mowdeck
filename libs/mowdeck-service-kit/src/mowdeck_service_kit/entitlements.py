"""Plan enforcement for services: the Entitlements client, its problem types and a FastAPI gate.

Every enforcement point is one call::

    await entitlements.require_limit(account_id, "max_mowers", current=count)   # 409 plan-limit
    await entitlements.require_feature(account_id, "manual_drive")              # 403

The client asks the Entitlements service over core NATS (``entitlements.check``) and caches the
account's :class:`~mowdeck_contracts.v1.EntitlementSnapshot` for ``cache_seconds`` (default 60),
so later questions of the same account are answered without a request.
``entitlements.plan.changed`` drops cached snapshots (:meth:`EntitlementsClient.subscribe`).

When Entitlements does not answer (not deployed, no bus, timeout), the client falls back on the
installation switch ``MOWDECK_ENTITLEMENTS_BILLING_ENABLED`` of the calling service: billing
disabled means everything is allowed (the plan ``unlimited``), billing enabled means the check
is refused (``entitlements_unavailable``). ``view`` and ``emergency_stop`` are never refused and
never asked: no plan and no outage can stop a mower (ADR 0028).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from fastapi import FastAPI, Request
from mowdeck_contracts.v1 import (
    ALWAYS_ALLOWED_FEATURES,
    ENTITLEMENTS_PLAN_CHANGED,
    UNLIMITED_PLAN,
    DenyReason,
    EntitlementCheckReply,
    EntitlementCheckRequest,
    EntitlementLimitQuery,
    EntitlementSnapshot,
    EventEnvelope,
    PlanChanged,
    Principal,
    ProblemFieldError,
)
from mowdeck_contracts.v1.subjects import ENTITLEMENTS_CHECK, event_subject_for_type
from pydantic import ValidationError

from mowdeck_service_kit.bus import BusError, BusMessage, DropMessage, EventBus, Subscription
from mowdeck_service_kit.errors import Conflict, Forbidden
from mowdeck_service_kit.logging import get_logger
from mowdeck_service_kit.tenancy import current_principal

_log = get_logger(__name__)

PLAN_CHANGED_SUBJECT = event_subject_for_type(ENTITLEMENTS_PLAN_CHANGED)
"""``event.entitlements.plan.changed``."""


class PlanLimitReached(Conflict):
    """409 ``urn:mowdeck:problem:plan-limit``: the plan allows no more of this.

    ``errors`` carries the numbers: ``{"parameter": <limit key>, "code": "limit", "detail":
    "<limit>"}`` and the same with ``"code": "current"``.
    """

    problem_type = "plan-limit"
    title = "Plan limit reached"

    def __init__(self, reply: EntitlementCheckReply, key: str) -> None:
        self.reply = reply
        self.key = key
        if reply.reason is DenyReason.PLAN_INACTIVE:
            detail = "the plan of the account is not active: only viewing and emergency stop"
        elif reply.reason is DenyReason.UNAVAILABLE:
            detail = "the plan of the account cannot be checked right now"
        else:
            detail = f"the plan {reply.plan or 'of the account'} allows {reply.limit} ({key})"
        errors = [
            ProblemFieldError(parameter=key, code="limit", detail=str(reply.limit)),
            ProblemFieldError(parameter=key, code="current", detail=str(reply.current)),
        ]
        super().__init__(detail, errors=errors)


class FeatureNotInPlan(Forbidden):
    """403 ``urn:mowdeck:problem:feature-not-in-plan``: the plan does not include the feature."""

    problem_type = "feature-not-in-plan"
    title = "Feature not in plan"

    def __init__(self, reply: EntitlementCheckReply, feature: str) -> None:
        self.reply = reply
        self.feature = feature
        if reply.reason is DenyReason.PLAN_INACTIVE:
            detail = "the plan of the account is not active: only viewing and emergency stop"
        elif reply.reason is DenyReason.UNAVAILABLE:
            detail = "the plan of the account cannot be checked right now"
        else:
            detail = f"the plan {reply.plan or 'of the account'} does not include {feature}"
        code = reply.reason.value if reply.reason else "feature_not_in_plan"
        super().__init__(
            detail, errors=[ProblemFieldError(parameter=feature, code=code, detail=detail)]
        )


@runtime_checkable
class Entitlements(Protocol):
    """What enforcement points need (the client, or a fake in tests)."""

    async def check_feature(self, account_id: UUID, feature: str) -> EntitlementCheckReply:
        """May the account use ``feature``?"""
        ...

    async def check_limit(self, account_id: UUID, key: str, current: int) -> EntitlementCheckReply:
        """May the account have one more of ``key`` while it has ``current``?"""
        ...

    async def plan_of(self, account_id: UUID) -> str | None:
        """The account's plan key (None when unknown)."""
        ...

    def invalidate(self, account_id: UUID | None = None) -> None:
        """Drop the cached snapshot of one account (or all)."""
        ...


async def require_feature(entitlements: Entitlements, account_id: UUID, feature: str) -> None:
    """Raise :class:`FeatureNotInPlan` unless the account may use ``feature``."""
    reply = await entitlements.check_feature(account_id, feature)
    if not reply.allowed:
        raise FeatureNotInPlan(reply, feature)


async def require_limit(
    entitlements: Entitlements, account_id: UUID, key: str, current: int
) -> None:
    """Raise :class:`PlanLimitReached` unless the account may have one more of ``key``."""
    reply = await entitlements.check_limit(account_id, key, current)
    if not reply.allowed:
        raise PlanLimitReached(reply, key)


@dataclass(slots=True)
class _Cached:
    snapshot: EntitlementSnapshot
    expires: float


class EntitlementsClient:
    """:class:`Entitlements` over ``entitlements.check`` with a per-account snapshot cache.

    ``bus`` may be attached later (:meth:`attach`, at startup). ``billing_enabled`` is the
    calling service's own ``MOWDECK_ENTITLEMENTS_BILLING_ENABLED``; it only matters when
    Entitlements does not answer.
    """

    def __init__(
        self,
        bus: EventBus | None = None,
        *,
        billing_enabled: bool = False,
        timeout_seconds: float = 1.0,
        cache_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bus = bus
        self.billing_enabled = billing_enabled
        self.timeout_seconds = timeout_seconds
        self.cache_seconds = cache_seconds
        self._clock = clock
        self._cache: dict[UUID, _Cached] = {}

    def attach(self, bus: EventBus | None) -> None:
        """Use ``bus`` from now on (the kit creates the bus at startup)."""
        self.bus = bus

    def __len__(self) -> int:
        """How many account snapshots are cached."""
        return len(self._cache)

    def __bool__(self) -> bool:
        """Always true (an empty cache must not make ``client or default`` pick the default)."""
        return True

    def invalidate(self, account_id: UUID | None = None) -> None:
        """Drop the cached snapshot of ``account_id``, or every snapshot."""
        if account_id is None:
            self._cache.clear()
        else:
            self._cache.pop(account_id, None)

    def apply(self, change: PlanChanged) -> None:
        """Apply an ``entitlements.plan.changed`` payload to the cache."""
        if change.all_accounts or change.account_id is None:
            self.invalidate()
        else:
            self.invalidate(change.account_id)

    def _cached(self, account_id: UUID) -> EntitlementSnapshot | None:
        entry = self._cache.get(account_id)
        if entry is None:
            return None
        if self._clock() >= entry.expires:
            self._cache.pop(account_id, None)
            return None
        return entry.snapshot

    def _fallback(self, request: EntitlementCheckRequest) -> EntitlementCheckReply:
        current = request.limit.current if request.limit else None
        if not self.billing_enabled:
            return EntitlementCheckReply(allowed=True, plan=UNLIMITED_PLAN, current=current)
        return EntitlementCheckReply(allowed=False, reason=DenyReason.UNAVAILABLE, current=current)

    async def _ask(self, request: EntitlementCheckRequest) -> EntitlementCheckReply:
        cached = self._cached(request.account_id)
        if cached is not None:
            return cached.decide(request.feature, request.limit)
        if self.bus is None:
            return self._fallback(request)
        try:
            message = await self.bus.request(
                ENTITLEMENTS_CHECK, request, timeout=self.timeout_seconds
            )
            reply = message.model(EntitlementCheckReply)
        except (BusError, ValidationError, ValueError) as exc:
            _log.warning(
                "entitlements did not answer; using the local billing switch",
                error=type(exc).__name__,
                billing_enabled=self.billing_enabled,
            )
            return self._fallback(request)
        if reply.snapshot is not None and self.cache_seconds > 0:
            self._cache[request.account_id] = _Cached(
                reply.snapshot, self._clock() + self.cache_seconds
            )
        return reply

    async def check_feature(self, account_id: UUID, feature: str) -> EntitlementCheckReply:
        """May the account use ``feature``? ``view`` and ``emergency_stop`` always may."""
        if feature in ALWAYS_ALLOWED_FEATURES:
            return EntitlementCheckReply(allowed=True)
        return await self._ask(EntitlementCheckRequest(account_id=account_id, feature=feature))

    async def check_limit(self, account_id: UUID, key: str, current: int) -> EntitlementCheckReply:
        """May the account have one more of ``key`` while it has ``current``?"""
        query = EntitlementLimitQuery.model_validate({"key": key, "current": current})
        return await self._ask(EntitlementCheckRequest(account_id=account_id, limit=query))

    async def plan_of(self, account_id: UUID) -> str | None:
        """The account's plan key; on an outage ``unlimited`` (billing disabled) or None."""
        return (await self._ask(EntitlementCheckRequest(account_id=account_id))).plan

    async def require_feature(self, account_id: UUID, feature: str) -> None:
        """Raise :class:`FeatureNotInPlan` unless the account may use ``feature``."""
        await require_feature(self, account_id, feature)

    async def require_limit(self, account_id: UUID, key: str, current: int) -> None:
        """Raise :class:`PlanLimitReached` unless the account may have one more of ``key``."""
        await require_limit(self, account_id, key, current)

    async def subscribe(self, bus: EventBus | None = None) -> Subscription | None:
        """Drop cached snapshots on ``entitlements.plan.changed`` (every instance, ephemeral)."""
        active = bus or self.bus
        if active is None:
            return None

        async def on_plan_changed(message: BusMessage) -> None:
            try:
                event = message.model(EventEnvelope[PlanChanged])
            except ValidationError as exc:
                raise DropMessage(
                    f"malformed plan.changed event: {exc.error_count()} errors"
                ) from exc
            self.apply(event.payload)

        return await active.subscribe(PLAN_CHANGED_SUBJECT, on_plan_changed)


def client_from_settings(settings: object, bus: EventBus | None = None) -> EntitlementsClient:
    """A client configured from the kit settings (``MOWDECK_ENTITLEMENTS_*``)."""
    return EntitlementsClient(
        bus,
        billing_enabled=bool(getattr(settings, "entitlements_billing_enabled", False)),
        timeout_seconds=float(getattr(settings, "entitlements_timeout_seconds", 1.0)),
        cache_seconds=float(getattr(settings, "entitlements_cache_seconds", 60.0)),
    )


def install_entitlements(app: FastAPI, entitlements: Entitlements) -> None:
    """Put ``entitlements`` on ``app.state.entitlements`` for :func:`feature_gate`."""
    app.state.entitlements = entitlements


def get_entitlements(request: Request) -> Entitlements | None:
    """The installed :class:`Entitlements`, or None (no gate installed: nothing is refused)."""
    found: Entitlements | None = getattr(request.app.state, "entitlements", None)
    return found


def feature_gate(feature: str) -> Callable[..., object]:
    """Dependency factory: 401 without a principal, 403 ``feature-not-in-plan`` without the
    feature on the principal's account. Principals without an account pass (they hold no account
    data; the permission checks refuse them)."""

    async def dependency(request: Request) -> Principal:
        principal = await current_principal(request)
        entitlements = get_entitlements(request)
        if entitlements is not None and principal.account_id is not None:
            await require_feature(entitlements, principal.account_id, feature)
        return principal

    dependency.__name__ = f"feature_gate_{feature.replace('.', '_')}"
    return dependency
