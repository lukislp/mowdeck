"""Pattern templates per account: save, list, use in a plan, delete."""

from typing import Any

from planner_support import API, harness

CHAIN: list[dict[str, Any]] = [
    {"pattern": "edge_only", "settings": {"laps": 2}},
    {"pattern": "diagonal", "settings": {"angle_deg": 10}},
]


async def test_planner_templates_round_trip_and_feed_plans() -> None:
    async with harness() as h:
        owner = await h.owner()
        other = await h.owner()
        mower_id = await h.mower(owner)
        url = f"{API}/templates/Edges and diagonal"
        created = await h.client.put(url, headers=owner.headers, json={"chain": CHAIN})
        replaced = await h.client.put(
            url, headers=owner.headers, json={"chain": CHAIN[:1], "description": "laps only"}
        )
        listed = await h.client.get(f"{API}/templates", headers=owner.headers)
        foreign = await h.client.get(f"{API}/templates", headers=other.headers)
        one = await h.client.get(url, headers=owner.headers)
        planned = await h.preview(
            owner,
            mower_id=str(mower_id),
            area={"area_id": "front"},
            template="Edges and diagonal",
        )
        missing = await h.preview(
            owner, mower_id=str(mower_id), area={"area_id": "front"}, template="nope"
        )
        deleted = await h.client.delete(url, headers=owner.headers)
        gone = await h.client.delete(url, headers=owner.headers)
        absent = await h.client.get(url, headers=owner.headers)
    assert created.status_code == 201, created.text
    assert replaced.status_code == 200
    assert replaced.json()["description"] == "laps only"
    assert replaced.json()["created_at"] == created.json()["created_at"]
    assert [t["name"] for t in listed.json()["items"]] == ["Edges and diagonal"]
    assert foreign.json()["items"] == []
    assert one.json()["chain"] == CHAIN[:1]
    assert planned.status_code == 200, planned.text
    assert planned.json()["pattern_chain"] == ["edge_only"]
    assert missing.status_code == 404
    assert deleted.status_code == 204
    assert gone.status_code == 404
    assert absent.status_code == 404


async def test_planner_templates_need_control_and_valid_chains() -> None:
    async with harness(planner_max_templates=1) as h:
        owner = await h.owner()
        viewer = await h.member(owner, "viewer")
        refused = await h.client.put(
            f"{API}/templates/mine", headers=viewer.headers, json={"chain": CHAIN}
        )
        readable = await h.client.get(f"{API}/templates", headers=viewer.headers)
        invalid = await h.client.put(
            f"{API}/templates/bad",
            headers=owner.headers,
            json={"chain": [{"pattern": "waves"}, {"pattern": "stripes", "settings": {"x": 1}}]},
        )
        first = await h.client.put(
            f"{API}/templates/a", headers=owner.headers, json={"chain": CHAIN}
        )
        second = await h.client.put(
            f"{API}/templates/b", headers=owner.headers, json={"chain": CHAIN}
        )
        viewer_delete = await h.client.delete(f"{API}/templates/a", headers=viewer.headers)
    assert refused.status_code == 403
    assert readable.status_code == 200
    assert invalid.status_code == 422
    details = [e["detail"] for e in invalid.json()["errors"]]
    assert details[0].startswith("chain.0: unknown pattern")
    assert details[1].startswith("chain.1.settings.x")
    assert first.status_code == 201
    assert second.status_code == 409
    assert viewer_delete.status_code == 403
