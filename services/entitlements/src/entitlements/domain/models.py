"""Plans, account plans, the history of assignments and the feature catalogue."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import (
    ALL_FEATURES,
    UNLIMITED_PLAN,
    PlanLimits,
    PlanSource,
    PlanStatus,
)


@dataclass(slots=True)
class Plan:
    """A plan as data: limits (``None`` = unlimited) and feature keys.

    ``active`` plans are offered (``GET /plans``); inactive plans keep applying to accounts that
    hold them. ``file_digest`` is the digest of the plan file entry it was last seeded from:
    a restart overwrites the plan only when that entry changed, so an operator's edit through
    the API survives restarts until the file entry itself changes.
    """

    key: str
    name: str
    limits: PlanLimits = field(default_factory=PlanLimits)
    features: list[str] = field(default_factory=list)
    trial_days: int = 0
    is_default: bool = False
    active: bool = True
    description: str | None = None
    file_digest: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def limits_json(self) -> dict[str, Any]:
        """The limits as a JSON object (only the set ones)."""
        return self.limits.model_dump(exclude_none=True)


def unlimited_plan() -> Plan:
    """The built-in plan ``unlimited``: no limits, every feature (billing disabled)."""
    return Plan(
        key=UNLIMITED_PLAN,
        name="Unlimited",
        description="No limits and every feature; used while billing is disabled.",
        features=[ALL_FEATURES],
    )


@dataclass(slots=True)
class AccountPlan:
    """The plan assigned to one account (no row: the default plan applies)."""

    account_id: UUID
    plan_key: str
    status: PlanStatus
    since: datetime
    source: PlanSource
    updated_at: datetime
    account_name: str | None = None
    trial_until: datetime | None = None
    grace_until: datetime | None = None

    def __post_init__(self) -> None:
        self.status = PlanStatus(self.status)
        self.source = PlanSource(self.source)


@dataclass(slots=True)
class PlanHistory:
    """One assignment of a plan (the audit row of ``PUT /accounts/{id}/plan``)."""

    id: UUID
    account_id: UUID
    at: datetime
    plan_key: str
    status: PlanStatus
    source: PlanSource
    reason: str | None = None
    actor: str | None = None
    trial_until: datetime | None = None
    grace_until: datetime | None = None

    def __post_init__(self) -> None:
        self.status = PlanStatus(self.status)
        self.source = PlanSource(self.source)


@dataclass(slots=True)
class FeatureEntry:
    """A feature key declared by a module descriptor (``features``)."""

    key: str
    description: str
    module: str
    first_seen_at: datetime
    last_seen_at: datetime
    retired: bool = False
    """No module declares the key any more (kept, so plans that list it stay readable)."""
