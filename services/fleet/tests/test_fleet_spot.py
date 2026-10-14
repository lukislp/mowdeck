"""``start_mow`` with a spot plan: plan feature ``spot_mowing``, size limit, recorded hash."""

from typing import Any

from fleet_support import API, fake_gateway, harness
from mowdeck_contracts.v1 import SpotMowPlan, SpotSegment
from mowdeck_service_kit.testing import FakeEntitlements


def _plan(points: int = 4) -> SpotMowPlan:
    path = [(1.0 + i * 0.37, 1.0 + (i % 2) * 0.35) for i in range(points)]
    return SpotMowPlan.build(
        [SpotSegment.from_points(path, blades_on=True, speed_mps=0.4)], ["stripes"]
    )


def _start(plan: SpotMowPlan) -> dict[str, Any]:
    return {"command": {"kind": "start_mow", "spot_plan": plan.model_dump(mode="json")}}


async def test_fleet_spot_plan_is_published_and_its_hash_recorded() -> None:
    async with harness() as h:
        await fake_gateway(h.bus)
        owner = await h.owner()
        mower_id = (await h.register(owner))["id"]
        plan = _plan()
        response = await h.client.post(
            f"{API}/mowers/{mower_id}/commands", headers=owner.headers, json=_start(plan)
        )
        assert response.status_code == 202, response.text
        command_id = response.json()["command_id"]
        record = await h.client.get(f"{API}/commands/{command_id}", headers=owner.headers)
        published = h.commands_published()
        stored = await h.store.tables.commands.get(command_id=published[0].command_id)
    assert record.json()["plan_hash"] == plan.plan_hash
    assert published[0].command.spot_plan == plan  # type: ignore[union-attr]
    assert stored is not None
    assert stored.plan_hash == plan.plan_hash
    assert stored.command["spot_plan"]["segments"] == 1
    assert "path" not in str(stored.command)


async def test_fleet_spot_plan_needs_the_spot_mowing_feature() -> None:
    plans = FakeEntitlements(plan="home", features=["view", "control", "emergency_stop"])
    async with harness(entitlements=plans) as h:
        owner = await h.owner()
        mower_id = (await h.register(owner))["id"]
        refused = await h.client.post(
            f"{API}/mowers/{mower_id}/commands", headers=owner.headers, json=_start(_plan())
        )
        assert refused.status_code == 403
        assert refused.json()["errors"][0]["parameter"] == "spot_mowing"
        plain = await h.client.post(
            f"{API}/mowers/{mower_id}/commands",
            headers=owner.headers,
            json={"command": {"kind": "start_mow", "area_ids": ["front"]}},
        )
        assert plain.status_code == 202


async def test_fleet_refuses_oversized_spot_plans_and_bulk_spot_plans() -> None:
    async with harness(spot_plan_max_bytes=1024) as h:
        owner = await h.owner()
        mower_id = (await h.register(owner))["id"]
        big = await h.client.post(
            f"{API}/mowers/{mower_id}/commands", headers=owner.headers, json=_start(_plan(400))
        )
        group_id = (await h.create(owner, "groups", name="North"))["id"]
        bulk = await h.client.post(
            f"{API}/groups/{group_id}/commands", headers=owner.headers, json=_start(_plan())
        )
        assert h.commands_published() == []
    assert big.status_code == 422
    assert "at most 1024" in big.json()["detail"]
    assert bulk.status_code == 422
    assert "one mower" in bulk.json()["detail"]
