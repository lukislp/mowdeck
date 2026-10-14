"""Status rules: a provider subscription status becomes a plan status in Entitlements.

=========================================  ===============================================
Provider status                            Plan status in Entitlements
=========================================  ===============================================
``trialing``                               ``trial`` until the trial end
``active``                                 ``active``
``past_due``, ``unpaid``                   ``grace`` until paid period end + grace days
                                           (``unpaid``: ``expired`` once that has passed)
``canceled``, ``incomplete_expired``,      ``grace`` until end + grace days while that lies
``paused``                                 ahead, else ``expired``
``incomplete``                             unchanged (the first payment is still pending)
=========================================  ===============================================

In ``grace`` and ``expired`` only viewing and the emergency stop remain (ADR 0028); a mower is
never stopped because of a payment problem.

"Period end" is the end of the last **paid** period: when a renewal fails the provider has already
moved ``current_period_end`` to the end of the unpaid period, so for ``past_due`` and ``unpaid``
the anchor is ``current_period_start`` (the old period end). For ended subscriptions it is
``ended_at`` when the provider reports it, else ``current_period_end``.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from mowdeck_contracts.v1 import PlanStatus, SubscriptionStatus


@dataclass(frozen=True, slots=True)
class PlanDecision:
    """What Billing sets in Entitlements for a subscription."""

    status: PlanStatus
    trial_until: datetime | None = None
    grace_until: datetime | None = None


def plan_decision(
    status: SubscriptionStatus,
    *,
    now: datetime,
    grace_days: int,
    current_period_start: datetime | None = None,
    current_period_end: datetime | None = None,
    ended_at: datetime | None = None,
    trial_end: datetime | None = None,
) -> PlanDecision | None:
    """The plan status for a provider status, or None when the plan stays as it is."""
    if status is SubscriptionStatus.INCOMPLETE:
        return None
    if status is SubscriptionStatus.TRIALING:
        return PlanDecision(PlanStatus.TRIAL, trial_until=trial_end)
    if status is SubscriptionStatus.ACTIVE:
        return PlanDecision(PlanStatus.ACTIVE)
    if status in (SubscriptionStatus.PAST_DUE, SubscriptionStatus.UNPAID):
        anchor = current_period_start or current_period_end
    else:
        anchor = ended_at or current_period_end
    grace_until = (anchor or now) + timedelta(days=grace_days)
    if status is SubscriptionStatus.PAST_DUE or grace_until > now:
        return PlanDecision(PlanStatus.GRACE, grace_until=grace_until)
    return PlanDecision(PlanStatus.EXPIRED)
