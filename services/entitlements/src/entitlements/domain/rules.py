"""Plan resolution: which plan and status apply to an account at a moment."""

from datetime import datetime, timedelta
from uuid import UUID

from mowdeck_contracts.v1 import (
    ALL_FEATURES,
    LIMIT_KEYS,
    EntitlementSnapshot,
    PlanLimits,
    PlanSource,
    PlanStatus,
)

from entitlements.domain.models import AccountPlan, Plan


def effective_status(row: AccountPlan, now: datetime) -> PlanStatus:
    """The status at ``now``: a trial or grace period that has ended counts as ``expired``.

    The stored status is never rewritten by time; the transition is computed, so a clock change
    or a late job cannot get it wrong.
    """
    if row.status is PlanStatus.TRIAL and row.trial_until is not None and now >= row.trial_until:
        return PlanStatus.EXPIRED
    if row.status is PlanStatus.GRACE and row.grace_until is not None and now >= row.grace_until:
        return PlanStatus.EXPIRED
    return row.status


def unknown_features(plan: Plan, known: set[str]) -> list[str]:
    """Feature keys of ``plan`` that no module declares (``*`` is always known)."""
    return sorted(f for f in set(plan.features) if f != ALL_FEATURES and f not in known)


def snapshot(
    account_id: UUID,
    plan: Plan,
    status: PlanStatus,
    *,
    billing_enabled: bool,
) -> EntitlementSnapshot:
    """The snapshot the API and ``entitlements.check`` answer with."""
    return EntitlementSnapshot(
        account_id=account_id,
        plan=plan.key,
        status=status,
        limits=plan.limits,
        features=sorted(set(plan.features)),
        billing_enabled=billing_enabled,
    )


def deny_all_plan(key: str) -> Plan:
    """What applies when the configured default plan does not exist: nothing but view and stop
    (the snapshot keeps them anyway), so a broken plan file never opens everything up."""
    limits = PlanLimits.model_validate(dict.fromkeys(LIMIT_KEYS, 0))
    return Plan(key=key, name=key, limits=limits, features=[])


def initial_status(plan: Plan, now: datetime) -> tuple[PlanStatus, datetime | None]:
    """Status of a new account on ``plan``: a trial of ``trial_days`` when the plan has one."""
    if plan.trial_days > 0:
        return PlanStatus.TRIAL, now + timedelta(days=plan.trial_days)
    return PlanStatus.ACTIVE, None


def default_row(account_id: UUID, plan: Plan, now: datetime) -> AccountPlan:
    """The row of an account that has none yet (``source=default``)."""
    status, trial_until = initial_status(plan, now)
    return AccountPlan(
        account_id=account_id,
        plan_key=plan.key,
        status=status,
        since=now,
        source=PlanSource.DEFAULT,
        updated_at=now,
        trial_until=trial_until,
    )
