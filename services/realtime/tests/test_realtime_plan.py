"""Plan enforcement in Realtime (ADR 0028): ``drive_start`` needs the feature
``manual_drive``; the refusal is ``drive_not_allowed`` and Fleet is never asked."""

from mowdeck_service_kit.testing import FakeEntitlements
from realtime_support import FakeFleet, harness, publish_capabilities


async def test_realtime_drive_start_needs_the_manual_drive_feature() -> None:
    plans = FakeEntitlements(plan="crafted", features=["view", "control"])
    async with harness(entitlements=plans) as h:
        fleet = FakeFleet()
        await fleet.attach(h.bus)
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await publish_capabilities(h.bus, mower_id, owner.account_id)
        client = await h.connect_as(owner)
        await client.send({"type": "drive_start", "request_id": "p", "mower_id": str(mower_id)})
        error = await client.expect("error", request_id="p")
        assert error["code"] == "drive_not_allowed"
        assert "plan" in error["message"]
        assert fleet.starts == []
        plans.features.append("manual_drive")
        await client.send({"type": "drive_start", "request_id": "q", "mower_id": str(mower_id)})
        started = await client.expect("drive_started", request_id="q")
        assert started["mower_id"] == str(mower_id)
    assert ("feature", "manual_drive") in plans.checks
