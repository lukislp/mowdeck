"""Plan enforcement in the Scheduler (ADR 0028): the feature ``schedules`` and the limit
``max_schedules`` on create."""

from mowdeck_contracts.v1 import PlanLimits
from mowdeck_service_kit.testing import FakeEntitlements
from scheduler_support import harness


async def test_scheduler_plan_without_schedules_refuses_create() -> None:
    plans = FakeEntitlements(plan="crafted", features=["view", "control"])
    async with harness(entitlements=plans) as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        response = await h.create(owner, target_type="mower", target_id=str(mower_id))
        assert response.status_code == 403
        assert response.json()["type"] == "urn:mowdeck:problem:feature-not-in-plan"
        plans.features.append("schedules")
        await h.schedule(owner, target_type="mower", target_id=str(mower_id))
    assert ("feature", "schedules") in plans.checks


async def test_scheduler_plan_limit_on_schedules_answers_409() -> None:
    plans = FakeEntitlements(
        plan="home", features=["schedules"], limits=PlanLimits(max_schedules=1)
    )
    async with harness(entitlements=plans) as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        await h.schedule(owner, target_type="mower", target_id=str(mower_id))
        response = await h.create(
            owner, name="Evening", target_type="mower", target_id=str(mower_id)
        )
        assert response.status_code == 409
        problem = response.json()
        assert problem["type"] == "urn:mowdeck:problem:plan-limit"
        assert {(e["code"], e["detail"]) for e in problem["errors"]} == {
            ("limit", "1"),
            ("current", "1"),
        }
