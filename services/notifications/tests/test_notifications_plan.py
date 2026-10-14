"""Plan enforcement in Notifications (ADR 0028): Web Push subscriptions need the feature
``notifications.push`` (``notifications`` grants it, keys are hierarchical)."""

from mowdeck_contracts.v1 import PlanStatus
from mowdeck_service_kit.testing import FakeEntitlements
from notifications_support import API, AUTH, P256DH, harness

BODY = {
    "endpoint": "https://push.example.test/s1",
    "keys": {"p256dh": P256DH, "auth": AUTH},
    "expirationTime": None,
}


async def test_notifications_push_subscription_needs_the_push_feature() -> None:
    plans = FakeEntitlements(plan="crafted", features=["view"])
    async with harness(entitlements=plans) as h:
        owner = await h.owner()
        await h.add_mower(owner)
        refused = await h.client.post(f"{API}/push-subscriptions", json=BODY, headers=owner.headers)
        assert refused.status_code == 403
        problem = refused.json()
        assert problem["type"] == "urn:mowdeck:problem:feature-not-in-plan"
        assert problem["errors"][0]["parameter"] == "notifications.push"
        listed = await h.client.get(f"{API}/push-subscriptions", headers=owner.headers)
        assert listed.status_code == 200  # reading stays possible
        plans.features.append("notifications")
        allowed = await h.client.post(f"{API}/push-subscriptions", json=BODY, headers=owner.headers)
        assert allowed.status_code == 201, allowed.text
        plans.status = PlanStatus.EXPIRED
        again = await h.client.post(f"{API}/push-subscriptions", json=BODY, headers=owner.headers)
        assert again.status_code == 403
    assert ("feature", "notifications.push") in plans.checks
