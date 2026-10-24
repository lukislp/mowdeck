"""Start settings per schedule (ADR 0024 amendment 2026-10-24): checked against the target
mowers' capabilities when saved (per member for a group), merged over the mow defaults and
fitted to each mower at fire time, blade height and speed sent before the start."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from scheduler_support import API, Harness, Person, harness

FIRST = datetime(2026, 10, 9, 8, 30, tzinfo=UTC)  # Friday 10:30 Berlin

AWD: dict[str, Any] = {
    "model": "LUBA 2 AWD 3000",
    "native_patterns": ["stripes", "diagonal", "checkerboard"],
    "path_spacing_range_mm": [200, 350],
    "blade_height_range_mm": [25, 70],
    "min_speed_mps": 0.2,
    "max_speed_mps": 1.0,
    "start_options": {
        "obstacle_detection": ["off", "standard", "sensitive"],
        "obstacle_laps_range": [0, 4],
        "orders": ["border_first", "grid_first"],
        "start_progress_range_pct": [0, 99],
    },
}
YUKA: dict[str, Any] = {
    "model": "Yuka",
    "native_patterns": ["stripes", "edge_only"],
    "blade_height_range_mm": [30, 60],
    "max_speed_mps": 0.8,
    "start_options": {"obstacle_laps_range": [0, 2]},
}


async def _mower(h: Harness, owner: Person, caps: dict[str, Any] | None, **body: Any) -> UUID:
    mower_id = await h.register(owner, **body)
    if caps is not None:
        await h.capabilities(mower_id, owner.account_id, **caps)
    return mower_id


async def test_scheduler_options_are_stored_and_returned() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await _mower(h, owner, AWD)
        created = await h.schedule(
            owner,
            target_type="mower",
            target_id=str(mower_id),
            pattern={
                "name": "diagonal",
                "angle_deg": 30,
                "spacing_m": 0.25,
                "obstacle_detection": "sensitive",
                "obstacle_laps": 1,
                "order": "grid_first",
                "border_laps": None,
            },
            blade_height_mm=45,
            speed_mps=0.6,
        )
        plain = await h.schedule(owner, target_type="mower", target_id=str(mower_id))
    assert created["pattern"] == {
        "name": "diagonal",
        "angle_deg": 30.0,
        "border_laps": None,
        "spacing_m": 0.25,
        "obstacle_detection": "sensitive",
        "obstacle_laps": 1,
        "order": "grid_first",
    }
    assert created["blade_height_mm"] == 45 and created["speed_mps"] == 0.6
    # Nothing set: every option falls back to the mow defaults, then the mower.
    assert plain["pattern"] is None
    assert plain["blade_height_mm"] is None and plain["speed_mps"] is None


async def test_scheduler_refuses_the_start_progress_and_unknown_keys() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await _mower(h, owner, AWD)
        target = {"target_type": "mower", "target_id": str(mower_id)}
        progress = await h.create(owner, **target, pattern={"start_progress_pct": 40})
        legacy = await h.create(owner, **target, pattern={"angle": 40})
        blade = await h.create(owner, **target, blade_height_mm=5)
    assert progress.status_code == 422
    assert progress.json()["errors"][0]["pointer"] == "#/pattern/start_progress_pct"
    assert legacy.status_code == 422
    assert blade.status_code == 422


async def test_scheduler_checks_the_options_against_the_mower() -> None:
    async with harness() as h:
        owner = await h.owner()
        bare = await _mower(h, owner, {"native_patterns": ["stripes"]}, name="Bare")
        unknown = await _mower(h, owner, None, name="Fresh")
        target = {"target_type": "mower", "target_id": str(bare)}
        detection = await h.create(owner, **target, pattern={"obstacle_detection": "standard"})
        pattern = await h.create(owner, **target, pattern={"name": "diagonal"})
        blade = await h.create(owner, **target, blade_height_mm=40)
        fresh = await h.create(
            owner, target_type="mower", target_id=str(unknown), pattern={"order": "grid_first"}
        )
        # Angle and border laps need no declaration: fine on any mower.
        plain = await h.create(
            owner,
            target_type="mower",
            target_id=str(unknown),
            pattern={"angle_deg": 90, "border_laps": 2},
        )
    assert detection.status_code == 422
    assert detection.json()["detail"] == (
        "start settings do not fit Bare: obstacle detection 'standard' is not offered"
    )
    assert pattern.status_code == 422 and "'diagonal' is not offered" in pattern.json()["detail"]
    assert blade.status_code == 422 and "blade height is not settable" in blade.json()["detail"]
    assert fresh.status_code == 422
    assert fresh.json()["detail"] == (
        "start settings do not fit Fresh: its capabilities are not known yet"
    )
    assert plain.status_code == 201, plain.text


async def test_scheduler_group_options_must_fit_every_member() -> None:
    async with harness() as h:
        owner = await h.owner()
        north = await h.fleet_post(owner, "groups", name="North")
        await _mower(h, owner, AWD, name="Luba", group_ids=[north["id"]])
        await _mower(h, owner, YUKA, name="Yuka", group_ids=[north["id"]])
        target = {"target_type": "group", "target_id": north["id"], "area_ids": []}
        # Only the AWD offers diagonal, obstacle detection and the spacing.
        diagonal = await h.create(owner, **target, pattern={"name": "diagonal"})
        detection = await h.create(owner, **target, pattern={"obstacle_detection": "off"})
        laps = await h.create(owner, **target, pattern={"obstacle_laps": 3})
        blade = await h.create(owner, **target, blade_height_mm=65)
        # What both offer: stripes, no-go laps 0 to 2, blade 30 to 60, speed 0.2 to 0.8.
        common = await h.create(
            owner,
            **target,
            pattern={"name": "stripes", "obstacle_laps": 2},
            blade_height_mm=55,
            speed_mps=0.8,
        )
    assert diagonal.status_code == 422
    assert diagonal.json()["detail"] == (
        "start settings do not fit Yuka: pattern 'diagonal' is not offered"
    )
    assert detection.status_code == 422 and "Yuka" in detection.json()["detail"]
    assert laps.status_code == 422
    assert "obstacle_laps must be between 0 and 2" in laps.json()["detail"]
    assert blade.status_code == 422 and "between 30 and 60" in blade.json()["detail"]
    assert common.status_code == 201, common.text


async def test_scheduler_patch_checks_only_changed_settings() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await _mower(h, owner, AWD)
        created = await h.schedule(
            owner,
            target_type="mower",
            target_id=str(mower_id),
            pattern={"obstacle_detection": "sensitive"},
        )
        url = f"{API}/schedules/{created['id']}"
        # The mower's firmware drops the option: a rename still saves, a new value does not.
        h.clock.advance(minutes=1)
        await h.capabilities(mower_id, owner.account_id, **{**AWD, "start_options": {}})
        renamed = await h.client.patch(url, headers=owner.headers, json={"name": "Evening"})
        same = await h.client.patch(
            url, headers=owner.headers, json={"pattern": {"obstacle_detection": "sensitive"}}
        )
        changed = await h.client.patch(
            url, headers=owner.headers, json={"pattern": {"obstacle_detection": "off"}}
        )
        cleared = await h.client.patch(url, headers=owner.headers, json={"pattern": None})
    assert renamed.status_code == 200, renamed.text
    assert same.status_code == 200, same.text
    assert changed.status_code == 422
    assert cleared.status_code == 200 and cleared.json()["pattern"] is None


async def test_scheduler_fires_with_schedule_over_defaults_over_mower() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await _mower(h, owner, AWD)
        saved = await h.fleet_client.put(
            f"/api/v1/fleet/mowers/{mower_id}/mow-defaults",
            headers=owner.headers,
            json={
                "pattern": {
                    "name": "checkerboard",
                    "angle_deg": 30,
                    "border_laps": 3,
                    "obstacle_detection": "standard",
                    "order": "border_first",
                    "start_progress_pct": 40,
                }
            },
        )
        assert saved.status_code == 200, saved.text
        created = await h.schedule(
            owner,
            target_type="mower",
            target_id=str(mower_id),
            pattern={"name": "diagonal", "obstacle_detection": "sensitive", "obstacle_laps": 0},
            blade_height_mm=45,
            speed_mps=0.6,
        )
        h.clock.set(FIRST + timedelta(seconds=10))
        await h.tick()
        [run] = await h.runs(owner, created["id"])
        record = await h.fleet_command(owner, run["command_id"])
    blade, speed, start = h.commands
    assert blade.command.model_dump() == {"kind": "set_blade_height", "mm": 45}
    assert speed.command.model_dump() == {"kind": "set_speed", "mps": 0.6}
    assert start.command.kind == "start_mow"
    assert start.command.model_dump()["pattern"] == {
        "name": "diagonal",  # schedule
        "angle_deg": 30.0,  # mow defaults
        "border_laps": 3,  # mow defaults
        "obstacle_detection": "sensitive",  # schedule over the defaults' standard
        "obstacle_laps": 0,  # schedule
        "order": "border_first",  # mow defaults
    }  # no spacing (nobody sets it: the mower's own), never the defaults' start progress
    assert run["outcome"] == "started" and record["kind"] == "start_mow"
    assert record["schedule_id"] == created["id"]


async def test_scheduler_group_run_leaves_out_what_a_later_member_does_not_offer() -> None:
    async with harness() as h:
        owner = await h.owner()
        north = await h.fleet_post(owner, "groups", name="North")
        luba = await _mower(h, owner, AWD, name="Luba", group_ids=[north["id"]])
        await h.map(luba, owner.account_id)
        created = await h.schedule(
            owner,
            target_type="group",
            target_id=north["id"],
            area_ids=[],
            pattern={"order": "grid_first"},
            blade_height_mm=65,
        )
        # A Yuka joins the group after the schedule was saved: no path order, blade 30 to 60.
        yuka = await _mower(h, owner, YUKA, name="Yuka", group_ids=[north["id"]])
        await h.map(yuka, owner.account_id)
        h.clock.set(FIRST + timedelta(seconds=10))
        await h.tick()
        runs = {run["mower_id"]: run for run in await h.runs(owner, created["id"])}
    by_mower: dict[UUID, list[dict[str, Any]]] = {}
    for envelope in h.commands:
        by_mower.setdefault(envelope.mower_id, []).append(envelope.command.model_dump())
    assert [c["kind"] for c in by_mower[luba]] == ["set_blade_height", "start_mow"]
    assert by_mower[luba][1]["pattern"]["order"] == "grid_first"
    assert [c["kind"] for c in by_mower[yuka]] == ["start_mow"]
    assert "order" not in by_mower[yuka][0]["pattern"]
    assert runs[str(yuka)]["outcome"] == "started"
    assert runs[str(yuka)]["details"].endswith(
        "left out (not offered by the mower): order, blade_height_mm"
    )
