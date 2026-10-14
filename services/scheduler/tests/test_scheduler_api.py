"""HTTP API: CRUD, validation, authorization per target, cross-account isolation, run-now,
preview, weather, the dev endpoint and the operational endpoints."""

from uuid import UUID, uuid4

from mowdeck_authz import mower
from mowdeck_contracts.v1 import MODULE_DESCRIPTOR_PATH, MowerActivity
from scheduler_support import API, harness

from scheduler.module import DESCRIPTOR, EDIT_PERMISSION, VIEW_PERMISSION


async def test_scheduler_api_operational_endpoints_and_descriptor() -> None:
    async with harness() as h:
        assert (await h.client.get("/healthz")).json() == {"status": "ok"}
        assert (await h.client.get("/readyz")).status_code == 200
        descriptor = (await h.client.get(MODULE_DESCRIPTOR_PATH)).json()
        anonymous = await h.client.get(f"{API}/schedules")
    assert descriptor == DESCRIPTOR.model_dump(mode="json")
    keys = {p["key"]: p for p in descriptor["permissions"]}
    assert keys[VIEW_PERMISSION]["implied_by"] == ["mower.view"]
    assert EDIT_PERMISSION in keys
    assert {e["type"] for e in descriptor["events"]} == {
        "scheduler.run.recorded",
        "scheduler.schedule.disabled",
    }
    # The web app's schedules module: every viewer of a mower sees its schedules.
    assert descriptor["ui_areas"] == [
        {
            "route": "/schedules",
            "menu_label": "Schedules",
            "labels": {"de": "Zeitpläne", "en": "Schedules"},
            "required_permission": "mower.view",
        }
    ]
    assert anonymous.status_code == 401


async def test_scheduler_api_crud_round_trip() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        created = await h.create(
            owner,
            target_type="mower",
            target_id=str(mower_id),
            kind="every_n_days",
            days_of_week=[],
            anchor_date="2026-10-10",
            interval_days=2,
            start_time="07:15",
            rain_pause={"probability_threshold": 40, "cooldown_hours": 0},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        url = f"{API}/schedules/{body['id']}"
        assert created.headers["location"] == url
        fetched = await h.client.get(url, headers=owner.headers)
        listed = await h.client.get(f"{API}/schedules", headers=owner.headers)
        patched = await h.client.patch(
            url,
            headers=owner.headers,
            json={
                "kind": "weekly",
                "days_of_week": ["sat", "sun"],
                "start_time": "09:00",
                "timezone": "Europe/London",
                "enabled": False,
                "pattern": None,
            },
        )
        enabled = await h.client.patch(url, headers=owner.headers, json={"enabled": True})
        back_to_site = await h.client.patch(url, headers=owner.headers, json={"timezone": None})
        deleted = await h.client.delete(url, headers=owner.headers)
        gone = await h.client.get(url, headers=owner.headers)
    assert body["next_run_at"] == "2026-10-10T05:15:00Z"
    assert body["rain_pause"]["probability_threshold"] == 40
    assert body["owner"]["subject"] == str(owner.principal.subject)
    assert fetched.json() == body
    assert [s["id"] for s in listed.json()["items"]] == [body["id"]]
    assert patched.status_code == 200, patched.text
    changed = patched.json()
    assert changed["days_of_week"] == ["sat", "sun"] and changed["anchor_date"] is None
    assert changed["timezone"] == "Europe/London" and changed["next_run_at"] is None
    assert enabled.json()["next_run_at"] == "2026-10-10T08:00:00Z"  # Saturday 09:00 London
    assert back_to_site.json()["timezone"] == "Europe/Berlin"
    assert deleted.status_code == 204 and gone.status_code == 404


async def test_scheduler_api_validation() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        target = {"target_type": "mower", "target_id": str(mower_id)}
        cases: list[dict[str, object]] = [
            {"days_of_week": []},
            {"kind": "every_n_days", "days_of_week": []},
            {"start_time": "25:00"},
            {"duration_minutes": 0},
            {"account_id": str(uuid4())},  # unknown fields are refused
            {"timezone": "Not/AZone"},
            {"rain_pause": {"probability_threshold": 150}},
        ]
        statuses = [(await h.create(owner, **target, **case)).status_code for case in cases]
        north = await h.fleet_post(owner, "groups", name="North")
        group_areas = await h.create(owner, target_type="group", target_id=north["id"])
        created = await h.schedule(owner, **target)
        bad_patch = await h.client.patch(
            f"{API}/schedules/{created['id']}", headers=owner.headers, json={"days_of_week": []}
        )
    assert statuses == [422] * len(cases)
    assert group_areas.status_code == 422
    assert bad_patch.status_code == 422


async def test_scheduler_api_rights_on_the_target() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        target = {"target_type": "mower", "target_id": str(mower_id)}
        viewer = await h.member(owner, "viewer")
        operator = await h.member(owner, "operator")
        editor = await h.member(owner, "scheduler")  # schedule.edit without mower.control
        outsider = await h.member(owner, "viewer", mower(uuid4()))
        created = await h.schedule(owner, **target)
        url = f"{API}/schedules/{created['id']}"
        by_viewer = await h.create(viewer, **target)
        by_operator = await h.create(operator, **target)
        by_editor = await h.create(editor, **target)
        seen = await h.client.get(url, headers=viewer.headers)
        listed = await h.client.get(f"{API}/schedules", headers=viewer.headers)
        hidden = await h.client.get(url, headers=outsider.headers)
        hidden_list = await h.client.get(f"{API}/schedules", headers=outsider.headers)
        patch = await h.client.patch(url, headers=viewer.headers, json={"name": "x"})
        delete = await h.client.delete(url, headers=operator.headers)
        skip = await h.client.post(f"{url}/skip-next", headers=viewer.headers)
        run_viewer = await h.client.post(f"{url}/run-now", headers=viewer.headers)
        run_operator = await h.client.post(f"{url}/run-now", headers=operator.headers)
    assert by_viewer.status_code == 403
    assert by_operator.status_code == 403  # no schedule.edit
    assert by_editor.status_code == 403  # could never run it
    assert seen.status_code == 200 and len(listed.json()["items"]) == 1
    assert hidden.status_code == 404 and hidden_list.json()["items"] == []
    assert patch.status_code == 403 and delete.status_code == 403 and skip.status_code == 403
    assert run_viewer.status_code == 403
    assert run_operator.status_code == 202
    [run] = run_operator.json()["items"]
    assert run["trigger"] == "manual" and run["outcome"] == "started"
    assert h.commands[0].issued_by.subject == operator.principal.subject


async def test_scheduler_api_other_accounts_see_nothing() -> None:
    async with harness() as h:
        lukas = await h.owner()
        bob = await h.owner()
        mower_id = await h.register(lukas)
        bobs_mower = await h.register(bob)
        created = await h.schedule(lukas, target_type="mower", target_id=str(mower_id))
        url = f"{API}/schedules/{created['id']}"
        answers = {
            "get": await h.client.get(url, headers=bob.headers),
            "patch": await h.client.patch(url, headers=bob.headers, json={"name": "mine"}),
            "delete": await h.client.delete(url, headers=bob.headers),
            "run-now": await h.client.post(f"{url}/run-now", headers=bob.headers),
            "skip-next": await h.client.post(f"{url}/skip-next", headers=bob.headers),
            "runs": await h.client.get(f"{url}/runs", headers=bob.headers),
            "preview": await h.client.get(f"{url}/preview", headers=bob.headers),
            "weather": await h.client.get(f"{API}/weather/{mower_id}", headers=bob.headers),
        }
        foreign_target = await h.create(bob, target_type="mower", target_id=str(mower_id))
        foreign_group = await h.create(
            bob, target_type="group", target_id=str(uuid4()), area_ids=[]
        )
        retarget = await h.client.patch(
            url, headers=lukas.headers, json={"target_id": str(bobs_mower)}
        )
        bobs_list = await h.client.get(f"{API}/schedules", headers=bob.headers)
        still = await h.client.get(url, headers=lukas.headers)
    assert {name: r.status_code for name, r in answers.items()} == dict.fromkeys(answers, 404)
    assert foreign_target.status_code == 404 and foreign_group.status_code == 404
    assert retarget.status_code == 404
    assert bobs_list.json()["items"] == []
    assert still.json()["name"] == "Morning"
    assert h.commands == []


async def test_scheduler_api_run_now_respects_rain_on_request() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        await h.map(mower_id, owner.account_id)
        created = await h.schedule(owner, target_type="mower", target_id=str(mower_id))
        url = f"{API}/schedules/{created['id']}/run-now"
        h.weather.mode = "rain"
        held = await h.client.post(f"{url}?respect_rain=true", headers=owner.headers)
        h.clock.advance(seconds=1)
        forced = await h.client.post(url, headers=owner.headers)
        h.clock.advance(seconds=1)
        await h.state(mower_id, owner.account_id, MowerActivity.MOWING)
        busy = await h.client.post(url, headers=owner.headers)
        schedule = (
            await h.client.get(f"{API}/schedules/{created['id']}", headers=owner.headers)
        ).json()
        page = await h.client.get(
            f"{API}/schedules/{created['id']}/runs?limit=2", headers=owner.headers
        )
        rest = await h.client.get(
            f"{API}/schedules/{created['id']}/runs",
            headers=owner.headers,
            params={"before": page.json()["next_before"]},
        )
    assert held.json()["items"][0]["outcome"] == "skipped_rain"
    assert forced.json()["items"][0]["outcome"] == "started"
    assert busy.json()["items"][0]["outcome"] == "skipped_mower_busy"
    assert schedule["next_run_at"] == created["next_run_at"]  # a manual run changes nothing
    assert len(page.json()["items"]) == 2 and page.json()["next_before"] is not None
    assert len(rest.json()["items"]) == 1


async def test_scheduler_api_preview_and_weather() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        created = await h.schedule(owner, target_type="mower", target_id=str(mower_id))
        url = f"{API}/schedules/{created['id']}/preview"
        no_location = await h.client.get(url, headers=owner.headers)
        await h.map(mower_id, owner.account_id)
        preview = await h.client.get(f"{url}?count=3", headers=owner.headers)
        h.weather.mode = "rain"
        weather = await h.client.get(f"{API}/weather/{mower_id}", headers=owner.headers)
        lenient = await h.client.get(
            f"{API}/weather/{mower_id}?probability_threshold=95&precipitation_mm_threshold=5",
            headers=owner.headers,
        )
        unknown_mower = await h.client.get(f"{API}/weather/{uuid4()}", headers=owner.headers)
        too_many = await h.client.get(f"{url}?count=50", headers=owner.headers)
    assert no_location.json()["weather"]["verdict"] == "no_location"
    body = preview.json()
    assert [i["at"] for i in body["items"]] == [
        "2026-10-09T08:30:00Z",
        "2026-10-10T08:30:00Z",
        "2026-10-11T08:30:00Z",
    ]
    assert body["items"][0]["window_end"] == "2026-10-09T10:30:00Z"
    assert body["weather"]["verdict"] == "run" and body["weather"]["location_source"] == "map"
    assert body["weather_mower_id"] == str(mower_id)
    assert len(body["weather"]["hours"]) == 6  # 2 h back, 3 h ahead, partial hours included
    decision = weather.json()["decision"]
    assert decision["verdict"] == "hold" and decision["holds"] is True
    assert decision["provider"] == "static" and decision["attribution"] is None
    assert (decision["latitude"], decision["longitude"]) == (48.137154, 11.576124)
    assert lenient.json()["decision"]["verdict"] == "run"
    assert unknown_mower.status_code == 404
    assert too_many.status_code == 422


async def test_scheduler_api_dev_weather_endpoint() -> None:
    async with harness() as h:
        owner = await h.owner()
        closed = await h.client.put(
            f"{API}/dev/weather", headers=owner.headers, json={"mode": "rain"}
        )
    assert closed.status_code == 404
    async with harness(scheduler_dev_endpoints=True) as h:
        owner = await h.owner()
        opened = await h.client.put(
            f"{API}/dev/weather", headers=owner.headers, json={"mode": "rain"}
        )
        assert h.weather.mode == "rain"
    assert opened.status_code == 200


async def test_scheduler_api_limits_and_conflicts() -> None:
    async with harness(scheduler_max_schedules_per_account=1) as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        target = {"target_type": "mower", "target_id": str(mower_id)}
        created = await h.schedule(owner, **target, enabled=False)
        second = await h.create(owner, **target)
        skip = await h.client.post(
            f"{API}/schedules/{created['id']}/skip-next", headers=owner.headers
        )
        missing = await h.client.delete(f"{API}/schedules/{uuid4()}", headers=owner.headers)
    assert created["next_run_at"] is None
    assert second.status_code == 409 and skip.status_code == 409
    assert missing.status_code == 404


async def test_scheduler_api_moving_a_schedule_to_a_group_drops_its_areas() -> None:
    async with harness() as h:
        owner = await h.owner()
        north = await h.fleet_post(owner, "groups", name="North")
        mower_id = await h.register(owner, group_ids=[north["id"]])
        created = await h.schedule(owner, target_type="mower", target_id=str(mower_id))
        manager = await h.member(owner, "manager")
        moved = await h.client.patch(
            f"{API}/schedules/{created['id']}",
            headers=manager.headers,
            json={"target_type": "group", "target_id": north["id"]},
        )
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body["area_ids"] == [] and body["target_type"] == "group"
    assert body["owner"]["subject"] == str(manager.principal.subject)  # the editor owns it now
    assert UUID(body["target_id"]) == UUID(north["id"])
