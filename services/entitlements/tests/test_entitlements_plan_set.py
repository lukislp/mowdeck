"""``entitlements.plan.set``: the Billing service sets an account's plan (``source=billing``)."""

from datetime import timedelta
from uuid import uuid4

from entitlements_support import T0, Harness, harness
from mowdeck_contracts.v1 import (
    ENTITLEMENTS_PLAN_CHANGED,
    PlanSetReply,
    PlanSetRequest,
    PlanSource,
    PlanStatus,
)
from mowdeck_contracts.v1.subjects import ENTITLEMENTS_PLAN_SET, event_subject_for_type


async def _set(h: Harness, request: PlanSetRequest) -> PlanSetReply:
    reply = await h.bus.request(ENTITLEMENTS_PLAN_SET, request, timeout=1.0)
    return PlanSetReply.model_validate_json(reply.data)


def _changed(h: Harness) -> int:
    return len(h.bus.messages(event_subject_for_type(ENTITLEMENTS_PLAN_CHANGED)))


async def test_entitlements_plan_set_grace_then_active_with_history() -> None:
    async with harness(entitlements_billing_enabled=True) as h:
        account_id = uuid4()
        before = _changed(h)
        grace_until = T0 + timedelta(days=20)
        reply = await _set(
            h,
            PlanSetRequest(
                account_id=account_id,
                plan="home",
                status=PlanStatus.GRACE,
                grace_until=grace_until,
                reason="stripe customer.subscription.updated (past_due)",
                reference="sub_123",
            ),
        )
        assert (reply.ok, reply.plan, reply.status, reply.changed) == (
            True,
            "home",
            PlanStatus.GRACE,
            True,
        )
        detail = await h.services.entitlements.account(account_id)
        assert detail.resolved.source is PlanSource.BILLING
        assert detail.resolved.row is not None
        assert detail.resolved.row.grace_until == grace_until
        assert detail.history[0].actor == "service:billing:sub_123"
        assert _changed(h) == before + 1

        # The same request again: nothing changes, no history row, no event.
        again = await _set(
            h,
            PlanSetRequest(
                account_id=account_id,
                plan="home",
                status=PlanStatus.GRACE,
                grace_until=grace_until,
                reason="retry",
            ),
        )
        assert (again.ok, again.changed) == (True, False)
        assert len((await h.services.entitlements.account(account_id)).history) == 1
        assert _changed(h) == before + 1

        restored = await _set(
            h,
            PlanSetRequest(
                account_id=account_id, plan="home", status=PlanStatus.ACTIVE, reason="paid"
            ),
        )
        assert (restored.status, restored.changed) == (PlanStatus.ACTIVE, True)
        detail = await h.services.entitlements.account(account_id)
        assert detail.resolved.row is not None
        assert detail.resolved.row.grace_until is None
        assert [e.status for e in detail.history] == [PlanStatus.ACTIVE, PlanStatus.GRACE]


async def test_entitlements_plan_set_defaults_trial_and_grace_ends() -> None:
    async with harness(entitlements_billing_enabled=True, entitlements_grace_days=7) as h:
        trial_account, grace_account = uuid4(), uuid4()
        await _set(
            h,
            PlanSetRequest(
                account_id=trial_account,
                plan="fleet",
                status=PlanStatus.TRIAL,
                reason="trial",
                account_name="Firma",
            ),
        )
        await _set(
            h,
            PlanSetRequest(
                account_id=grace_account, plan="home", status=PlanStatus.GRACE, reason="late"
            ),
        )
        trial = (await h.services.entitlements.account(trial_account)).resolved.row
        grace = (await h.services.entitlements.account(grace_account)).resolved.row
        assert trial is not None
        assert grace is not None
        assert trial.trial_until == T0 + timedelta(days=14)  # fleet: 14 trial days
        assert trial.account_name == "Firma"
        assert grace.grace_until == T0 + timedelta(days=7)
        # The grace period ends as expired.
        h.clock.advance(days=8)
        resolved = await h.services.entitlements.resolve(grace_account)
        assert resolved.snapshot.status is PlanStatus.EXPIRED


async def test_entitlements_plan_set_rejects_unknown_plans_and_invalid_requests() -> None:
    async with harness() as h:
        reply = await _set(
            h,
            PlanSetRequest(account_id=uuid4(), plan="gold", status=PlanStatus.ACTIVE, reason="x"),
        )
        assert (reply.ok, reply.error) == (False, "unknown_plan")
        raw = await h.bus.request(ENTITLEMENTS_PLAN_SET, b'{"plan": 1}', timeout=1.0)
        invalid = PlanSetReply.model_validate_json(raw.data)
        assert (invalid.ok, invalid.error) == (False, "invalid_request")


async def test_entitlements_plan_set_with_billing_disabled_is_stored_but_unlimited() -> None:
    async with harness() as h:
        account_id = uuid4()
        reply = await _set(
            h,
            PlanSetRequest(
                account_id=account_id, plan="home", status=PlanStatus.ACTIVE, reason="x"
            ),
        )
        assert reply.ok
        assert reply.plan == "home"
        detail = await h.services.entitlements.account(account_id)
        assert detail.resolved.snapshot.plan == "unlimited"
        assert detail.resolved.row is not None
        assert detail.resolved.row.plan_key == "home"
