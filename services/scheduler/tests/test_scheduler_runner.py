"""The ticker: firings end to end through Fleet, skips, auto-disable and the follow-up."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from mowdeck_authz import account, group, principal_subject
from mowdeck_contracts.v1 import (
    SCHEDULER_RUN_RECORDED,
    SCHEDULER_SCHEDULE_DISABLED,
    MowerActivity,
)
from scheduler_support import API, T0, harness

from scheduler.domain.errors import Unavailable
from scheduler.domain.rain import Forecast

FIRST = datetime(2026, 10, 9, 8, 30, tzinfo=UTC)  # Friday 10:30 Berlin


async def test_scheduler_fires_and_starts_the_mower_through_fleet() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        created = await h.schedule(
            owner,
            target_type="mower",
            target_id=str(mower_id),
            pattern={"spacing_cm": 20},
            rotation_angles=[0, 90],
        )
        assert created["next_run_at"] == "2026-10-09T08:30:00Z"
        assert created["timezone"] == "Europe/Berlin"
        assert await h.tick() == 0  # not due yet
        h.clock.set(FIRST + timedelta(seconds=10))
        assert await h.tick() == 1
        assert await h.tick() == 0  # fired once only
        [run] = await h.runs(owner, created["id"])
        record = await h.fleet_command(owner, run["command_id"])
        h.clock.set(FIRST + timedelta(days=1, seconds=5))
        await h.tick()
        schedule = (
            await h.client.get(f"{API}/schedules/{created['id']}", headers=owner.headers)
        ).json()
    assert run["outcome"] == "started" and run["trigger"] == "schedule"
    assert run["mower_id"] == str(mower_id) and run["details"] == "areas front, angle 0"
    assert run["scheduled_for"] == "2026-10-09T08:30:00Z"
    assert run["window_end_at"] == "2026-10-09T10:30:00Z"
    assert record["origin"] == "scheduler" and record["schedule_id"] == created["id"]
    assert record["status"] == "executed"
    assert record["issued_by"]["subject"] == str(owner.principal.subject)
    first, second = h.commands
    assert first.command.kind == "start_mow"
    # MowPattern-shaped: stripes with one border lap, the rotation's angle as angle_deg.
    base = {"name": "stripes", "border_laps": 1, "spacing_cm": 20}
    assert first.command.model_dump()["pattern"] == {**base, "angle_deg": 0.0}
    assert second.command.model_dump()["pattern"] == {**base, "angle_deg": 90.0}
    assert schedule["rotation_index"] == 2
    assert schedule["next_run_at"] == "2026-10-11T08:30:00Z"
    assert schedule["last_run_at"] is not None
    events = h.events(SCHEDULER_RUN_RECORDED)
    assert [e["outcome"] for e in events] == ["started", "started"]


async def test_scheduler_rain_pause_holds_the_run() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        await h.map(mower_id, owner.account_id)  # geo origin of the map: the location
        created = await h.schedule(owner, target_type="mower", target_id=str(mower_id))
        h.weather.mode = "rain"
        h.clock.set(FIRST)
        await h.tick()
        [run] = await h.runs(owner, created["id"])
    assert run["outcome"] == "skipped_rain" and "probability 90 %" in run["details"]
    assert h.commands == []


async def test_scheduler_rain_without_location_or_forecast_runs_by_default() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        held = await h.schedule(
            owner,
            name="Careful",
            target_type="mower",
            target_id=str(mower_id),
            rain_pause={"run_when_unknown": False},
        )
        relaxed = await h.schedule(owner, target_type="mower", target_id=str(mower_id))
        h.weather.mode = "rain"
        h.clock.set(FIRST)
        await h.tick()  # no location known at all: both run
        no_location = [r["outcome"] for r in await h.runs(owner, held["id"])]
        await h.state(mower_id, owner.account_id, MowerActivity.DOCKED, position=(52.5, 13.4))
        h.weather.mode = "unknown"
        h.clock.set(FIRST + timedelta(days=1))
        await h.tick()
        unknown_held = (await h.runs(owner, held["id"]))[0]
        unknown_relaxed = (await h.runs(owner, relaxed["id"]))[0]
    assert no_location == ["started"]
    assert unknown_held["outcome"] == "skipped_rain" and "holding" in unknown_held["details"]
    assert unknown_relaxed["outcome"] == "started"


async def test_scheduler_site_coordinates_win_over_the_map() -> None:
    async with harness() as h:
        owner = await h.owner()
        site = await h.fleet_post(owner, "sites", name="Garden", latitude=52.52, longitude=13.4)
        mower_id = await h.register(owner, site_id=site["id"])
        await h.map(mower_id, owner.account_id, origin=(48.1, 11.5))
        seen: list[tuple[float, float]] = []
        original = h.weather.forecast

        async def spy(latitude: float, longitude: float) -> Forecast | None:
            seen.append((latitude, longitude))
            return await original(latitude, longitude)

        h.weather.forecast = spy  # type: ignore[method-assign]
        await h.schedule(owner, target_type="mower", target_id=str(mower_id))
        h.clock.set(FIRST)
        await h.tick()
        weather = await h.client.get(f"{API}/weather/{mower_id}", headers=owner.headers)
    assert seen[0] == (52.52, 13.4)
    assert weather.json()["decision"]["location_source"] == "site"


async def test_scheduler_busy_mower_is_skipped() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        created = await h.schedule(owner, target_type="mower", target_id=str(mower_id))
        await h.state(mower_id, owner.account_id, MowerActivity.MOWING)
        await h.state(  # an older state does not replace the newer one
            mower_id, owner.account_id, MowerActivity.DOCKED, at=T0 - timedelta(minutes=5)
        )
        h.clock.set(FIRST)
        await h.tick()
        [run] = await h.runs(owner, created["id"])
    assert run["outcome"] == "skipped_mower_busy" and run["details"] == "the mower is mowing"
    assert h.commands == []


async def test_scheduler_permission_recheck_skips_and_disables() -> None:
    async with harness(scheduler_max_permission_skips=2) as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        manager = await h.member(owner, "manager")
        created = await h.schedule(manager, target_type="mower", target_id=str(mower_id))
        await h.revoke(manager)  # the owner of the schedule loses can_control
        for day in range(3):
            h.clock.set(FIRST + timedelta(days=day))
            await h.tick()
        runs = await h.runs(owner, created["id"])
        schedule = (
            await h.client.get(f"{API}/schedules/{created['id']}", headers=owner.headers)
        ).json()
    assert [r["outcome"] for r in runs] == ["skipped_permission", "skipped_permission"]
    assert schedule["enabled"] is False and schedule["next_run_at"] is None
    assert "lost can_control" in schedule["disabled_reason"]
    [disabled] = h.events(SCHEDULER_SCHEDULE_DISABLED)
    assert disabled["schedule_id"] == created["id"]
    assert disabled["owner"]["subject"] == str(manager.principal.subject)
    assert h.commands == []


async def test_scheduler_a_start_resets_the_permission_counter() -> None:
    async with harness(scheduler_max_permission_skips=2) as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        manager = await h.member(owner, "manager")
        created = await h.schedule(manager, target_type="mower", target_id=str(mower_id))
        await h.revoke(manager)
        h.clock.set(FIRST)
        await h.tick()
        await h.identity.replace_subject_grants(
            principal_subject(manager.principal),
            account(owner.account_id),
            ["mower.view", "mower.control", "schedule.edit"],
        )
        h.clock.set(FIRST + timedelta(days=1))
        await h.tick()
        schedule = (
            await h.client.get(f"{API}/schedules/{created['id']}", headers=owner.headers)
        ).json()
    assert schedule["enabled"] is True and schedule["permission_skips"] == 0


async def test_scheduler_group_schedule_mows_all_areas_of_each_visible_mower() -> None:
    async with harness() as h:
        owner = await h.owner()
        north = await h.fleet_post(owner, "groups", name="North")
        first = await h.register(owner, group_ids=[north["id"]])
        second = await h.register(owner, group_ids=[north["id"]])
        await h.map(first, owner.account_id, areas=("a", "b"))
        created = await h.schedule(owner, target_type="group", target_id=north["id"], area_ids=[])
        h.clock.set(FIRST)
        await h.tick()
        runs = {UUID(r["mower_id"]): r for r in await h.runs(owner, created["id"])}
    assert runs[first]["outcome"] == "started" and runs[first]["details"] == "areas a, b, angle 0"
    assert runs[second]["outcome"] == "failed" and "no areas" in runs[second]["details"]
    assert [c.mower_id for c in h.commands] == [first]


async def test_scheduler_group_member_with_rights_on_the_group_only() -> None:
    async with harness() as h:
        owner = await h.owner()
        north = await h.fleet_post(owner, "groups", name="North")
        inside = await h.register(owner, group_ids=[north["id"]])
        outside = await h.register(owner)
        manager = await h.member(owner, "manager", group(UUID(north["id"])))
        created = await h.schedule(manager, target_type="group", target_id=north["id"], area_ids=[])
        refused = await h.create(manager, target_type="mower", target_id=str(outside))
        await h.map(inside, owner.account_id)
        h.clock.set(FIRST)
        await h.tick()
        [run] = await h.runs(manager, created["id"])
        listed = await h.client.get(f"{API}/schedules", headers=manager.headers)
    assert refused.status_code == 404
    assert run["outcome"] == "started" and run["mower_id"] == str(inside)
    assert [s["id"] for s in listed.json()["items"]] == [created["id"]]


async def test_scheduler_skip_next_records_a_manual_skip() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        created = await h.schedule(owner, target_type="mower", target_id=str(mower_id))
        skipped = await h.client.post(
            f"{API}/schedules/{created['id']}/skip-next", headers=owner.headers
        )
        preview = await h.client.get(
            f"{API}/schedules/{created['id']}/preview?count=2", headers=owner.headers
        )
        h.clock.set(FIRST)
        await h.tick()
        h.clock.set(FIRST + timedelta(days=1))
        await h.tick()
        runs = await h.runs(owner, created["id"])
    assert skipped.json()["skip_until"] == "2026-10-09T08:30:00Z"
    assert [i["skipped"] for i in preview.json()["items"]] == [True, False]
    assert [r["outcome"] for r in runs] == ["started", "skipped_manual"]


async def test_scheduler_closed_window_after_downtime_is_skipped() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        created = await h.schedule(
            owner, target_type="mower", target_id=str(mower_id), duration_minutes=60
        )
        h.clock.set(FIRST + timedelta(minutes=30))  # late, but inside the window: starts
        await h.tick()
        h.clock.set(FIRST + timedelta(days=3, hours=2))  # down for days: one skip, no catch up
        await h.tick()
        runs = await h.runs(owner, created["id"])
        schedule = (
            await h.client.get(f"{API}/schedules/{created['id']}", headers=owner.headers)
        ).json()
    assert [r["outcome"] for r in runs] == ["skipped_window", "started"]
    assert runs[0]["scheduled_for"] == "2026-10-10T08:30:00Z"
    assert schedule["next_run_at"] == "2026-10-13T08:30:00Z"


async def test_scheduler_fleet_unavailable_and_target_removed() -> None:
    class Down:
        async def resolve(self, *args: object) -> object:
            raise Unavailable("fleet did not answer fleet.targets.resolve")

        async def issue(self, request: object) -> object:  # pragma: no cover - not reached
            raise Unavailable("down")

    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        created = await h.schedule(owner, target_type="mower", target_id=str(mower_id))
        gone = await h.fleet_client.delete(
            f"/api/v1/fleet/mowers/{mower_id}", headers=owner.headers
        )
        assert gone.status_code == 204
        h.clock.set(FIRST)
        await h.tick()
        runs = await h.runs(owner, created["id"])
        schedule = await h.client.get(f"{API}/schedules/{created['id']}", headers=owner.headers)
    assert [r["outcome"] for r in runs] == ["failed"]
    assert runs[0]["details"] == "the target no longer exists"
    assert schedule.status_code == 200  # account-wide rights still see it (and may delete it)
    assert schedule.json()["enabled"] is False
    [disabled] = h.events(SCHEDULER_SCHEDULE_DISABLED)
    assert disabled["reason"] == "the target no longer exists"

    async with harness(fleet=Down()) as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        refused = await h.create(owner, target_type="mower", target_id=str(mower_id))
        assert refused.status_code == 503


async def test_scheduler_fleet_refusal_is_recorded() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        created = await h.schedule(owner, target_type="mower", target_id=str(mower_id))
        h.fleet_app.state.fleet.commands.publisher = _Broken()
        h.clock.set(FIRST)
        await h.tick()
        [run] = await h.runs(owner, created["id"])
    assert run["outcome"] == "failed" and run["details"].startswith("fleet: unavailable")


class _Broken:
    async def publish(self, envelope: object) -> None:
        raise ConnectionError("down")


async def test_scheduler_follow_up_docks_on_rain_and_at_window_end() -> None:
    async with harness() as h:
        owner = await h.owner()
        rainy = await h.register(owner)
        late = await h.register(owner)
        done = await h.register(owner)
        for mower_id in (rainy, late, done):
            await h.map(mower_id, owner.account_id)
        a = await h.schedule(owner, target_type="mower", target_id=str(rainy))
        b = await h.schedule(
            owner,
            target_type="mower",
            target_id=str(late),
            duration_minutes=30,
            return_to_dock_at_window_end=True,
            rain_pause={"enabled": False},
        )
        c = await h.schedule(owner, target_type="mower", target_id=str(done))
        h.clock.set(FIRST)
        await h.tick()
        for mower_id in (rainy, late, done):
            await h.state(mower_id, owner.account_id, MowerActivity.MOWING)
        h.weather.mode = "rain"
        h.clock.advance(minutes=10)
        await h.state(done, owner.account_id, MowerActivity.CHARGING)
        await h.tick()  # rain: the rainy mower docks; the other one has no rain pause
        h.clock.advance(minutes=25)
        await h.tick()  # the window of b closed
        runs = {
            name: (await h.runs(owner, s["id"]))[0] for name, s in (("a", a), ("b", b), ("c", c))
        }
    kinds = [(c.mower_id, c.command.kind) for c in h.commands]
    assert (rainy, "return_to_dock") in kinds and (late, "return_to_dock") in kinds
    assert (done, "return_to_dock") not in kinds
    assert runs["a"]["followup"] == "rain_dock" and "rain pause" in runs["a"]["details"]
    assert runs["b"]["followup"] == "window_dock" and "window closed" in runs["b"]["details"]
    assert runs["c"]["followup"] == "finished"


async def test_scheduler_open_runs_are_closed_after_the_horizon_and_purged() -> None:
    async with harness(scheduler_run_retention_days=1) as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        created = await h.schedule(owner, target_type="mower", target_id=str(mower_id))
        h.clock.set(FIRST)
        await h.tick()
        h.clock.advance(hours=5)
        await h.tick()
        [run] = await h.runs(owner, created["id"])
        assert run["followup"] == "closed"
        h.clock.advance(days=2)
        assert await h.services.runner.purge() == 1
        assert await h.runs(owner, created["id"]) == []


async def test_scheduler_follows_the_site_time_zone() -> None:
    async with harness() as h:
        owner = await h.owner()
        site = await h.fleet_post(owner, "sites", name="Lisbon", timezone="Europe/Lisbon")
        mower_id = await h.register(owner, site_id=site["id"])
        created = await h.schedule(owner, target_type="mower", target_id=str(mower_id))
        pinned = await h.schedule(
            owner, target_type="mower", target_id=str(mower_id), timezone="Asia/Tokyo"
        )
        changed = await h.fleet_client.patch(
            f"/api/v1/fleet/sites/{site['id']}",
            headers=owner.headers,
            json={"timezone": "Europe/Berlin"},
        )
        assert changed.status_code == 200
        h.clock.set(datetime(2026, 10, 9, 9, 30, tzinfo=UTC))  # 10:30 Lisbon
        await h.tick()
        schedule = (
            await h.client.get(f"{API}/schedules/{created['id']}", headers=owner.headers)
        ).json()
    assert created["timezone"] == "Europe/Lisbon"
    assert pinned["timezone"] == "Asia/Tokyo" and pinned["next_run_at"] == "2026-10-10T01:30:00Z"
    assert schedule["timezone"] == "Europe/Berlin"  # followed the site at the firing
    assert schedule["next_run_at"] == "2026-10-10T08:30:00Z"


async def test_scheduler_starts_from_the_mowers_saved_defaults() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        saved = await h.fleet_client.put(
            f"/api/v1/fleet/mowers/{mower_id}/mow-defaults",
            headers=owner.headers,
            json={"pattern": {"name": "checkerboard", "angle_deg": 30, "border_laps": 3}},
        )
        assert saved.status_code == 200, saved.text
        await h.schedule(
            owner,
            target_type="mower",
            target_id=str(mower_id),
            pattern={"angle": 100},
            rotation_angles=[45, 225],
        )
        h.clock.set(FIRST + timedelta(seconds=10))
        await h.tick()
        h.clock.set(FIRST + timedelta(days=1, seconds=5))
        await h.tick()
    first, second = h.commands
    # The saved defaults stay; only the angle is the schedule's (normalised to 0..180).
    assert first.command.model_dump()["pattern"] == {
        "name": "checkerboard",
        "angle_deg": 45.0,
        "border_laps": 3,
    }
    assert second.command.model_dump()["pattern"]["angle_deg"] == 45.0
