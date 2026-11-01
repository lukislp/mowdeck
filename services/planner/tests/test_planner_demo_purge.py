"""Demo sandboxes (ADR 0076): ``DELETE /api/v1/planner/demo/accounts/{id}`` removes the
templates and mower facts of one account and nothing of another."""

from typing import Any
from uuid import UUID, uuid4

from mowdeck_service_kit.demo import DEMO_SEED_HEADER
from planner_support import API, Harness, Person, harness

from planner.adapters.memory import MemoryStore

SEED = "demo-seed-secret"
SEEDED = {DEMO_SEED_HEADER: SEED}
CHAIN: list[dict[str, Any]] = [{"pattern": "edge_only", "settings": {"laps": 2}}]


def purge_path(account_id: UUID) -> str:
    return f"{API}/demo/accounts/{account_id}"


def rows_of(store: MemoryStore, account_id: UUID) -> int:
    """Templates and mower facts of the account in the memory store."""
    templates = sum(1 for acc, _ in store.data.templates if acc == account_id)
    return templates + sum(1 for f in store.data.facts.values() if f.account_id == account_id)


async def seed_account(h: Harness, owner: Person) -> UUID:
    """A mower with map and capabilities and two templates; the mower id."""
    mower_id = await h.mower(owner)
    for name in ("Edges", "Stripes"):
        saved = await h.client.put(
            f"{API}/templates/{name}", headers=owner.headers, json={"chain": CHAIN}
        )
        assert saved.status_code in (200, 201), saved.text
    return mower_id


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
        bobs_mower = await seed_account(h, bob)
        assert rows_of(h.store, alice.account_id) == 3  # two templates, one mower's facts
        bobs_rows = rows_of(h.store, bob.account_id)

        first = await h.client.delete(purge_path(alice.account_id), headers=SEEDED)
        second = await h.client.delete(purge_path(alice.account_id), headers=SEEDED)
        listed = await h.client.get(f"{API}/templates", headers=alice.headers)

        assert first.status_code == 200 and first.json() == {"deleted": 3}, first.text
        assert second.status_code == 200 and second.json() == {"deleted": 0}
        assert rows_of(h.store, alice.account_id) == 0
        assert listed.json()["items"] == []
        assert rows_of(h.store, bob.account_id) == bobs_rows
        assert bobs_mower in h.store.data.facts


async def test_purge_of_an_account_without_data_answers_zero() -> None:
    async with harness(demo_mode=True, demo_seed_token=SEED) as h:
        response = await h.client.delete(purge_path(uuid4()), headers=SEEDED)
    assert response.status_code == 200 and response.json() == {"deleted": 0}
