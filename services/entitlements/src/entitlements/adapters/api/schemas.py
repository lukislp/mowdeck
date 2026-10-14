"""Request and response bodies of the Entitlements API."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from mowdeck_contracts.v1 import PlanLimits, PlanSource, PlanStatus
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from entitlements.application.entitlements import AccountDetail, AccountView, Resolved
from entitlements.domain.models import AccountPlan, FeatureEntry, Plan, PlanHistory
from entitlements.domain.planfile import FeatureEntryKey, PlanSpec, check_limit_keys


class PlanOut(BaseModel):
    """A plan as offered."""

    key: str
    name: str
    description: str | None
    limits: PlanLimits = Field(description="Limits; null means unlimited.")
    features: list[str] = Field(description="Feature keys; `*` grants every feature.")
    trial_days: int
    is_default: bool
    active: bool

    @classmethod
    def of(cls, plan: Plan) -> "PlanOut":
        """From the domain plan."""
        return cls(
            key=plan.key,
            name=plan.name,
            description=plan.description,
            limits=plan.limits,
            features=list(plan.features),
            trial_days=plan.trial_days,
            is_default=plan.is_default,
            active=plan.active,
        )


class PlanDetailOut(PlanOut):
    """A plan for operators: also the features no module declares and where it came from."""

    unknown_features: list[str] = Field(
        description="Feature keys of the plan that no module declares (kept, they gate nothing "
        "until a module enforcing them is deployed)."
    )
    from_plan_file: bool = Field(
        description="Seeded from the plan file (a changed file entry "
        "overwrites it at the next start)."
    )
    created_at: datetime | None
    updated_at: datetime | None

    @classmethod
    def detail(cls, plan: Plan, unknown: list[str]) -> "PlanDetailOut":
        """From the domain plan and its unknown features."""
        return cls(
            **PlanOut.of(plan).model_dump(),
            unknown_features=unknown,
            from_plan_file=plan.file_digest is not None,
            created_at=plan.created_at,
            updated_at=plan.updated_at,
        )


class PlanIn(BaseModel):
    """Body of ``PUT /plans/{key}``: the whole plan."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    limits: PlanLimits = Field(default_factory=PlanLimits)
    features: list[FeatureEntryKey] = Field(default_factory=list, max_length=500)
    trial_days: int = Field(default=0, ge=0, le=365)
    is_default: bool = False
    active: bool = True

    _limit_keys = field_validator("limits", mode="before")(check_limit_keys)

    def spec(self, key: str) -> PlanSpec:
        """The plan spec with the key from the path."""
        return PlanSpec.model_validate({"key": key, **self.model_dump()})


class FeatureOut(BaseModel):
    """A feature of the catalogue."""

    key: str
    description: str
    module: str
    retired: bool

    @classmethod
    def of(cls, entry: FeatureEntry) -> "FeatureOut":
        """From the catalogue entry."""
        return cls(
            key=entry.key, description=entry.description, module=entry.module, retired=entry.retired
        )


class ResolvedOut(BaseModel):
    """The plan that applies to an account now."""

    account_id: UUID
    plan: str
    plan_name: str
    status: PlanStatus = Field(description="Effective status (an ended trial is `expired`).")
    source: PlanSource
    billing_enabled: bool = Field(
        description="False: billing is disabled on this installation and every account has the "
        "plan `unlimited`, whatever was assigned."
    )
    limits: PlanLimits
    features: list[str]
    since: datetime | None = None
    trial_until: datetime | None = None
    grace_until: datetime | None = None

    @classmethod
    def of(cls, resolved: Resolved) -> "ResolvedOut":
        """From the resolved plan."""
        snap, row = resolved.snapshot, resolved.row
        assigned = row if snap.billing_enabled else None
        return cls(
            account_id=snap.account_id,
            plan=snap.plan,
            plan_name=resolved.plan.name,
            status=snap.status,
            source=resolved.source,
            billing_enabled=snap.billing_enabled,
            limits=snap.limits,
            features=snap.features,
            since=assigned.since if assigned else None,
            trial_until=assigned.trial_until if assigned else None,
            grace_until=assigned.grace_until if assigned else None,
        )


class AssignedOut(BaseModel):
    """The stored assignment of an account."""

    plan: str
    status: PlanStatus = Field(description="Stored status (time does not change it).")
    source: PlanSource
    since: datetime
    trial_until: datetime | None
    grace_until: datetime | None
    updated_at: datetime

    @classmethod
    def of(cls, row: AccountPlan) -> "AssignedOut":
        """From the row."""
        return cls(
            plan=row.plan_key,
            status=row.status,
            source=row.source,
            since=row.since,
            trial_until=row.trial_until,
            grace_until=row.grace_until,
            updated_at=row.updated_at,
        )


class AccountOut(BaseModel):
    """An account in the operator list: its assignment and what applies (no mower data)."""

    account_id: UUID
    account_name: str | None
    assigned: AssignedOut
    effective: ResolvedOut

    @classmethod
    def of(cls, view: AccountView) -> "AccountOut":
        """From the list view."""
        return cls(
            account_id=view.row.account_id,
            account_name=view.row.account_name,
            assigned=AssignedOut.of(view.row),
            effective=ResolvedOut.of(view.resolved),
        )


class AccountPageOut(BaseModel):
    """A page of accounts (``offset``/``limit``)."""

    items: list[AccountOut]
    total: int
    offset: int
    limit: int
    billing_enabled: bool


class HistoryOut(BaseModel):
    """One assignment."""

    at: datetime
    plan: str
    status: PlanStatus
    source: PlanSource
    reason: str | None
    actor: str | None
    trial_until: datetime | None
    grace_until: datetime | None

    @classmethod
    def of(cls, entry: PlanHistory) -> "HistoryOut":
        """From the history row."""
        return cls(
            at=entry.at,
            plan=entry.plan_key,
            status=entry.status,
            source=entry.source,
            reason=entry.reason,
            actor=entry.actor,
            trial_until=entry.trial_until,
            grace_until=entry.grace_until,
        )


class AccountDetailOut(BaseModel):
    """One account for operators: what applies, the assignment and its history."""

    account_id: UUID
    account_name: str | None
    effective: ResolvedOut
    assigned: AssignedOut | None
    history: list[HistoryOut]

    @classmethod
    def of(cls, detail: AccountDetail) -> "AccountDetailOut":
        """From the detail view."""
        row = detail.resolved.row
        return cls(
            account_id=detail.resolved.snapshot.account_id,
            account_name=row.account_name if row else None,
            effective=ResolvedOut.of(detail.resolved),
            assigned=AssignedOut.of(row) if row else None,
            history=[HistoryOut.of(h) for h in detail.history],
        )


class AssignmentIn(BaseModel):
    """Body of ``PUT /accounts/{id}/plan``."""

    model_config = ConfigDict(extra="forbid")

    plan: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    status: PlanStatus = PlanStatus.ACTIVE
    trial_until: AwareDatetime | None = Field(
        default=None, description="End of a trial; default now plus the plan's trial days."
    )
    grace_until: AwareDatetime | None = Field(
        default=None, description="End of a grace period; default now plus the grace days."
    )
    reason: str = Field(min_length=1, max_length=500, description="Why (kept in the history).")
    account_name: str | None = Field(default=None, max_length=200)
