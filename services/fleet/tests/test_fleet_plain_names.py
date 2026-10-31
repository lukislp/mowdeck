"""Names others read (sites, groups, areas, mowers) lose bidi overrides and zero-width
characters, so nobody can spoof a label for the other people of the account."""

from fleet_support import API, harness

RLO = "\u202e"
ZWSP = "\u200b"


async def test_fleet_names_lose_invisible_characters() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower = await h.register(owner)
        site = await h.client.post(
            f"{API}/sites", headers=owner.headers, json={"name": f"{RLO}Home{ZWSP}"}
        )
        group = await h.client.post(
            f"{API}/groups", headers=owner.headers, json={"name": f"North{ZWSP}"}
        )
        renamed = await h.client.patch(
            f"{API}/groups/{group.json()['id']}",
            headers=owner.headers,
            json={"name": f"{RLO}South"},
        )
        area = await h.client.put(
            f"{API}/mowers/{mower['id']}/area-names/a1",
            headers=owner.headers,
            json={"name": f"Side{ZWSP} {RLO}strip"},
        )
        mower_name = await h.client.patch(
            f"{API}/mowers/{mower['id']}", headers=owner.headers, json={"name": f"Pebble{RLO}"}
        )
    assert site.status_code == 201, site.text
    assert site.json()["name"] == "Home"
    assert group.json()["name"] == "North"
    assert renamed.json()["name"] == "South"
    assert area.json() == {"names": {"a1": "Side strip"}}
    assert mower_name.json()["name"] == "Pebble"


async def test_a_name_of_only_invisible_characters_is_refused() -> None:
    async with harness() as h:
        owner = await h.owner()
        response = await h.client.post(
            f"{API}/groups", headers=owner.headers, json={"name": RLO + ZWSP}
        )
    assert response.status_code == 422, response.text
