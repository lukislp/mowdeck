"""HTTP API of the Planner: patterns, previews, plans and the start through Fleet."""

from typing import Any
from uuid import uuid4

import pytest
from mowdeck_authz import mower
from mowdeck_contracts.v1 import (
    MODULE_DESCRIPTOR_PATH,
    PathOrder,
    SpotMowPlan,
    StartMow,
    StartOptions,
)
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
    # border_fill asks for the border first; a mower without a path order choice keeps its own.
    assert "order" not in native.json()["execution"]["native_job"]
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


async def test_planner_shapes_lane_ends_for_a_turn_style() -> None:
    """ADR 0043: arcs or three-point turns inside the spot run's geofence; multi-point falls
    back to arcs for a mower whose drive link cannot reverse."""
    from shapely.geometry import LineString

    from planner.application.planning import spot_geofence

    # a spot in the corner of `front`: the geofence (0.3 m from the map's edge) binds there
    spot = {"area_id": "front", "polygon": [(0, 0), (5, 0), (5, 3), (0, 3)]}
    async with harness() as h:
        owner = await h.owner()
        sim = str(await h.mower(owner))
        luba = str(await h.mower(owner, LUBA_LIKE))
        plain = (await h.preview(owner, mower_id=sim, area=spot, chain=STRIPES)).json()
        arc = await h.preview(owner, mower_id=sim, area=spot, chain=STRIPES, turn_style="arc")
        multi = await h.preview(
            owner, mower_id=sim, area=spot, chain=STRIPES, turn_style="multi_point"
        )
        fallback = await h.preview(
            owner, mower_id=luba, area=spot, chain=STRIPES, turn_style="multi_point"
        )
    assert arc.status_code == 200, arc.text
    assert plain["turn_style"] is None
    fence = spot_geofence(default_map()).buffer(1e-3)

    def turns(body: dict[str, Any]) -> list[dict[str, Any]]:
        return [f for f in body["preview"]["features"] if f["properties"]["kind"] == "turn"]

    arc_body, multi_body, fallback_body = arc.json(), multi.json(), fallback.json()
    assert arc_body["turn_style"] == "arc"
    assert arc_body["plan_hash"] != plain["plan_hash"]

    def shaped(body: dict[str, Any]) -> list[dict[str, Any]]:
        return [f for f in turns(body) if len(f["geometry"]["coordinates"]) > 2]

    assert shaped(arc_body)
    assert not any(f["properties"]["reverse"] for f in turns(arc_body))
    for feature in shaped(arc_body) + shaped(multi_body):
        assert fence.covers(LineString(feature["geometry"]["coordinates"]))
    # the lowest lane lies within 0.3 m of the map's edge: its lane end keeps turning in place
    assert any("turn in place" in w for w in arc_body["warnings"])
    assert multi_body["turn_style"] == "multi_point"
    reverse = [f for f in turns(multi_body) if f["properties"]["reverse"]]
    assert reverse
    assert all(not f["properties"]["blades_on"] for f in reverse)
    assert fallback_body["turn_style"] == "arc"
    assert not any(f["properties"]["reverse"] for f in turns(fallback_body))
    assert any("cannot reverse" in w for w in fallback_body["warnings"])


async def test_planner_native_job_keeps_the_order_a_mower_offers() -> None:
    offers_order = LUBA_LIKE.model_copy(
        update={"start_options": StartOptions(orders=[PathOrder.BORDER_FIRST])}
    )
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.mower(owner, offers_order)
        native = await h.preview(
            owner,
            mower_id=str(mower_id),
            area={"area_id": "front"},
            chain=[{"pattern": "border_fill", "settings": {"laps": 2}}],
        )
    job = native.json()["execution"]["native_job"]
    assert (job["border_laps"], job["order"]) == (2, "border_first")
