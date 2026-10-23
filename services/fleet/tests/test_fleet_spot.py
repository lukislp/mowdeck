"""Planner routes in commands: ``start_mow`` with a spot plan and ``spot_mow`` (ADR 0039).
Plan feature ``spot_mowing``, size limit, one mower only, recorded hash; the gates of
``firmware_update``."""

from typing import Any

from fleet_support import API, fake_gateway, harness
from mowdeck_contracts.v1 import SpotMowPlan, SpotSegment
from mowdeck_service_kit.testing import FakeEntitlements

from fleet.domain.rules import SINGLE_MOWER_KINDS, command_features


def _plan(points: int = 4) -> SpotMowPlan:
    path = [(1.0 + i * 0.37, 1.0 + (i % 2) * 0.35) for i in range(points)]
    return SpotMowPlan.build(
        [SpotSegment.from_points(path, blades_on=True, speed_mps=0.4)], ["stripes"]
    )


def _start(plan: SpotMowPlan) -> dict[str, Any]:
    return {"command": {"kind": "start_mow", "spot_plan": plan.model_dump(mode="json")}}


def _spot_mow(plan: SpotMowPlan) -> dict[str, Any]:
    return {
        "command": {
            "kind": "spot_mow",
            "plan": plan.model_dump(mode="json"),
            "speed_mps": 0.2,
            "spot": {"center_wgs84": [48.1, 11.5], "radius_m": 1.5},
        }
    }


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


def test_fleet_command_features_per_kind() -> None:
    assert command_features("emergency_stop") == frozenset()
    assert command_features("start_mow") == {"control"}
    assert command_features("firmware_update") == {"control"}
    assert command_features("set_light") == {"control", "camera"}
    assert command_features("spot_mow") == {"control", "spot_mowing"}
    assert command_features("spot_stop") == {"control"}
    assert command_features("drive_calibrate") == {"control"}
    assert command_features("set_drive_speed") == {"control"}
    assert {"spot_mow", "drive_calibrate", "set_drive_speed"} <= SINGLE_MOWER_KINDS
    assert {"start_mow", "pause_mow", "return_to_dock", "emergency_stop"}.isdisjoint(
        SINGLE_MOWER_KINDS
    )


async def test_fleet_spot_mow_needs_drive_rights_the_feature_and_one_mower() -> None:
    plans = FakeEntitlements(plan="home", features=["view", "control", "emergency_stop"])
    async with harness(entitlements=plans) as h:
        await fake_gateway(h.bus)
        owner = await h.owner()
        operator = await h.member(owner, "operator")
        manager = await h.member(owner, "manager")  # no mower.drive
        mower_id = (await h.register(owner))["id"]
        url = f"{API}/mowers/{mower_id}/commands"
        plan = _plan()
        no_feature = await h.client.post(url, headers=operator.headers, json=_spot_mow(plan))
        plans.features.append("spot_mowing")
        no_drive = await h.client.post(url, headers=manager.headers, json=_spot_mow(plan))
        accepted = await h.client.post(url, headers=operator.headers, json=_spot_mow(plan))
        stop = await h.client.post(
            url, headers=operator.headers, json={"command": {"kind": "spot_stop"}}
        )
        group_id = (await h.create(owner, "groups", name="North"))["id"]
        bulk = await h.client.post(
            f"{API}/groups/{group_id}/commands", headers=owner.headers, json=_spot_mow(plan)
        )
        published = h.commands_published()
        record = await h.client.get(
            f"{API}/commands/{accepted.json()['command_id']}", headers=owner.headers
        )
        stored = await h.store.tables.commands.get(command_id=published[0].command_id)
    assert no_feature.status_code == 403
    assert no_feature.json()["errors"][0]["parameter"] == "spot_mowing"
    assert no_drive.status_code == 403
    assert accepted.status_code == 202, accepted.text
    assert stop.status_code == 202
    assert bulk.status_code == 422 and "one mower" in bulk.json()["detail"]
    assert [c.command.kind for c in published] == ["spot_mow", "spot_stop"]
    assert published[0].command.plan == plan  # type: ignore[union-attr]
    assert record.json()["plan_hash"] == plan.plan_hash
    assert stored is not None and stored.plan_hash == plan.plan_hash
    assert stored.command["plan"]["segments"] == 1 and "path" not in str(stored.command)


async def test_fleet_firmware_update_needs_manage_rights_and_the_control_feature() -> None:
    plans = FakeEntitlements(plan="home", features=["view", "emergency_stop"])
    async with harness(entitlements=plans) as h:
        await fake_gateway(h.bus)
        owner = await h.owner()
        operator = await h.member(owner, "operator")  # controls and drives, does not manage
        mower_id = (await h.register(owner))["id"]
        url = f"{API}/mowers/{mower_id}/commands"
        body = {"command": {"kind": "firmware_update", "version": "1.12.0"}}
        no_feature = await h.client.post(url, headers=owner.headers, json=body)
        plans.features.append("control")
        no_right = await h.client.post(url, headers=operator.headers, json=body)
        accepted = await h.client.post(url, headers=owner.headers, json=body)
        published = h.commands_published()
    assert no_feature.status_code == 403
    assert no_feature.json()["errors"][0]["parameter"] == "control"
    assert no_right.status_code == 403
    assert accepted.status_code == 202, accepted.text
    assert [c.command.kind for c in published] == ["firmware_update"]
