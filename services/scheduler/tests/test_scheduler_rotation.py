"""Mowing angle rotation (ADR 0061): step rotation from the mower's last angle per area, the
angle of each run in the run history and the event, the angle of each firing in the preview,
and the mower angle API for manual starts."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from scheduler_support import API, Harness, Person, harness

from scheduler.domain.models import Schedule
from scheduler.domain.rotation import (
    AreaAngle,
    directed,
    last_angle,
    list_sequence,
    normalised,
    step_sequence,
    stepped,
    valid_step,
)
from scheduler.domain.rules import run_angle, step_angle

FIRST = datetime(2026, 10, 9, 8, 30, tzinfo=UTC)  # Friday 10:30 Berlin
MOWER = uuid4()
ACCOUNT = uuid4()
PATTERNS = ["stripes", "edge_only", "random_angle"]


def _angle(area: str, angle: float, minutes: int) -> AreaAngle:
    return AreaAngle(
        mower_id=MOWER,
        area_id=area,
        account_id=ACCOUNT,
        angle_deg=angle,
        used_at=FIRST + timedelta(minutes=minutes),
        source="schedule",
    )


# --- domain ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("angle", "expected"),
    [(0, 0.0), (179.9, 179.9), (180, 0.0), (225, 45.0), (-30, 150.0), (179.96, 0.0)],
)
def test_rotation_normalises_to_half_a_turn(angle: float, expected: float) -> None:
    assert normalised(angle) == expected


def test_rotation_steps_from_the_last_angle_or_starts_at_the_base() -> None:
    assert stepped(None, 45, 30) == 30.0
    assert stepped(30, 45, 0) == 75.0
    assert stepped(165, 45, 0) == 30.0  # 210 is the stripe at 30
    assert step_sequence(None, 45, 30, 5) == [30.0, 75.0, 120.0, 165.0, 30.0]
    assert step_sequence(90, 90, 0, 3) == [0.0, 90.0, 0.0]
    assert list_sequence([0, 90, 200], 2, 4) == [20.0, 0.0, 90.0, 20.0]
    assert list_sequence([], 0, 3) == []


def test_rotation_continues_from_the_area_mowed_last() -> None:
    angles = [_angle("front", 10, 0), _angle("back", 100, 5), _angle("side", 50, 9)]
    assert last_angle(angles, ["front", "back"]) == 100  # back was mowed later
    assert last_angle(angles, ["front"]) == 10
    assert last_angle(angles, ["pond"]) is None


@pytest.mark.parametrize(
    ("step", "ok"),
    [
        (1, True),
        (45, True),
        (179, True),
        (0, False),
        (0.5, False),
        (180, False),
        (float("nan"), False),
    ],
)
def test_rotation_step_range(step: float, ok: bool) -> None:
    assert valid_step(step) is ok


def test_rotation_skips_patterns_without_direction() -> None:
    assert directed("stripes") and directed(None)
    assert not directed("edge_only") and not directed("random_angle")
    assert run_angle({"name": "edge_only", "angle_deg": 30.0}) is None
    assert run_angle({"name": "checkerboard", "angle_deg": 30.0}) == 30.0


def _schedule(**values: Any) -> Schedule:
    now = FIRST
    return Schedule(
        id=uuid4(),
        account_id=ACCOUNT,
        name="Rotation",
        target_type="mower",
        target_id=MOWER,
        kind="weekly",
        start_time=now.time(),
        duration_minutes=60,
        timezone="Europe/Berlin",
        owner={},
        owner_subject=uuid4(),
        created_at=now,
        updated_at=now,
        **values,
    )


def test_step_angle_of_a_schedule() -> None:
    angles = [_angle("front", 120, 0)]
    stepping = _schedule(rotation_step_deg=90, pattern={"angle_deg": 15})
    assert step_angle(stepping, None, angles, ["front"]) == 30.0  # 120 + 90 = 210
    assert step_angle(stepping, None, [], ["front"]) == 15.0  # the schedule's base
    # No angle in the schedule: the mow defaults', else 0.
    plain = _schedule(rotation_step_deg=90)
    assert step_angle(plain, {"angle_deg": 60}, [], ["front"]) == 60.0
    assert step_angle(plain, None, [], ["front"]) == 0.0
    assert step_angle(_schedule(), None, angles, ["front"]) is None  # no step rotation
    edges = _schedule(rotation_step_deg=45, pattern={"name": "edge_only"})
    assert step_angle(edges, None, angles, ["front"]) is None


# --- end to end --------------------------------------------------------------------------------


async def _fire(h: Harness, day: int) -> None:
    h.clock.set(FIRST + timedelta(days=day, seconds=10))
    assert await h.tick() == 1


def _angles_sent(h: Harness) -> list[float]:
    return [
        e.command.model_dump()["pattern"]["angle_deg"]
        for e in h.commands
        if e.command.kind == "start_mow"
    ]


async def _mower_angles(h: Harness, person: Person, mower_id: UUID) -> dict[str, Any]:
    response = await h.client.get(f"{API}/mowers/{mower_id}/angles", headers=person.headers)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


async def test_scheduler_step_rotation_turns_every_run() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        created = await h.schedule(
            owner,
            target_type="mower",
            target_id=str(mower_id),
            pattern={"angle_deg": 30},
            rotation_step_deg=45,
        )
        assert created["rotation_step_deg"] == 45
        preview = await h.client.get(
            f"{API}/schedules/{created['id']}/preview?count=5", headers=owner.headers
        )
        for day in range(5):
            await _fire(h, day)
        runs = await h.runs(owner, created["id"])
        state = await _mower_angles(h, owner, mower_id)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["angle_mower_id"] == str(mower_id)
    assert [i["angle_deg"] for i in body["items"]] == [30.0, 75.0, 120.0, 165.0, 30.0]
    assert _angles_sent(h) == [30.0, 75.0, 120.0, 165.0, 30.0]
    assert [r["angle_deg"] for r in reversed(runs)] == [30.0, 75.0, 120.0, 165.0, 30.0]
    assert [e["angle_deg"] for e in h.events("scheduler.run.recorded")] == [
        30.0,
        75.0,
        120.0,
        165.0,
        30.0,
    ]
    assert state["areas"] == [
        {
            "area_id": "front",
            "angle_deg": 30.0,
            "used_at": "2026-10-13T08:30:10Z",
            "source": "schedule",
            "schedule_id": created["id"],
        }
    ]


async def test_scheduler_rotation_continues_across_schedules_and_manual_starts() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        target = {"target_type": "mower", "target_id": str(mower_id)}
        # A schedule with a fixed angle mows "front" first: its angle counts as the last one.
        await h.schedule(owner, **target, pattern={"angle_deg": 10})
        await _fire(h, 0)
        rotating = await h.schedule(
            owner, **target, name="Rotate", start_time="11:30", rotation_step_deg=90
        )
        h.clock.set(FIRST + timedelta(hours=1, seconds=10))
        assert await h.tick() == 1  # the rotating schedule: 10 + 90
        # A manual start with rotation chosen reports its angle.
        recorded = await h.client.post(
            f"{API}/mowers/{mower_id}/angles",
            headers=owner.headers,
            json={"area_ids": ["front", "front"], "angle_deg": 135},
        )
        preview = await h.client.get(
            f"{API}/schedules/{rotating['id']}/preview?count=2", headers=owner.headers
        )
    assert _angles_sent(h) == [10.0, 100.0]
    assert recorded.status_code == 200, recorded.text
    [front] = recorded.json()["areas"]
    assert (front["angle_deg"], front["source"], front["schedule_id"]) == (135.0, "manual", None)
    # The next firings continue from the manual start's angle.
    assert [i["angle_deg"] for i in preview.json()["items"]] == [45.0, 135.0]


async def test_scheduler_rotation_list_and_fixed_angle_in_the_preview() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        await h.capabilities(mower_id, owner.account_id, native_patterns=PATTERNS)
        target = {"target_type": "mower", "target_id": str(mower_id)}
        listed = await h.schedule(owner, **target, rotation_angles=[0, 60, 120])
        fixed = await h.schedule(owner, **target, name="Fixed", pattern={"angle_deg": 75})
        edges = await h.schedule(
            owner, **target, name="Edges", pattern={"name": "edge_only", "angle_deg": 75}
        )
        skipped = await h.client.post(
            f"{API}/schedules/{listed['id']}/skip-next", headers=owner.headers
        )
        assert skipped.status_code == 200, skipped.text

        async def angles(schedule: dict[str, Any]) -> list[float | None]:
            response = await h.client.get(
                f"{API}/schedules/{schedule['id']}/preview?count=4", headers=owner.headers
            )
            assert response.status_code == 200, response.text
            return [i["angle_deg"] for i in response.json()["items"]]

        # The skipped firing has no angle and does not advance the list.
        assert await angles(listed) == [None, 0.0, 60.0, 120.0]
        assert await angles(fixed) == [75.0] * 4
        assert await angles(edges) == [None] * 4


async def test_scheduler_undirected_patterns_store_no_angle() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        await h.capabilities(mower_id, owner.account_id, native_patterns=PATTERNS)
        created = await h.schedule(
            owner,
            target_type="mower",
            target_id=str(mower_id),
            pattern={"name": "random_angle"},
            rotation_step_deg=45,
        )
        await _fire(h, 0)
        [run] = await h.runs(owner, created["id"])
        state = await _mower_angles(h, owner, mower_id)
    assert run["outcome"] == "started" and run["angle_deg"] is None
    assert state["areas"] == []


async def test_scheduler_group_rotation_follows_each_mower() -> None:
    async with harness() as h:
        owner = await h.owner()
        group = await h.fleet_post(owner, "groups", name="Both")
        first = await h.register(owner, group_ids=[group["id"]])
        second = await h.register(owner, group_ids=[group["id"]])
        for mower_id in (first, second):
            await h.map(mower_id, owner.account_id, areas=["lawn"])
        # Only the first mower mowed before.
        await h.client.post(
            f"{API}/mowers/{first}/angles",
            headers=owner.headers,
            json={"area_ids": ["lawn"], "angle_deg": 60},
        )
        await h.schedule(
            owner, target_type="group", target_id=group["id"], area_ids=[], rotation_step_deg=30
        )
        await _fire(h, 0)
    sent = {
        e.mower_id: e.command.model_dump()["pattern"]["angle_deg"]
        for e in h.commands
        if e.command.kind == "start_mow"
    }
    assert sent == {first: 90.0, second: 0.0}


@pytest.mark.parametrize(
    "body",
    [
        {"rotation_step_deg": 0},
        {"rotation_step_deg": 180},
        {"rotation_step_deg": 45, "rotation_angles": [0, 90]},
    ],
)
async def test_scheduler_refuses_invalid_rotation(body: dict[str, Any]) -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        response = await h.create(owner, target_type="mower", target_id=str(mower_id), **body)
    assert response.status_code == 422, response.text


async def test_scheduler_rotation_is_switched_by_patch() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        created = await h.schedule(owner, target_type="mower", target_id=str(mower_id))
        path = f"{API}/schedules/{created['id']}"
        on = await h.client.patch(path, headers=owner.headers, json={"rotation_step_deg": 60})
        clash = await h.client.patch(path, headers=owner.headers, json={"rotation_angles": [0]})
        off = await h.client.patch(path, headers=owner.headers, json={"rotation_step_deg": None})
    assert on.status_code == 200 and on.json()["rotation_step_deg"] == 60
    assert clash.status_code == 422
    assert clash.json()["detail"] == "rotation_step_deg and rotation_angles exclude each other"
    assert off.status_code == 200 and off.json()["rotation_step_deg"] is None


async def test_scheduler_manual_step_and_rights() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        viewer = await h.member(owner, "viewer")
        stranger = await h.owner()
        settings = f"{API}/mowers/{mower_id}/angles/settings"
        empty = await _mower_angles(h, viewer, mower_id)
        saved = await h.client.put(settings, headers=owner.headers, json={"manual_step_deg": 45})
        seen = await _mower_angles(h, viewer, mower_id)
        refused = await h.client.put(settings, headers=viewer.headers, json={"manual_step_deg": 90})
        report = await h.client.post(
            f"{API}/mowers/{mower_id}/angles",
            headers=viewer.headers,
            json={"area_ids": ["front"], "angle_deg": 10},
        )
        foreign = await h.client.get(f"{API}/mowers/{mower_id}/angles", headers=stranger.headers)
        invalid = await h.client.put(settings, headers=owner.headers, json={"manual_step_deg": 0})
        off = await h.client.put(settings, headers=owner.headers, json={"manual_step_deg": None})
    assert empty == {"mower_id": str(mower_id), "manual_step_deg": None, "areas": []}
    assert saved.status_code == 200 and saved.json()["manual_step_deg"] == 45
    assert seen["manual_step_deg"] == 45
    assert refused.status_code == 403
    assert report.status_code == 403
    assert foreign.status_code == 404
    assert invalid.status_code == 422
    assert off.status_code == 200 and off.json()["manual_step_deg"] is None
