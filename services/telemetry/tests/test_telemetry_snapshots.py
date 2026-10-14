"""Device snapshots: map and capabilities from device.mower.<id>.*, served per mower."""

from uuid import UUID, uuid4

from mowdeck_authz import RelationshipTuple, account, mower, principal_subject
from mowdeck_contracts.v1 import (
    DockPose,
    GeoReference,
    MapArea,
    MowerCapabilities,
    MowerCapabilitiesSnapshot,
    MowerMap,
    MowerMapSnapshot,
    Principal,
    PrincipalKind,
)
from mowdeck_contracts.v1.subjects import device_subject
from telemetry_support import API, T0, Harness, Person, harness, seconds

from telemetry.adapters.bus.subscriptions import DEVICE_CONSUMER

SQUARE = [(0.0, 0.0), (12.0, 0.0), (12.0, 8.0), (0.0, 8.0)]


def a_map(revision: str | None = "sim-1", name: str = "Front lawn") -> MowerMap:
    return MowerMap(
        reference=GeoReference(origin_lat=48.1, origin_lon=11.5),
        areas=[MapArea(id="front", name=name, boundary=SQUARE)],
        revision=revision,
    )


async def publish_map(
    h: Harness, mower_id: UUID, account_id: UUID, mower_map: MowerMap, at: float = 0
) -> None:
    snapshot = MowerMapSnapshot(
        mower_id=mower_id, account_id=account_id, observed_at=T0 + seconds(at), map=mower_map
    )
    await h.bus.publish(device_subject(mower_id, "map"), snapshot)


async def publish_caps(h: Harness, mower_id: UUID, account_id: UUID) -> None:
    snapshot = MowerCapabilitiesSnapshot(
        mower_id=mower_id,
        account_id=account_id,
        observed_at=T0,
        capabilities=MowerCapabilities(blade_height_range_mm=(20, 100), max_speed_mps=1.0),
    )
    await h.bus.publish(device_subject(mower_id, "capabilities"), snapshot)


async def test_telemetry_serves_the_newest_map_with_an_etag() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await publish_map(h, mower_id, owner.account_id, a_map("sim-2", "New"), at=10)
        await publish_map(h, mower_id, owner.account_id, a_map("sim-1", "Old"), at=0)
        url = f"{API}/mowers/{mower_id}/map"
        first = await h.client.get(url, headers=owner.headers)
        etag = first.headers.get("etag")
        again = await h.client.get(url, headers={**owner.headers, "If-None-Match": etag or ""})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["revision"].startswith("sim-2.")  # the mower's revision plus a content hash
    assert body["map"]["revision"] == "sim-2"
    assert body["map"]["areas"][0]["name"] == "New"  # the older snapshot did not win
    assert etag == f'"{body["revision"]}"'
    assert again.status_code == 304


async def test_telemetry_map_etag_changes_when_the_dock_arrives_with_the_same_revision() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await publish_map(h, mower_id, owner.account_id, a_map("sim-1"), at=0)
        url = f"{API}/mowers/{mower_id}/map"
        first = await h.client.get(url, headers=owner.headers)
        with_dock = a_map("sim-1").model_copy(
            update={"dock": DockPose(x=-0.06, y=0.15, heading_deg=304.0)}
        )
        await publish_map(h, mower_id, owner.account_id, with_dock, at=10)
        etag = first.headers.get("etag") or ""
        again = await h.client.get(url, headers={**owner.headers, "If-None-Match": etag})
    assert first.status_code == 200, first.text
    assert again.status_code == 200, "a changed body must not answer 304"
    assert again.json()["map"]["dock"]["heading_deg"] == 304.0
    assert again.headers.get("etag") != etag


async def test_telemetry_map_without_revision_gets_a_content_hash() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await publish_map(h, mower_id, owner.account_id, a_map(None))
        response = await h.client.get(f"{API}/mowers/{mower_id}/map", headers=owner.headers)
    assert response.status_code == 200
    assert len(response.json()["revision"]) == 32


async def test_telemetry_serves_capabilities() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await publish_caps(h, mower_id, owner.account_id)
        response = await h.client.get(
            f"{API}/mowers/{mower_id}/capabilities", headers=owner.headers
        )
    assert response.status_code == 200, response.text
    caps = response.json()["capabilities"]
    assert caps["blade_height_range_mm"] == [20, 100]
    assert caps["max_speed_mps"] == 1.0


async def test_telemetry_snapshots_are_404_before_the_gateway_reported_them() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        no_map = await h.client.get(f"{API}/mowers/{mower_id}/map", headers=owner.headers)
        no_caps = await h.client.get(f"{API}/mowers/{mower_id}/capabilities", headers=owner.headers)
    assert no_map.status_code == 404
    assert no_map.json()["type"] == "urn:mowdeck:problem:no-map-yet"
    assert no_caps.status_code == 404
    assert no_caps.json()["type"] == "urn:mowdeck:problem:no-capabilities-yet"


async def test_telemetry_snapshots_of_a_foreign_mower_are_hidden() -> None:
    async with harness() as h:
        owner = await h.owner()
        intruder = await h.owner()
        mower_id = await h.add_mower(owner)
        await publish_map(h, mower_id, owner.account_id, a_map())
        await publish_caps(h, mower_id, owner.account_id)
        for suffix in ("map", "capabilities"):
            foreign = await h.client.get(
                f"{API}/mowers/{mower_id}/{suffix}", headers=intruder.headers
            )
            assert foreign.status_code == 404
            assert foreign.json()["type"] == "urn:mowdeck:problem:not-found"


async def test_telemetry_snapshot_naming_another_account_is_dropped() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await publish_map(h, mower_id, uuid4(), a_map())
        response = await h.client.get(f"{API}/mowers/{mower_id}/map", headers=owner.headers)
        assert response.status_code == 404


async def test_telemetry_removing_a_mower_drops_its_snapshots() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await publish_map(h, mower_id, owner.account_id, a_map())
        await h.announce(mower_id, owner.account_id, removed=True)
        assert not any(k[0] == mower_id for k in h.store.data.snapshots)


def test_telemetry_device_consumer_name() -> None:
    assert DEVICE_CONSUMER == "telemetry-device"


async def test_telemetry_capabilities_list_only_visible_mowers_with_capabilities() -> None:
    async with harness() as h:
        owner = await h.owner()
        other = await h.owner()
        mine = [await h.add_mower(owner) for _ in range(2)]
        theirs = await h.add_mower(other)
        await h.add_mower(owner)  # visible, no capabilities yet: absent from the list
        for m in mine:
            await publish_caps(h, m, owner.account_id)
        await publish_caps(h, theirs, other.account_id)
        url = f"{API}/accounts/me/mowers/capabilities"
        everything = await h.client.get(url, headers=owner.headers)
        foreign = await h.client.get(url, headers=other.headers)
        anonymous = await h.client.get(url)
    assert everything.status_code == 200, everything.text
    assert {i["mower_id"] for i in everything.json()["items"]} == {str(m) for m in mine}
    assert everything.json()["items"][0]["capabilities"]["max_speed_mps"] == 1.0
    assert everything.json()["truncated"] is False
    assert [i["mower_id"] for i in foreign.json()["items"]] == [str(theirs)]
    assert anonymous.status_code == 401


async def test_telemetry_capabilities_list_is_empty_without_snapshots() -> None:
    async with harness() as h:
        owner = await h.owner()
        await h.add_mower(owner)
        response = await h.client.get(
            f"{API}/accounts/me/mowers/capabilities", headers=owner.headers
        )
    assert response.json() == {"items": [], "truncated": False}


async def test_telemetry_capabilities_list_is_bounded() -> None:
    async with harness(telemetry_states_limit=2) as h:
        owner = await h.owner()
        for _ in range(3):
            await publish_caps(h, await h.add_mower(owner), owner.account_id)
        response = await h.client.get(
            f"{API}/accounts/me/mowers/capabilities", headers=owner.headers
        )
    assert len(response.json()["items"]) == 2 and response.json()["truncated"] is True


async def test_telemetry_capabilities_list_of_a_subuser_has_only_granted_mowers() -> None:
    async with harness() as h:
        owner = await h.owner()
        mine = [await h.add_mower(owner) for _ in range(2)]
        for m in mine:
            await publish_caps(h, m, owner.account_id)
        sub = Principal(subject=uuid4(), account_id=owner.account_id, kind=PrincipalKind.SUBUSER)
        person = Person(sub, h.keys.mint(sub))
        await h.identity.write(
            [RelationshipTuple(principal_subject(sub), "member", account(owner.account_id))]
        )
        await h.identity.replace_subject_grants(
            principal_subject(sub), mower(mine[0]), ["mower.view"]
        )
        response = await h.client.get(
            f"{API}/accounts/me/mowers/capabilities", headers=person.headers
        )
    assert [i["mower_id"] for i in response.json()["items"]] == [str(mine[0])]
