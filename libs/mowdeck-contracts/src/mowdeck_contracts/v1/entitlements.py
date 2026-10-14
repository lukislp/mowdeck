"""Plans, limits and feature gates: what the Entitlements service answers (ADR 0028).

Entitlements answers one question: "may account X use feature Y, or create one more Z?". The
answer depends on the account's plan (data in the Entitlements database, seeded from
``infra/entitlements/plans.yaml``) and its status:

=========  =================================================================================
Status     Meaning
=========  =================================================================================
active     the plan's limits and features apply
trial      like active, until ``trial_until``; afterwards the account counts as ``expired``
grace      a payment problem: only ``view`` and ``emergency_stop`` remain, nothing new may be
           created; a mower is never stopped because of a plan change
expired    like grace, without an end
=========  =================================================================================

Services ask over core NATS (``entitlements.check``, request :class:`EntitlementCheckRequest`,
reply :class:`EntitlementCheckReply`). Every reply carries the account's
:class:`EntitlementSnapshot`, so the client in ``mowdeck-service-kit`` caches one snapshot per
account and decides later questions locally with :meth:`EntitlementSnapshot.decide`, the same
function the service uses. ``entitlements.plan.changed`` (payload :class:`PlanChanged`) drops
cached snapshots. The Billing service sets plans with ``entitlements.plan.set``
(:class:`PlanSetRequest` in, :class:`PlanSetReply` out, ``source=billing``).

Feature keys are hierarchical: holding ``notifications`` grants ``notifications.push``; the key
``*`` grants every feature (plan ``unlimited``).
"""

from enum import StrEnum
from typing import Final, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime
from mowdeck_contracts.v1.module import FeatureKey

ENTITLEMENTS_PLAN_CHANGED: Final = "entitlements.plan.changed"
"""Event type: the plan or status of an account (or a plan definition) changed."""

UNLIMITED_PLAN: Final = "unlimited"
"""Key of the plan every account resolves to while billing is disabled."""

ALL_FEATURES: Final = "*"
"""Feature entry that grants every feature."""

FEATURE_VIEW: Final = "view"
"""Viewing mowers, maps and history. Always kept, also in ``grace`` and ``expired``."""
FEATURE_CONTROL: Final = "control"
"""Every mower command except the emergency stop."""
FEATURE_EMERGENCY_STOP: Final = "emergency_stop"
"""The emergency stop. Always kept; clients never block it on a plan."""

INACTIVE_FEATURES: Final = (FEATURE_VIEW, FEATURE_EMERGENCY_STOP)
"""What remains of any plan in ``grace`` and ``expired``."""

ALWAYS_ALLOWED_FEATURES: Final = frozenset(INACTIVE_FEATURES)
"""Features no plan and no outage can take away (clients do not even ask)."""

LimitKey = Literal[
    "max_mowers", "max_users", "max_sites", "max_groups", "max_schedules", "history_days"
]
"""The limits a plan defines."""

LIMIT_KEYS: Final[tuple[str, ...]] = (
    "max_mowers",
    "max_users",
    "max_sites",
    "max_groups",
    "max_schedules",
    "history_days",
)
"""Every :data:`LimitKey`, in display order."""


class PlanStatus(StrEnum):
    """Status of an account's plan."""

    ACTIVE = "active"
    TRIAL = "trial"
    GRACE = "grace"
    EXPIRED = "expired"

    @property
    def is_inactive(self) -> bool:
        """True for ``grace`` and ``expired`` (only view and emergency stop remain)."""
        return self in (PlanStatus.GRACE, PlanStatus.EXPIRED)


class PlanSource(StrEnum):
    """Who set an account's plan."""

    DEFAULT = "default"
    """The default plan (no assignment, or billing disabled)."""
    MANUAL = "manual"
    """An operator assigned it (``PUT /entitlements/accounts/{id}/plan``)."""
    BILLING = "billing"
    """The Billing service set it (``entitlements.plan.set``, from the payment provider)."""


class DenyReason(StrEnum):
    """Why a check was refused."""

    FEATURE_NOT_IN_PLAN = "feature_not_in_plan"
    LIMIT_REACHED = "limit_reached"
    PLAN_INACTIVE = "plan_inactive"
    """``grace`` or ``expired``: nothing new may be created, only view and emergency stop."""
    UNAVAILABLE = "entitlements_unavailable"
    """Entitlements did not answer and billing is enabled (the client fails closed)."""


class PlanLimits(ContractModel):
    """The limits of a plan; ``None`` means unlimited."""

    max_mowers: int | None = Field(default=None, ge=0)
    max_users: int | None = Field(default=None, ge=0)
    max_sites: int | None = Field(default=None, ge=0)
    max_groups: int | None = Field(default=None, ge=0)
    max_schedules: int | None = Field(default=None, ge=0)
    history_days: int | None = Field(default=None, ge=0)

    def get(self, key: str) -> int | None:
        """The limit named ``key`` (``ValueError`` for an unknown key)."""
        if key not in LIMIT_KEYS:
            msg = f"unknown limit {key!r}"
            raise ValueError(msg)
        value: int | None = getattr(self, key)
        return value


class EntitlementLimitQuery(ContractModel):
    """A hard limit question: may the account have one more, given ``current`` exist now?"""

    key: LimitKey
    current: int = Field(ge=0, description="How many the account has now (before creating).")


class EntitlementCheckReply(ContractModel):
    """Reply on ``entitlements.check``.

    ``allowed`` answers the question; ``plan`` and ``status`` name the resolved plan; ``limit``
    and ``current`` echo a limit question. ``snapshot`` lets the client answer later questions
    of the account from its cache.
    """

    allowed: bool
    plan: str | None = Field(default=None, max_length=64)
    status: PlanStatus | None = None
    reason: DenyReason | None = None
    limit: int | None = None
    current: int | None = None
    snapshot: "EntitlementSnapshot | None" = None


class EntitlementSnapshot(ContractModel):
    """The resolved plan of one account at one moment: everything a decision needs."""

    account_id: UUID
    plan: str = Field(min_length=1, max_length=64)
    status: PlanStatus = PlanStatus.ACTIVE
    limits: PlanLimits = Field(default_factory=PlanLimits)
    features: list[str] = Field(default_factory=list, max_length=500)
    billing_enabled: bool = False

    def has_feature(self, feature: str) -> bool:
        """True if the plan grants ``feature`` (or a parent key, or ``*``), honouring status."""
        if feature in ALWAYS_ALLOWED_FEATURES:
            return True
        if self.status.is_inactive:
            return False
        granted = set(self.features)
        if ALL_FEATURES in granted:
            return True
        parts = feature.split(".")
        return any(".".join(parts[:end]) in granted for end in range(1, len(parts) + 1))

    def decide(
        self, feature: str | None = None, limit: EntitlementLimitQuery | None = None
    ) -> EntitlementCheckReply:
        """Answer a feature or limit question (neither: allowed, names the plan)."""
        allowed = True
        reason: DenyReason | None = None
        maximum: int | None = None
        current: int | None = None
        if limit is not None:
            maximum, current = self.limits.get(limit.key), limit.current
            if self.status.is_inactive:
                allowed, reason = False, DenyReason.PLAN_INACTIVE
            elif maximum is not None and limit.current >= maximum:
                allowed, reason = False, DenyReason.LIMIT_REACHED
        elif feature is not None and not self.has_feature(feature):
            allowed = False
            reason = (
                DenyReason.PLAN_INACTIVE
                if self.status.is_inactive
                else DenyReason.FEATURE_NOT_IN_PLAN
            )
        return EntitlementCheckReply(
            allowed=allowed,
            plan=self.plan,
            status=self.status,
            reason=reason,
            limit=maximum,
            current=current,
            snapshot=self,
        )


EntitlementCheckReply.model_rebuild()


class EntitlementCheckRequest(ContractModel):
    """Request on ``entitlements.check`` (core NATS, internal).

    Ask either a ``feature`` or a ``limit``; with neither the reply only names the plan (Identity
    uses that for the token's ``plan`` claim).
    """

    account_id: UUID
    feature: FeatureKey | None = None
    limit: EntitlementLimitQuery | None = None

    @model_validator(mode="after")
    def _one_question(self) -> Self:
        if self.feature is not None and self.limit is not None:
            msg = "ask either a feature or a limit, not both"
            raise ValueError(msg)
        return self


class PlanChanged(ContractModel):
    """Payload of ``entitlements.plan.changed``: drop cached snapshots.

    ``account_id`` names one account (manual assignment); ``all_accounts`` asks consumers to drop
    every snapshot (a plan definition changed, or Entitlements restarted with other settings).
    """

    account_id: UUID | None = None
    plan: str | None = Field(default=None, max_length=64)
    status: PlanStatus | None = None
    all_accounts: bool = False
    reason: str | None = Field(default=None, max_length=500)


class PlanSetRequest(ContractModel):
    """Request on ``entitlements.plan.set`` (core NATS, internal): the Billing service sets the
    plan of an account (``source=billing``).

    Core NATS is reachable by services only (clients come through Traefik), so the request
    carries no principal. A ``trial`` without ``trial_until`` lasts the plan's trial days, a
    ``grace`` without ``grace_until`` lasts ``MOWDECK_ENTITLEMENTS_GRACE_DAYS``. Repeating the
    same request changes nothing (no history row, no event).
    """

    account_id: UUID
    plan: str = Field(min_length=1, max_length=64)
    status: PlanStatus
    trial_until: UtcDateTime | None = None
    grace_until: UtcDateTime | None = None
    reason: str = Field(min_length=1, max_length=500)
    account_name: str | None = Field(default=None, max_length=200)
    reference: str | None = Field(
        default=None,
        max_length=200,
        description="Provider reference for the history, e.g. the subscription id.",
    )


class PlanSetError(StrEnum):
    """Why ``entitlements.plan.set`` refused."""

    UNKNOWN_PLAN = "unknown_plan"
    INVALID_REQUEST = "invalid_request"


class PlanSetReply(ContractModel):
    """Reply on ``entitlements.plan.set``: ``ok`` with the plan and its effective status, or an
    ``error`` (not retried by Billing)."""

    ok: bool
    plan: str | None = Field(default=None, max_length=64)
    status: PlanStatus | None = None
    changed: bool = False
    error: PlanSetError | None = None
    detail: str | None = Field(default=None, max_length=500)
