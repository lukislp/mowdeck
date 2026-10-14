"""Entitlements contracts: snapshot decisions, request validation, events."""

from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    ALL_FEATURES,
    ENTITLEMENTS_PLAN_CHANGED,
    LIMIT_KEYS,
    DenyReason,
    EntitlementCheckReply,
    EntitlementCheckRequest,
    EntitlementLimitQuery,
    EntitlementSnapshot,
    PlanChanged,
    PlanLimits,
    PlanStatus,
)
from mowdeck_contracts.v1.subjects import ENTITLEMENTS_CHECK, event_subject_for_type
from pydantic import ValidationError


def _snapshot(**values: object) -> EntitlementSnapshot:
    base: dict[str, object] = {
        "account_id": uuid4(),
        "plan": "home",
        "limits": PlanLimits(max_mowers=4, max_sites=1),
        "features": ["control", "notifications", "manual_drive"],
    }
    return EntitlementSnapshot.model_validate({**base, **values})


def test_entitlements_feature_keys_are_hierarchical() -> None:
    snapshot = _snapshot()
    assert snapshot.has_feature("notifications")
    assert snapshot.has_feature("notifications.push")
    assert not snapshot.has_feature("notifications_extra")
    assert not snapshot.has_feature("reports")
    assert snapshot.has_feature("view")
    assert snapshot.has_feature("emergency_stop")


def test_entitlements_wildcard_grants_everything() -> None:
    snapshot = _snapshot(plan="unlimited", features=[ALL_FEATURES], limits=PlanLimits())
    assert snapshot.has_feature("reports.export")
    reply = snapshot.decide(limit=EntitlementLimitQuery(key="max_mowers", current=10_000))
    assert reply.allowed
    assert reply.limit is None


@pytest.mark.parametrize("status", [PlanStatus.GRACE, PlanStatus.EXPIRED])
def test_entitlements_inactive_plans_keep_only_view_and_stop(status: PlanStatus) -> None:
    snapshot = _snapshot(status=status, features=[ALL_FEATURES])
    assert snapshot.has_feature("view")
    assert snapshot.has_feature("emergency_stop")
    assert not snapshot.has_feature("control")
    denied = snapshot.decide(feature="control")
    assert (denied.allowed, denied.reason) == (False, DenyReason.PLAN_INACTIVE)
    limit = snapshot.decide(limit=EntitlementLimitQuery(key="max_mowers", current=0))
    assert (limit.allowed, limit.reason) == (False, DenyReason.PLAN_INACTIVE)


def test_entitlements_limit_decisions() -> None:
    snapshot = _snapshot()
    below = snapshot.decide(limit=EntitlementLimitQuery(key="max_mowers", current=3))
    assert below.allowed and (below.limit, below.current) == (4, 3)
    at = snapshot.decide(limit=EntitlementLimitQuery(key="max_mowers", current=4))
    assert (at.allowed, at.reason, at.limit, at.current) == (
        False,
        DenyReason.LIMIT_REACHED,
        4,
        4,
    )
    unlimited = snapshot.decide(limit=EntitlementLimitQuery(key="max_users", current=99))
    assert unlimited.allowed and unlimited.limit is None


def test_entitlements_feature_and_plan_only_decisions() -> None:
    snapshot = _snapshot(status=PlanStatus.TRIAL)
    missing = snapshot.decide(feature="reports")
    assert (missing.allowed, missing.reason) == (False, DenyReason.FEATURE_NOT_IN_PLAN)
    plan_only = snapshot.decide()
    assert plan_only.allowed and plan_only.plan == "home"
    assert plan_only.status is PlanStatus.TRIAL
    assert plan_only.snapshot == snapshot


def test_entitlements_limits_get_rejects_unknown_keys() -> None:
    limits = PlanLimits(max_groups=2)
    assert [limits.get(k) for k in LIMIT_KEYS].count(None) == len(LIMIT_KEYS) - 1
    with pytest.raises(ValueError, match="unknown limit"):
        limits.get("max_cats")
    with pytest.raises(ValidationError):
        PlanLimits(max_mowers=-1)


def test_entitlements_request_asks_one_question() -> None:
    account_id = uuid4()
    assert EntitlementCheckRequest(account_id=account_id, feature="manual_drive").limit is None
    with pytest.raises(ValidationError, match="either a feature or a limit"):
        EntitlementCheckRequest(
            account_id=account_id,
            feature="manual_drive",
            limit=EntitlementLimitQuery(key="max_mowers", current=1),
        )
    with pytest.raises(ValidationError):
        EntitlementCheckRequest(account_id=account_id, feature="Not A Key")
    with pytest.raises(ValidationError):
        EntitlementLimitQuery.model_validate({"key": "max_cats", "current": 1})


def test_entitlements_reply_round_trip_and_subjects() -> None:
    reply = _snapshot().decide(feature="manual_drive")
    again = EntitlementCheckReply.model_validate_json(reply.model_dump_json())
    assert again == reply
    assert ENTITLEMENTS_CHECK == "entitlements.check"
    assert event_subject_for_type(ENTITLEMENTS_PLAN_CHANGED) == "event.entitlements.plan.changed"
    change = PlanChanged(all_accounts=True, reason="restart")
    assert change.account_id is None and change.all_accounts
