"""The iOS widget overview (ADR 0059): next runs, rain verdict, access."""

from uuid import uuid4

from scheduler_support import API, harness


async def test_widget_overview_lists_the_next_runs_and_the_verdict() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        other_id = await h.register(owner)
        await h.map(mower_id, owner.account_id)
        await h.schedule(owner, name="Morning", target_type="mower", target_id=str(mower_id))
        await h.schedule(
            owner, name="Evening", start_time="18:00", target_type="mower", target_id=str(mower_id)
        )
        await h.schedule(owner, name="Other", target_type="mower", target_id=str(other_id))
        dry = await h.client.get(f"{API}/mowers/{mower_id}/widget", headers=owner.headers)
        h.weather.mode = "rain"
        rain = await h.client.get(f"{API}/mowers/{mower_id}/widget", headers=owner.headers)
    body = dry.json()
    assert dry.status_code == 200
    assert dry.headers["cache-control"] == "private, max-age=60"
    assert [r["name"] for r in body["runs"]] == ["Morning", "Evening"]  # the other mower's is out
    assert body["runs"][0]["starts_at"] == "2026-10-09T08:30:00Z"
    assert body["runs"][0]["duration_minutes"] == 120
    assert body["verdict"] == "runs"
    assert body["rain"]["raining_now"] is False
    assert rain.json()["verdict"] == "rain_pause"
    assert rain.json()["rain"]["raining_now"] is True
    assert rain.json()["rain"]["expected_mm_next_hours"] > 0


async def test_widget_overview_leaves_out_disabled_and_skipped_runs() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        await h.map(mower_id, owner.account_id)
        first = await h.schedule(owner, name="A", target_type="mower", target_id=str(mower_id))
        second = await h.schedule(
            owner, name="B", start_time="20:00", target_type="mower", target_id=str(mower_id)
        )
        await h.client.post(f"{API}/schedules/{first['id']}/skip-next", headers=owner.headers)
        await h.client.patch(
            f"{API}/schedules/{second['id']}", headers=owner.headers, json={"enabled": False}
        )
        overview = await h.client.get(f"{API}/mowers/{mower_id}/widget", headers=owner.headers)
    assert overview.json()["runs"] == []  # A is skipped, B is off


async def test_widget_overview_without_location_is_unsure_and_access_is_checked() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        no_map = await h.client.get(f"{API}/mowers/{mower_id}/widget", headers=owner.headers)
        stranger = await h.owner()
        foreign = await h.client.get(f"{API}/mowers/{mower_id}/widget", headers=stranger.headers)
        unknown = await h.client.get(f"{API}/mowers/{uuid4()}/widget", headers=owner.headers)
        anonymous = await h.client.get(f"{API}/mowers/{mower_id}/widget")
    assert no_map.json()["verdict"] == "unsure"
    assert no_map.json()["rain"]["raining_now"] is None
    assert no_map.json()["runs"] == []
    assert foreign.status_code == 404
    assert unknown.status_code == 404
    assert anonymous.status_code == 401
