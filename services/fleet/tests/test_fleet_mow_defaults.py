"""Mowing defaults per mower: the API, its authorization, the audit and ``MowerOut``."""

from uuid import uuid4

import pytest
from fleet_support import API, harness
from mowdeck_contracts.v1 import MowPattern

from fleet.adapters.memory import MemoryStore
from fleet.domain.models import MowDefaults

DIAGONAL = {"name": "diagonal", "angle_deg": 45, "border_laps": 2}


def _actions(store: MemoryStore) -> list[str]:
    return [
        a.action
        for a in store.tables.audit.rows.values()
        if a.action == "mower.mow_defaults_changed"
    ]


async def test_fleet_mow_defaults_set_read_reset_and_audit() -> None:
    store = MemoryStore()
    async with harness(store=store) as h:
        owner = await h.owner()
        mower = await h.register(owner)
        assert mower["mow_defaults"] is None
        url = f"{API}/mowers/{mower['id']}/mow-defaults"

        saved = await h.client.put(url, headers=owner.headers, json={"pattern": DIAGONAL})
        assert saved.status_code == 200, saved.text
        expected = {"name": "diagonal", "angle_deg": 45.0, "border_laps": 2, "spacing_m": None}
        assert saved.json()["mow_defaults"] == {"pattern": expected}

        one = await h.client.get(f"{API}/mowers/{mower['id']}", headers=owner.headers)
        every = await h.client.get(f"{API}/mowers", headers=owner.headers)
        assert one.json()["mow_defaults"] == {"pattern": expected}
        assert every.json()["items"][0]["mow_defaults"] == {"pattern": expected}
        # The store keeps the domain value (the JSON shape is the database adapter's business).
        row = next(r for r in store.tables.mowers.rows.values() if str(r.id) == mower["id"])
        assert row.mow_defaults == MowDefaults(
            pattern=MowPattern(name="diagonal", angle_deg=45, border_laps=2)
        )

        # The same value again writes no audit row.
        again = await h.client.put(url, headers=owner.headers, json={"pattern": DIAGONAL})
        assert again.status_code == 200
        assert _actions(store) == ["mower.mow_defaults_changed"]

        partial = await h.client.put(url, headers=owner.headers, json={"pattern": {}})
        assert partial.json()["mow_defaults"]["pattern"] == {
            "name": "stripes",
            "angle_deg": 0.0,
            "border_laps": 1,
            "spacing_m": None,
        }
        cleared = await h.client.put(url, headers=owner.headers, json={"pattern": None})
        assert cleared.status_code == 200
        assert cleared.json()["mow_defaults"] is None
        assert len(_actions(store)) == 3
        changed = [
            a.details for a in store.tables.audit.rows.values() if a.action in _actions(store)
        ]
        assert changed[0] == {
            "mow_defaults": {"pattern": {"name": "diagonal", "angle_deg": 45.0, "border_laps": 2}},
            "previous": None,
        }


@pytest.mark.parametrize(
    "body",
    [
        {"pattern": {"name": "spiral"}},
        {"pattern": {"angle_deg": 200}},
        {"pattern": {"border_laps": 5}},
        {"pattern": {"spacing_m": 0.01}},
        {"pattern": {"name": "stripes", "obstacle_laps": 2}},
        {"pattern": DIAGONAL, "account_id": str(uuid4())},
        {},
    ],
)
async def test_fleet_mow_defaults_refuse_invalid_bodies(body: dict[str, object]) -> None:
    async with harness() as h:
        owner = await h.owner()
        mower = await h.register(owner)
        response = await h.client.put(
            f"{API}/mowers/{mower['id']}/mow-defaults", headers=owner.headers, json=body
        )
    assert response.status_code == 422, response.text


async def test_fleet_mow_defaults_need_can_control() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower = await h.register(owner)
        url = f"{API}/mowers/{mower['id']}/mow-defaults"
        viewer = await h.member(owner, "viewer")
        operator = await h.member(owner, "operator")

        refused = await h.client.put(url, headers=viewer.headers, json={"pattern": DIAGONAL})
        allowed = await h.client.put(url, headers=operator.headers, json={"pattern": DIAGONAL})
        seen = await h.client.get(f"{API}/mowers/{mower['id']}", headers=viewer.headers)
    assert refused.status_code == 403
    assert allowed.status_code == 200
    assert seen.json()["mow_defaults"]["pattern"]["name"] == "diagonal"


async def test_fleet_mow_defaults_are_hidden_from_other_accounts() -> None:
    async with harness() as h:
        owner = await h.owner()
        stranger = await h.owner()
        mower = await h.register(owner)
        url = f"{API}/mowers/{mower['id']}/mow-defaults"
        foreign = await h.client.put(url, headers=stranger.headers, json={"pattern": DIAGONAL})
        unknown = await h.client.put(
            f"{API}/mowers/{uuid4()}/mow-defaults", headers=owner.headers, json={"pattern": None}
        )
        mine = await h.client.get(f"{API}/mowers/{mower['id']}", headers=owner.headers)
    assert (foreign.status_code, unknown.status_code) == (404, 404)
    assert mine.json()["mow_defaults"] is None
