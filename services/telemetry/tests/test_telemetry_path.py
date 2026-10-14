"""The mow path snapshot (device.mower.<id>.path), served while a job runs."""

from uuid import UUID

from mowdeck_contracts.v1 import MowerPathSnapshot, MowPath, MowPathProgress
from mowdeck_contracts.v1.subjects import device_subject
from telemetry_support import API, T0, Harness, harness, seconds


def a_path(index: int = 2) -> MowPath:
    return MowPath(
        path_hash="18446744073709551557",
        polylines=[[(0.0, 0.0), (4.0, 0.0)], [(4.0, 0.5), (0.0, 0.5)]],
        kinds=["edge", "mow"],
        points_total=4,
        progress=MowPathProgress(index=index, direction=1),
        area_ids=["1"],
        planned_at=T0,
    )


async def publish_path(
    h: Harness, mower_id: UUID, account_id: UUID, path: MowPath | None, at: float = 0
) -> None:
    snapshot = MowerPathSnapshot(
        mower_id=mower_id, account_id=account_id, observed_at=T0 + seconds(at), path=path
    )
    await h.bus.publish(device_subject(mower_id, "path"), snapshot)


async def test_telemetry_serves_the_mow_path_of_the_running_job() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        url = f"{API}/mowers/{mower_id}/path"
        before = await h.client.get(url, headers=owner.headers)
        await publish_path(h, mower_id, owner.account_id, a_path(3), at=10)
        await publish_path(h, mower_id, owner.account_id, a_path(1), at=0)  # older: ignored
        running = await h.client.get(url, headers=owner.headers)
        await publish_path(h, mower_id, owner.account_id, None, at=20)  # the job ended
        ended = await h.client.get(url, headers=owner.headers)
    assert before.status_code == 404
    assert before.json()["type"] == "urn:mowdeck:problem:no-path"
    assert running.status_code == 200, running.text
    body = running.json()
    assert body["path"]["path_hash"] == "18446744073709551557"
    assert body["path"]["progress"] == {"index": 3, "direction": 1}
    assert body["path"]["kinds"] == ["edge", "mow"]
    assert ended.status_code == 404
    assert ended.json()["type"] == "urn:mowdeck:problem:no-path"


async def test_telemetry_mow_path_of_a_foreign_mower_is_hidden() -> None:
    async with harness() as h:
        owner = await h.owner()
        intruder = await h.owner()
        mower_id = await h.add_mower(owner)
        await publish_path(h, mower_id, owner.account_id, a_path())
        foreign = await h.client.get(f"{API}/mowers/{mower_id}/path", headers=intruder.headers)
    assert foreign.status_code == 404
    assert foreign.json()["type"] == "urn:mowdeck:problem:not-found"
