"""Billing contracts: plan.set request and reply, the subscription.changed event."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    BILLING_SUBSCRIPTION_CHANGED,
    PlanSetError,
    PlanSetReply,
    PlanSetRequest,
    PlanStatus,
    SubscriptionChanged,
    SubscriptionStatus,
)
from mowdeck_contracts.v1.subjects import (
    ENTITLEMENTS_PLAN_SET,
    event_subject_for_type,
    stream_for_subject,
)
from pydantic import ValidationError


def test_plan_set_request_normalises_dates_to_utc() -> None:
    local = datetime(2026, 11, 1, 10, 0, tzinfo=timezone(timedelta(hours=2)))
    request = PlanSetRequest(
        account_id=uuid4(), plan="home", status=PlanStatus.GRACE, grace_until=local, reason="x"
    )
    assert request.grace_until == datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
    assert request.trial_until is None
    again = PlanSetRequest.model_validate_json(request.model_dump_json())
    assert again == request


def test_plan_set_request_requires_reason_and_aware_dates() -> None:
    with pytest.raises(ValidationError):
        PlanSetRequest(account_id=uuid4(), plan="home", status=PlanStatus.ACTIVE, reason="")
    with pytest.raises(ValidationError):
        PlanSetRequest(
            account_id=uuid4(),
            plan="home",
            status=PlanStatus.TRIAL,
            trial_until=datetime(2026, 1, 1),  # naive on purpose
            reason="x",
        )


def test_plan_set_reply_error() -> None:
    reply = PlanSetReply(ok=False, error=PlanSetError.UNKNOWN_PLAN, detail="no plan gold")
    assert PlanSetReply.model_validate_json(reply.model_dump_json()).error == "unknown_plan"
    assert ENTITLEMENTS_PLAN_SET == "entitlements.plan.set"
    assert stream_for_subject(ENTITLEMENTS_PLAN_SET) is None


def test_subscription_changed_event_subject_and_payload() -> None:
    subject = event_subject_for_type(BILLING_SUBSCRIPTION_CHANGED)
    assert subject == "event.billing.subscription.changed"
    stream = stream_for_subject(subject)
    assert stream is not None
    assert stream.name == "EVENTS"
    change = SubscriptionChanged(
        account_id=uuid4(),
        provider="stripe",
        plan="home",
        status=SubscriptionStatus.PAST_DUE,
        plan_status=PlanStatus.GRACE,
    )
    assert change.cancel_at_period_end is False
    assert SubscriptionChanged.model_validate_json(change.model_dump_json()) == change
    assert {s.value for s in SubscriptionStatus} >= {"trialing", "active", "past_due", "unpaid"}
