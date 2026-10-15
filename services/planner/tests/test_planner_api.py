"""HTTP API of the Planner: patterns, previews, plans and the start through Fleet."""

from typing import Any
from uuid import uuid4

import pytest
from mowdeck_authz import mower
from mowdeck_contracts.v1 import MODULE_DESCRIPTOR_PATH, SpotMowPlan, StartMow
from mowdeck_control import default_map, plan_transit, to_wgs84
from mowdeck_service_kit.testing import FakeEntitlements
from planner_support import API, FLEET, LUBA_LIKE, harness

from planner.module import DESCRIPTOR, SPOT_MOWING_FEATURE, VIEW_PERMISSION

STRIPES: list[dict[str, Any]] = [{"pattern": "stripes", "settings": {"angle_deg": 0}}]


async def test_planner_health_descriptor_and_patterns() -> None:
    async with harness() as h:
        owner = await h.owner()
        assert (await h.client.get("/healthz")).json() == {"status": "ok"}
        assert (await h.client.get("/readyz")).status_code == 200
        descriptor = (await h.client.get(MODULE_DESCRIPTOR_PATH)).json()
        anonymous = await h.client.get(f"{API}/patterns")
        listed = await h.client.get(f"{API}/patterns", headers=owner.headers)
    assert descriptor == DESCRIPTOR.model_dump(mode="json")
    assert descriptor["features"][0]["key"] == SPOT_MOWING_FEATURE
    assert descriptor["permissions"][0]["key"] == VIEW_PERMISSION
    assert anonymous.status_code == 401
    items = {item["key"]: item for item in listed.json()["items"]}
    assert {"stripes", "spiral", "remow", "cells", "border_fill"} <= set(items)
    assert items["stripes"]["native"] == "stripes"
    assert items["spiral"]["native"] is None
    assert items["stripes"]["settings_schema"]["properties"]["angle_deg"]["type"] == "number"


async def test_planner_preview_for_a_simulated_mower() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.mower(owner)
        response = await h.preview(
            owner, mower_id=str(mower_id), area={"area_id": "front"}, chain=STRIPES
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pattern_chain"] == ["stripes"]
    assert body["execution"]["mode"] == "custom_paths"
    assert body["execution"]["native_job"]["name"] == "stripes"
    assert body["coverage_ratio"] > 0.95
    assert body["area_m2"] == 88.5
    assert body["mower"]["spacing_m"] == 0.35
    assert body["preview"]["type"] == "FeatureCollection"
    assert body["preview"]["properties"]["plan_hash"] == body["plan_hash"]
    kinds = {f["properties"]["kind"] for f in body["preview"]["features"]}
    assert kinds <= {"mow", "turn", "transfer"}


async def test_planner_preview_with_a_wgs84_spot_and_wgs84_output() -> None:
    reference = default_map().reference
    spot = [to_wgs84(reference, x, y) for x, y in [(1, 1), (6, 1), (6, 4), (1, 4)]]
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.mower(owner)
        response = await h.preview(
            owner,
            mower_id=str(mower_id),
            area={"polygon_wgs84": spot},
            chain=[{"pattern": "spiral"}],
            wgs84=True,
        )
        outside = await h.preview(
            owner,
            mower_id=str(mower_id),
            area={"polygon": [(-5, -5), (-1, -5), (-1, -1)]},
            chain=STRIPES,
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert 14 < body["area_m2"] < 16
    assert body["execution"]["native_job"] is None
    lon, lat = body["preview"]["features"][0]["geometry"]["coordinates"][0]
    assert abs(lat - reference.origin_lat) < 0.001 and abs(lon - reference.origin_lon) < 0.001
    assert outside.status_code == 422
    assert "inside the mower's mapped areas" in outside.json()["detail"]


async def test_planner_preview_of_a_raw_map_without_a_mower() -> None:
    async with harness() as h:
        owner = await h.owner()
        response = await h.preview(
            owner,
            map=default_map().model_dump(mode="json"),
            area={"area_id": "back"},
            chain=[{"pattern": "edge_only", "settings": {"laps": 2}}, {"pattern": "diagonal"}],
        )
        polygon_only = await h.preview(
            owner, area={"polygon": [(0, 0), (8, 0), (8, 5), (0, 5)]}, chain=STRIPES
        )
    assert response.status_code == 200, response.text
    assert response.json()["execution"]["mode"] == "unknown"
    assert response.json()["pattern_chain"] == ["edge_only", "diagonal"]
    assert polygon_only.status_code == 200, polygon_only.text


SPOT_IN_BACK = {"area_id": "back", "polygon": [(4, 13), (6, 13), (6, 15), (4, 15)]}


async def test_planner_preview_plans_the_transit_like_the_gateway() -> None:
    """The preview's transit is the run's: the same pure planner on the same map and start."""
    garden = default_map()
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.mower(owner)
        docked = await h.preview(
            owner,
            mower_id=str(mower_id),
            area=SPOT_IN_BACK,
            chain=[{"pattern": "spiral"}],
            transit={"from_dock": True},
        )
        on_lawn = await h.preview(
            owner,
            mower_id=str(mower_id),
            area=SPOT_IN_BACK,
            chain=[{"pattern": "spiral"}],
            transit={"position": [6.0, 4.0]},
        )
        without = await h.preview(
            owner, mower_id=str(mower_id), area=SPOT_IN_BACK, chain=[{"pattern": "spiral"}]
        )
        bad = await h.preview(
            owner,
            mower_id=str(mower_id),
            area=SPOT_IN_BACK,
            chain=[{"pattern": "spiral"}],
            transit={"from_dock": True, "position": [1.0, 1.0]},
        )
    assert docked.status_code == 200, docked.text
    transit = docked.json()["transit"]
    assert transit["found"] and transit["from_dock"] and transit["reason"] is None
    assert garden.dock is not None
    assert transit["route"][0] == [garden.dock.x, garden.dock.y]
    first = docked.json()["preview"]["features"][0]["geometry"]["coordinates"][0]
    assert transit["route"][-1] == pytest.approx(first, abs=0.01)
    expected = plan_transit(garden, (garden.dock.x, garden.dock.y), tuple(first))
    assert transit["length_m"] == pytest.approx(expected.length_m, abs=0.02)
    assert transit["length_m"] > 10  # through the side path to the back lawn
    assert on_lawn.json()["transit"]["found"] and not on_lawn.json()["transit"]["from_dock"]
    assert without.json()["transit"] is None
    assert bad.status_code == 422


async def test_planner_preview_reports_no_route() -> None:
    unconnected = default_map().model_copy(update={"paths": []})
    async with harness() as h:
        owner = await h.owner()
        response = await h.preview(
            owner,
            map=unconnected.model_dump(mode="json"),
            area=SPOT_IN_BACK,
            chain=[{"pattern": "spiral"}],
            transit={"from_dock": True},
        )
    assert response.status_code == 200, response.text
    transit = response.json()["transit"]
    assert not transit["found"] and transit["route"] == []
    assert transit["reason"].startswith("no_route:")


async def test_planner_preview_errors() -> None:
    async with harness() as h:
        owner = await h.owner()
        other = await h.owner()
        mower_id = await h.mower(owner)
        unmapped = await h.register(owner)
        base: dict[str, Any] = {"mower_id": str(mower_id), "area": {"area_id": "front"}}
        foreign = await h.preview(other, **base, chain=STRIPES)
        no_map = await h.preview(
            owner, mower_id=str(unmapped), area={"area_id": "front"}, chain=STRIPES
        )
        bad_settings = await h.preview(
            owner, **base, chain=[{"pattern": "stripes", "settings": {"x": 1}}]
        )
        unknown = await h.preview(owner, **base, chain=[{"pattern": "waves"}])
        unknown_area = await h.preview(
            owner, mower_id=str(mower_id), area={"area_id": "moon"}, chain=STRIPES
        )
        rejected = await h.preview(owner, **base, chain=STRIPES, mower={"turning_radius_m": 0.5})
        both = await h.preview(owner, **base, chain=STRIPES, template="x")
        bad_spec = await h.preview(owner, **base, chain=STRIPES, mower={"overlap_m": 0.5})
    assert foreign.status_code == 404
    assert no_map.status_code == 409
    assert bad_settings.status_code == 422
    assert bad_settings.json()["errors"][0]["detail"].startswith("x:")
    assert unknown.status_code == 422 and "waves" in unknown.json()["detail"]
    assert unknown_area.status_code == 422
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "the plan failed the validation chain"
    assert "turning radius" in rejected.json()["errors"][0]["detail"]
    assert both.status_code == 422
    assert bad_spec.status_code == 422


async def test_planner_plan_starts_through_fleet_with_the_spot_plan() -> None:
    async with harness() as h:
        owner = await h.owner()
        viewer = await h.member(owner, "viewer")
        mower_id = await h.mower(owner)
        body = {"mower_id": str(mower_id), "area": {"area_id": "front"}, "chain": STRIPES}
        refused = await h.client.post(f"{API}/plans", headers=viewer.headers, json=body)
        allowed_preview = await h.preview(viewer, **body)
        created = await h.client.post(f"{API}/plans", headers=owner.headers, json=body)
        again = await h.client.post(f"{API}/plans", headers=owner.headers, json=body)
        assert created.status_code == 200, created.text
        plan = created.json()
        started = await h.fleet_client.post(
            f"{FLEET}/mowers/{mower_id}/commands",
            headers=owner.headers,
            json={"command": plan["command"]},
        )
        assert started.status_code == 202, started.text
        record = await h.fleet_client.get(
            f"{FLEET}/commands/{started.json()['command_id']}", headers=owner.headers
        )
    assert refused.status_code == 403
    assert allowed_preview.status_code == 200
    spot = SpotMowPlan.model_validate(plan["spot_plan"])
    assert spot.plan_hash == plan["plan_hash"] == again.json()["plan_hash"]
    assert spot.native is not None and spot.native.area_ids == ["front"]
    [envelope] = h.commands
    assert isinstance(envelope.command, StartMow)
    assert envelope.command.spot_plan == spot
    assert record.json()["plan_hash"] == spot.plan_hash


async def test_planner_execution_modes_on_a_mower_without_custom_paths() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.mower(owner, LUBA_LIKE)
        native = await h.preview(
            owner,
            mower_id=str(mower_id),
            area={"area_id": "front"},
            chain=[{"pattern": "border_fill", "settings": {"laps": 1}}],
        )
        spot = await h.preview(
            owner,
            mower_id=str(mower_id),
            area={"area_id": "front", "polygon": [(1, 1), (5, 1), (5, 4), (1, 4)]},
            chain=STRIPES,
        )
        spiral = await h.preview(
            owner, mower_id=str(mower_id), area={"area_id": "front"}, chain=[{"pattern": "spiral"}]
        )
    assert native.json()["execution"]["mode"] == "native"
    assert native.json()["execution"]["native_job"]["border_laps"] == 1
    assert spot.json()["execution"]["mode"] == "unsupported"
    assert spiral.json()["execution"]["mode"] == "unsupported"


async def test_planner_needs_the_spot_mowing_feature() -> None:
    plans = FakeEntitlements(plan="home", features=["view", "control", "emergency_stop"])
    async with harness(entitlements=plans) as h:
        owner = await h.owner()
        mower_id = await h.mower(owner)
        response = await h.preview(
            owner, mower_id=str(mower_id), area={"area_id": "front"}, chain=STRIPES
        )
        listed = await h.client.get(f"{API}/patterns", headers=owner.headers)
    assert response.status_code == 403
    assert response.json()["type"] == "urn:mowdeck:problem:feature-not-in-plan"
    assert listed.status_code == 200


async def test_planner_size_limits() -> None:
    async with harness(planner_max_vertices=10, planner_max_plan_bytes=2048) as h:
        owner = await h.owner()
        mower_id = await h.mower(owner)
        ring = [(1 + i * 0.1, 1 + (i % 2) * 0.1) for i in range(12)]
        many = await h.preview(owner, area={"polygon": ring}, chain=STRIPES)
        big = await h.preview(
            owner, mower_id=str(mower_id), area={"area_id": "front"}, chain=STRIPES
        )
    assert many.status_code == 422 and "vertices" in many.json()["detail"]
    assert big.status_code == 422 and "split the area" in big.json()["detail"]


async def test_planner_hides_mowers_of_other_accounts_and_unknown_ones() -> None:
    async with harness() as h:
        owner = await h.owner()
        response = await h.preview(
            owner, mower_id=str(uuid4()), area={"area_id": "front"}, chain=STRIPES
        )
    assert response.status_code == 404
    assert mower(uuid4()).type == "mower"
