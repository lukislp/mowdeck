"""Demo sandboxes (ADR 0076): ``DELETE /api/v1/scheduler/demo/accounts/{id}`` removes every
schedule, run and mower fact of one account and nothing of another."""

from uuid import UUID, uuid4

from mowdeck_contracts.v1 import MowerActivity
from mowdeck_service_kit.demo import DEMO_SEED_HEADER
from scheduler_support import API, Harness, Person, harness

from scheduler.adapters.memory import MemoryStore
from scheduler.domain.rotation import AreaAngle, MowerRotation

SEED = "demo-seed-secret"
SEEDED = {DEMO_SEED_HEADER: SEED}


def purge_path(account_id: UUID) -> str:
    return f"{API}/demo/accounts/{account_id}"


def rows_of(store: MemoryStore, account_id: UUID) -> int:
    """Rows of the account in the memory store, every collection."""
    data = store.data
    return (
        sum(1 for s in data.schedules.values() if s.account_id == account_id)
        + sum(1 for r in data.runs.values() if r.account_id == account_id)
        + sum(1 for s in data.status.values() if s.account_id == account_id)
        + sum(1 for p in data.places.values() if p.account_id == account_id)
        + sum(1 for a in data.capability_accounts.values() if a == account_id)
        + sum(1 for a in data.angles.values() if a.account_id == account_id)
        + sum(1 for r in data.rotation.values() if r.account_id == account_id)
    )


async def seed_account(h: Harness, owner: Person) -> str:
    """A mower with state, map, capabilities, angles and rotation, a schedule and two runs."""
    mower_id = await h.register(owner)
    await h.state(mower_id, owner.account_id, MowerActivity.DOCKED)
    await h.map(mower_id, owner.account_id)
    await h.capabilities(mower_id, owner.account_id)
    created = await h.schedule(owner, target_type="mower", target_id=str(mower_id))
    for _ in range(2):
        h.clock.advance(seconds=1)
        run = await h.client.post(f"{API}/schedules/{created['id']}/run-now", headers=owner.headers)
        assert run.status_code == 202, run.text
    async with h.store.transaction() as tx:
        await tx.put_area_angles(
            [AreaAngle(mower_id, "front", owner.account_id, 30.0, h.clock.now, "manual")]
        )
        await tx.put_rotation(MowerRotation(mower_id, owner.account_id, 45.0, h.clock.now))
    schedule_id: str = created["id"]
    return schedule_id


async def test_purge_is_hidden_outside_the_demo() -> None:
    async with harness() as h:
        response = await h.client.delete(purge_path(uuid4()), headers=SEEDED)
    assert response.status_code == 404, response.text


async def test_purge_needs_the_seed_secret() -> None:
    async with harness(demo_mode=True, demo_seed_token=SEED) as h:
        owner = await h.owner()
        missing = await h.client.delete(purge_path(owner.account_id), headers=owner.headers)
        wrong = await h.client.delete(
            purge_path(owner.account_id), headers={**owner.headers, DEMO_SEED_HEADER: "nope"}
        )
    assert missing.status_code == 403, missing.text
    assert wrong.status_code == 403, wrong.text


async def test_purge_removes_one_account_and_keeps_the_other() -> None:
    async with harness(demo_mode=True, demo_seed_token=SEED) as h:
        alice, bob = await h.owner(), await h.owner()
        await seed_account(h, alice)
        bobs_schedule = await seed_account(h, bob)
        expected, bobs_rows = rows_of(h.store, alice.account_id), rows_of(h.store, bob.account_id)
        assert expected == 8  # schedule, two runs, status, place, capabilities, angle, rotation

        first = await h.client.delete(purge_path(alice.account_id), headers=SEEDED)
        second = await h.client.delete(purge_path(alice.account_id), headers=SEEDED)
        listed = await h.client.get(f"{API}/schedules", headers=alice.headers)

        assert first.status_code == 200 and first.json() == {"deleted": expected}, first.text
        assert second.status_code == 200 and second.json() == {"deleted": 0}
        assert rows_of(h.store, alice.account_id) == 0
        assert listed.json()["items"] == []
        assert rows_of(h.store, bob.account_id) == bobs_rows
        assert len(await h.runs(bob, bobs_schedule)) == 2


async def test_purge_of_an_account_without_data_answers_zero() -> None:
    async with harness(demo_mode=True, demo_seed_token=SEED) as h:
        response = await h.client.delete(purge_path(uuid4()), headers=SEEDED)
    assert response.status_code == 200 and response.json() == {"deleted": 0}
