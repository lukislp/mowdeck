"""Virtual zones through the API and the bus (ADR 0070): availability by capability and data,
drawing, the current zone after states, time per zone, the vendor zone link, rights, and the
aerial photo following the zones."""

from typing import Any
from uuid import UUID, uuid4

from mowdeck_authz import RelationshipTuple, account, mower, principal_subject
from mowdeck_contracts.v1 import (
    GeoReference,
    MapArea,
    MowerActivity,
    MowerCapabilities,
    MowerCapabilitiesSnapshot,
    MowerMap,
    MowerMapSnapshot,
    Principal,
    PrincipalKind,
    StartPlan,
    StartPlanKind,
)
from mowdeck_contracts.v1.subjects import device_subject
from mowdeck_control.geo import to_wgs84
from telemetry_support import API, T0, Harness, Person, harness, make_state, seconds

from telemetry.adapters.memory_zones import MemoryZoneStore
from telemetry.application.aerial import FetchedTile

HOME = GeoReference(origin_lat=52.52045, origin_lon=13.40495)


def geo(x: float, y: float) -> list[float]:
    lat, lon = to_wgs84(HOME, x, y)
    return [lat, lon]


def rect(x0: float, y0: float, x1: float, y1: float) -> list[list[float]]:
    return [geo(x0, y0), geo(x1, y0), geo(x1, y1), geo(x0, y1)]


async def publish_map(h: Harness, mower_id: UUID, account_id: UUID, *, areas: bool) -> None:
    body = MowerMap(
        reference=HOME,
        areas=[MapArea(id="a1", boundary=[(0, 0), (5, 0), (5, 5), (0, 5)])] if areas else [],
    )
    await h.bus.publish(
        device_subject(mower_id, "map"),
        MowerMapSnapshot(mower_id=mower_id, account_id=account_id, observed_at=T0, map=body),
    )


async def publish_capabilities(h: Harness, mower_id: UUID, account_id: UUID) -> None:
    caps = MowerCapabilities(
        start_plans=[
            StartPlan(ref="now", kind=StartPlanKind.NOW),
            StartPlan(ref="zone:1", kind=StartPlanKind.ZONE, name="Zone 1"),
            StartPlan(ref="zone:2", kind=StartPlanKind.ZONE, name="Zone 2"),
        ]
    )
    await h.bus.publish(
        device_subject(mower_id, "capabilities"),
        MowerCapabilitiesSnapshot(
            mower_id=mower_id, account_id=account_id, observed_at=T0, capabilities=caps
        ),
    )


async def state_at(
    h: Harness, mower_id: UUID, owner: Person, x: float, y: float, after: float, **extra: Any
) -> None:
    lat, lon = geo(x, y)
    await h.publish_state(
        make_state(mower_id, owner.account_id, at=T0 + seconds(after), position=(lat, lon), **extra)
    )


async def gps_mower(h: Harness, owner: Person) -> UUID:
    """A mower whose vendor shares no map areas but reports positions (a Worx with GPS)."""
    mower_id = await h.add_mower(owner)
    await publish_map(h, mower_id, owner.account_id, areas=False)
    await publish_capabilities(h, mower_id, owner.account_id)
    await state_at(h, mower_id, owner, 5, 5, 0)
    return mower_id


async def viewer(h: Harness, owner: Person, mower_id: UUID) -> Person:
    sub = Principal(subject=uuid4(), account_id=owner.account_id, kind=PrincipalKind.SUBUSER)
    person = Person(sub, h.keys.mint(sub))
    await h.identity.write(
        [RelationshipTuple(principal_subject(sub), "member", account(owner.account_id))]
    )
    await h.identity.replace_subject_grants(principal_subject(sub), mower(mower_id), ["mower.view"])
    return person


async def test_a_gps_mower_without_map_areas_gets_zones_and_the_current_zone() -> None:
    clock_now = [T0 + seconds(1)]
    async with harness(clock=lambda: clock_now[0]) as h:
        owner = await h.owner()
        mower_id = await gps_mower(h, owner)
        base = f"{API}/mowers/{mower_id}/zones"

        empty = (await h.client.get(base, headers=owner.headers)).json()
        assert empty["availability"] == "available"
        assert empty["zones"] == [] and empty["presence"] is None
        assert empty["zone_plans"] == [
            {"ref": "zone:1", "name": "Zone 1"},
            {"ref": "zone:2", "name": "Zone 2"},
        ]
        assert empty["anchor"] == {"lat": HOME.origin_lat, "lon": HOME.origin_lon}
        assert empty["limits"]["max_corners"] == 64

        created = await h.client.post(
            base,
            headers=owner.headers,
            json={"name": "  Front  lawn ", "ring": rect(0, 0, 10, 10), "start_plan_ref": "zone:2"},
        )
        assert created.status_code == 201, created.text
        lawn = created.json()
        assert lawn["name"] == "Front lawn" and lawn["start_plan_ref"] == "zone:2"
        assert 99 < lawn["area_m2"] < 101

        # Drawing re-evaluates the last position at once: the mower stands in the new zone.
        listed = (await h.client.get(base, headers=owner.headers)).json()
        assert listed["presence"]["zone_id"] == lawn["id"]
        assert listed["presence"]["zone_name"] == "Front lawn"

        back = await h.client.post(
            base, headers=owner.headers, json={"name": "Back", "ring": rect(20, 0, 30, 10)}
        )
        assert back.status_code == 201

        # Mowing in the lawn for a minute, then over to the back: time per zone.
        await state_at(h, mower_id, owner, 6, 5, 30, activity=MowerActivity.MOWING)
        await state_at(h, mower_id, owner, 7, 5, 60, activity=MowerActivity.MOWING)
        await state_at(h, mower_id, owner, 25, 5, 90)
        clock_now[0] = T0 + seconds(120)
        now = (await h.client.get(base, headers=owner.headers)).json()
        assert now["presence"]["zone_id"] == back.json()["id"]

        stats = await h.client.get(f"{base}/stats", headers=owner.headers, params={"days": 7})
        assert stats.status_code == 200
        items = {i["name"]: i for i in stats.json()["items"]}
        # 30 s to 90 s: the presence that drawing created counts nothing before the next report
        assert items["Front lawn"]["seconds"] == 60.0
        assert items["Front lawn"]["mowing_seconds"] == 60.0
        assert items["Front lawn"]["entries"] == 1
        assert items["Back"]["entries"] == 1 and items["Back"]["seconds"] == 0.0
        assert items["Front lawn"]["days"] == [
            {"day": T0.date().isoformat(), "seconds": 60.0, "mowing_seconds": 60.0}
        ]


async def test_edit_unlink_and_delete_a_zone() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await gps_mower(h, owner)
        base = f"{API}/mowers/{mower_id}/zones"
        zone = (
            await h.client.post(
                base,
                headers=owner.headers,
                json={"name": "Lawn", "ring": rect(0, 0, 10, 10), "start_plan_ref": "zone:1"},
            )
        ).json()
        renamed = await h.client.patch(
            f"{base}/{zone['id']}",
            headers=owner.headers,
            json={"name": "Beds", "ring": rect(20, 20, 26, 26), "unlink_start_plan": True},
        )
        assert renamed.status_code == 200, renamed.text
        body = renamed.json()
        assert body["name"] == "Beds" and body["start_plan_ref"] is None
        assert 35 < body["area_m2"] < 37
        # The mower at (5, 5) is no longer inside the reshaped zone.
        listed = (await h.client.get(base, headers=owner.headers)).json()
        assert listed["presence"]["zone_id"] is None

        unknown = await h.client.patch(
            f"{base}/{uuid4()}", headers=owner.headers, json={"name": "x"}
        )
        assert unknown.status_code == 404
        assert unknown.json()["type"].endswith("no-zone")
        deleted = await h.client.delete(f"{base}/{zone['id']}", headers=owner.headers)
        assert deleted.status_code == 204
        assert (await h.client.get(base, headers=owner.headers)).json()["zones"] == []
        again = await h.client.delete(f"{base}/{zone['id']}", headers=owner.headers)
        assert again.status_code == 404


async def test_bad_drawings_are_422_with_a_pointer() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await gps_mower(h, owner)
        base = f"{API}/mowers/{mower_id}/zones"
        bow_tie = [geo(0, 0), geo(10, 10), geo(10, 0), geo(0, 10)]
        crossed = await h.client.post(
            base, headers=owner.headers, json={"name": "Tie", "ring": bow_tie}
        )
        assert crossed.status_code == 422
        assert crossed.json()["errors"][0]["pointer"] == "#/ring"
        far = await h.client.post(
            base, headers=owner.headers, json={"name": "Far", "ring": rect(5000, 0, 5010, 10)}
        )
        assert far.status_code == 422
        assert "garden" in far.json()["detail"]
        plan = await h.client.post(
            base,
            headers=owner.headers,
            json={"name": "P", "ring": rect(0, 0, 10, 10), "start_plan_ref": "now"},
        )
        assert plan.status_code == 422
        assert plan.json()["errors"][0]["pointer"] == "#/start_plan_ref"
        short = await h.client.post(
            base, headers=owner.headers, json={"name": "S", "ring": rect(0, 0, 10, 10)[:2]}
        )
        assert short.status_code == 422


async def test_zones_are_not_offered_for_vendor_maps_or_without_positions() -> None:
    async with harness() as h:
        owner = await h.owner()
        mapped = await h.add_mower(owner)
        await publish_map(h, mapped, owner.account_id, areas=True)
        await state_at(h, mapped, owner, 1, 1, 0)
        blind = await h.add_mower(owner)
        await h.publish_state(make_state(blind, owner.account_id, at=T0))

        for mower_id, reason in ((mapped, "vendor_map"), (blind, "no_position")):
            base = f"{API}/mowers/{mower_id}/zones"
            listed = (await h.client.get(base, headers=owner.headers)).json()
            assert listed["availability"] == reason
            refused = await h.client.post(
                base, headers=owner.headers, json={"name": "Z", "ring": rect(0, 0, 3, 3)}
            )
            assert refused.status_code == 409
            assert refused.json()["errors"][0]["code"] == reason


async def test_rights_viewers_read_strangers_see_nothing() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await gps_mower(h, owner)
        base = f"{API}/mowers/{mower_id}/zones"
        zone = (
            await h.client.post(
                base, headers=owner.headers, json={"name": "Lawn", "ring": rect(0, 0, 10, 10)}
            )
        ).json()
        see = await viewer(h, owner, mower_id)
        assert (await h.client.get(base, headers=see.headers)).status_code == 200
        assert (await h.client.get(f"{base}/stats", headers=see.headers)).status_code == 200
        post = await h.client.post(
            base, headers=see.headers, json={"name": "Z", "ring": rect(0, 0, 3, 3)}
        )
        assert post.status_code == 403
        assert (
            await h.client.delete(f"{base}/{zone['id']}", headers=see.headers)
        ).status_code == 403
        stranger = await h.owner()
        assert (await h.client.get(base, headers=stranger.headers)).status_code == 404
        assert (
            await h.client.patch(
                f"{base}/{zone['id']}", headers=stranger.headers, json={"name": "x"}
            )
        ).status_code == 404
        assert (await h.client.get(base)).status_code == 401


async def test_a_mower_has_at_most_32_zones() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await gps_mower(h, owner)
        base = f"{API}/mowers/{mower_id}/zones"
        for i in range(32):
            ok = await h.client.post(
                base,
                headers=owner.headers,
                json={"name": f"Z{i}", "ring": rect(i * 3, 0, i * 3 + 2, 2)},
            )
            assert ok.status_code == 201, ok.text
        full = await h.client.post(
            base, headers=owner.headers, json={"name": "One more", "ring": rect(0, 10, 2, 12)}
        )
        assert full.status_code == 409
        assert full.json()["errors"][0]["code"] == "limit"


class _Source:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def fetch(self, url: str) -> FetchedTile:
        self.urls.append(url)
        return FetchedTile(b"\xff\xd8\xff\xe0" + b"\x00" * 64, "image/jpeg")


async def test_the_aerial_photo_box_grows_with_the_zones() -> None:
    wms: dict[str, Any] = {
        "telemetry_aerial_wms_url": "https://example.test/wms",
        "telemetry_aerial_wms_layer": "photo",
        "telemetry_aerial_attribution": "Test",
    }
    async with harness(aerial_source=_Source(), **wms) as h:
        owner = await h.owner()
        mower_id = await gps_mower(h, owner)
        before = (
            await h.client.get(f"{API}/mowers/{mower_id}/aerial", headers=owner.headers)
        ).json()["bounds"]
        created = await h.client.post(
            f"{API}/mowers/{mower_id}/zones",
            headers=owner.headers,
            json={"name": "Far corner", "ring": rect(300, 300, 320, 320)},
        )
        assert created.status_code == 201
        after = (
            await h.client.get(f"{API}/mowers/{mower_id}/aerial", headers=owner.headers)
        ).json()["bounds"]
    far_lat, far_lon = geo(320, 320)
    assert before["north"] < far_lat
    assert after["north"] > far_lat and after["east"] > far_lon
    assert after["south"] == before["south"]


async def test_ingestion_survives_a_failing_zone_store() -> None:
    class Broken(MemoryZoneStore):
        async def zones_for(self, mower_id: UUID) -> list[Any]:
            raise RuntimeError("database down")

    async with harness(zone_store=Broken()) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await state_at(h, mower_id, owner, 1, 1, 0)
        state = await h.client.get(f"{API}/mowers/{mower_id}/state", headers=owner.headers)
    assert state.status_code == 200
