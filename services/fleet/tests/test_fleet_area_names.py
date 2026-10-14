"""Custom area names: the API, its authorization, the event and the names in ``MowerOut``."""

from uuid import uuid4

import pytest
from fleet_support import API, harness
from mowdeck_contracts.v1 import (
    FLEET_AREA_NAMES_CHANGED,
    AreaNamesChanged,
    EventEnvelope,
)

from fleet.adapters.memory import MemoryStore

SUBJECT = f"event.{FLEET_AREA_NAMES_CHANGED}"


async def test_fleet_area_names_set_read_reset_and_publish() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower = await h.register(owner)
        assert mower["area_names"] == {}
        base = f"{API}/mowers/{mower['id']}/area-names"

        first = await h.client.put(
            f"{base}/4525245168006937683", headers=owner.headers, json={"name": "  Vorgarten "}
        )
        assert first.status_code == 200, first.text
        assert first.json() == {"names": {"4525245168006937683": "Vorgarten"}}
        await h.client.put(f"{base}/back", headers=owner.headers, json={"name": "Hinten"})
        renamed = await h.client.put(f"{base}/back", headers=owner.headers, json={"name": "Hof"})
        assert renamed.json()["names"] == {"4525245168006937683": "Vorgarten", "back": "Hof"}

        listed = await h.client.get(base, headers=owner.headers)
        one = await h.client.get(f"{API}/mowers/{mower['id']}", headers=owner.headers)
        every = await h.client.get(f"{API}/mowers", headers=owner.headers)
        assert listed.json()["names"] == renamed.json()["names"]
        assert one.json()["area_names"] == renamed.json()["names"]
        assert every.json()["items"][0]["area_names"] == renamed.json()["names"]

        # Null and blank both drop the override; dropping a missing one changes nothing.
        cleared = await h.client.put(f"{base}/back", headers=owner.headers, json={"name": None})
        assert cleared.json()["names"] == {"4525245168006937683": "Vorgarten"}
        blank = await h.client.put(
            f"{base}/4525245168006937683", headers=owner.headers, json={"name": "   "}
        )
        assert blank.json()["names"] == {}
        again = await h.client.put(f"{base}/back", headers=owner.headers, json={"name": None})
        assert again.status_code == 200

        published = [m.model(EventEnvelope[AreaNamesChanged]) for m in h.bus.messages(SUBJECT)]
        # Five changes; the repeated reset published nothing.
        assert [e.payload.names for e in published] == [
            {"4525245168006937683": "Vorgarten"},
            {"4525245168006937683": "Vorgarten", "back": "Hinten"},
            {"4525245168006937683": "Vorgarten", "back": "Hof"},
            {"4525245168006937683": "Vorgarten"},
            {},
        ]
        assert all(str(e.payload.mower_id) == mower["id"] for e in published)
        assert all(e.account_id == owner.account_id for e in published)
        actions = [
            a.action for a in h.store.tables.audit.rows.values() if a.action.startswith("mower.")
        ]
        assert actions.count("mower.area_renamed") == 5


@pytest.mark.parametrize(
    ("name", "status"),
    [("x" * 60, 200), ("  " + "x" * 60 + "  ", 200), ("x" * 61, 422), ("x" * 201, 422)],
)
async def test_fleet_area_names_length_limit(name: str, status: int) -> None:
    async with harness() as h:
        owner = await h.owner()
        mower = await h.register(owner)
        response = await h.client.put(
            f"{API}/mowers/{mower['id']}/area-names/a1", headers=owner.headers, json={"name": name}
        )
        assert response.status_code == status, response.text


async def test_fleet_area_names_refuse_unknown_fields_and_long_ids() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower = await h.register(owner)
        base = f"{API}/mowers/{mower['id']}/area-names"
        smuggled = await h.client.put(
            f"{base}/a1", headers=owner.headers, json={"name": "x", "account_id": str(uuid4())}
        )
        long_id = await h.client.put(f"{base}/{'a' * 129}", headers=owner.headers, json={})
        missing = await h.client.put(f"{base}/a1", headers=owner.headers, json={})
    assert smuggled.status_code == 422
    assert long_id.status_code == 422
    assert missing.status_code == 422


async def test_fleet_area_names_viewers_read_but_cannot_rename() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower = await h.register(owner)
        base = f"{API}/mowers/{mower['id']}/area-names"
        await h.client.put(f"{base}/a1", headers=owner.headers, json={"name": "Front"})
        viewer = await h.member(owner, "viewer")
        operator = await h.member(owner, "operator")

        seen = await h.client.get(base, headers=viewer.headers)
        refused = await h.client.put(f"{base}/a1", headers=viewer.headers, json={"name": "No"})
        allowed = await h.client.put(f"{base}/a1", headers=operator.headers, json={"name": "Op"})
    assert seen.status_code == 200
    assert seen.json() == {"names": {"a1": "Front"}}
    assert refused.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json() == {"names": {"a1": "Op"}}


async def test_fleet_area_names_are_hidden_from_other_accounts_and_go_with_the_mower() -> None:
    store = MemoryStore()
    async with harness(store=store) as h:
        owner = await h.owner()
        stranger = await h.owner()
        mower = await h.register(owner)
        base = f"{API}/mowers/{mower['id']}/area-names"
        await h.client.put(f"{base}/a1", headers=owner.headers, json={"name": "Front"})

        read = await h.client.get(base, headers=stranger.headers)
        write = await h.client.put(f"{base}/a1", headers=stranger.headers, json={"name": "x"})
        unknown = await h.client.get(f"{API}/mowers/{uuid4()}/area-names", headers=owner.headers)
        assert (read.status_code, write.status_code, unknown.status_code) == (404, 404, 404)

        assert len(store.tables.area_names.rows) == 1
        deleted = await h.client.delete(f"{API}/mowers/{mower['id']}", headers=owner.headers)
        assert deleted.status_code == 204
        assert store.tables.area_names.rows == {}
