"""Demo sandboxes (ADR 0076): ``DELETE /api/v1/fleet/demo/accounts/{id}`` removes everything
Fleet holds of one account (rows, tuples, ``mower.removed`` per mower) and nothing else."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fleet_support import API, Harness, Person, harness
from mowdeck_contracts.v1 import EventEnvelope, MowerRegistration
from mowdeck_service_kit.demo import DEMO_SEED_HEADER

from fleet.adapters.memory import MemoryStore
from fleet.domain.models import (
    BaseStation,
    DriveSession,
    LinkedAccount,
    MowerAreaName,
    MowerLink,
)

SEED = "demo-seed-secret"
DEMO: dict[str, Any] = {"demo_mode": True, "demo_seed_token": SEED}
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def purge_path(account_id: UUID) -> str:
    return f"{API}/demo/accounts/{account_id}"


def rows_of(store: MemoryStore, account_id: UUID) -> dict[str, int]:
    """Rows per memory table that belong to the account."""
    counts: dict[str, int] = {}
    for name in vars(store.tables):
        table = getattr(store.tables, name)
        counts[name] = sum(1 for row in table.rows.values() if row.account_id == account_id)
    return counts


async def seed_account(h: Harness, owner: Person) -> list[str]:
    """Sites, groups, mowers, a command and the rows the demo API refuses; the mower ids."""
    seeded = {**owner.headers, DEMO_SEED_HEADER: SEED}
    home = await h.create(owner, "sites", name="Home")
    north = await h.create(owner, "groups", name="North", site_id=home["id"])
    await h.create(owner, "groups", name="Everywhere")
    mower_ids: list[str] = []
    for body in (
        {"name": "Grouped", "site_id": home["id"], "group_ids": [north["id"]]},
        {"name": "Default"},  # the hidden default site
    ):
        response = await h.client.post(
            f"{API}/mowers", headers=seeded, json={"connection_kind": "simulated", **body}
        )
        assert response.status_code == 201, response.text
        mower_ids.append(response.json()["id"])
    command = await h.client.post(
        f"{API}/mowers/{mower_ids[0]}/commands",
        headers=owner.headers,
        json={"command": {"kind": "pause_mow"}},
    )
    assert command.status_code == 202, command.text
    await add_hidden_rows(h.store, owner.account_id, UUID(mower_ids[0]))
    return mower_ids


async def add_hidden_rows(store: MemoryStore, account_id: UUID, mower_id: UUID) -> None:
    """Rows the demo API refuses to visitors: linked account, base station, link, area name,
    drive session."""
    async with store.transaction() as tx:
        linked = LinkedAccount(uuid4(), account_id, "acme", "a@x.test", b"x", NOW, NOW)
        await tx.linked_accounts.add(linked)
        await tx.base_stations.add(
            BaseStation(uuid4(), account_id, "RTK", "cloud", NOW, NOW, linked.id)
        )
        await tx.mower_links.add(
            MowerLink(uuid4(), mower_id, account_id, "ble_bridge", b"x", NOW, NOW)
        )
        await tx.area_names.add(MowerAreaName(mower_id, "front", account_id, "Front", NOW))
        await tx.drive_sessions.add(
            DriveSession(
                uuid4(), account_id, mower_id, uuid4(), {}, NOW, NOW + timedelta(minutes=5)
            )
        )


def object_ids(store: MemoryStore, account_id: UUID) -> set[str]:
    """``type:id`` of every site, group and mower of the account."""
    found: set[str] = set()
    for name, kind in (("sites", "site"), ("groups", "group"), ("mowers", "mower")):
        table = getattr(store.tables, name)
        found |= {f"{kind}:{r.id}" for r in table.rows.values() if r.account_id == account_id}
    return found


def removed_events(h: Harness) -> list[EventEnvelope[MowerRegistration]]:
    return [
        m.model(EventEnvelope[MowerRegistration]) for m in h.bus.messages("event.mower.removed")
    ]


async def test_purge_is_hidden_outside_the_demo() -> None:
    async with harness() as h:
        response = await h.client.delete(purge_path(uuid4()), headers={DEMO_SEED_HEADER: SEED})
    assert response.status_code == 404, response.text


async def test_purge_needs_the_seed_secret() -> None:
    async with harness(**DEMO) as h:
        owner = await h.owner()
        missing = await h.client.delete(purge_path(owner.account_id), headers=owner.headers)
        wrong = await h.client.delete(
            purge_path(owner.account_id), headers={**owner.headers, DEMO_SEED_HEADER: "nope"}
        )
    assert missing.status_code == 403, missing.text
    assert wrong.status_code == 403, wrong.text


async def test_purge_removes_one_account_and_keeps_the_other() -> None:
    async with harness(**DEMO) as h:
        alice, bob = await h.owner(), await h.owner()
        alice_mowers = await seed_account(h, alice)
        bob_mowers = await seed_account(h, bob)
        alice_objects = object_ids(h.store, alice.account_id)
        bob_objects = object_ids(h.store, bob.account_id)
        bob_rows = rows_of(h.store, bob.account_id)
        bob_tuples = {o: h.tuples_on(o) for o in bob_objects}
        assert all(rows_of(h.store, alice.account_id).values()), "every table has rows"
        expected = sum(rows_of(h.store, alice.account_id).values())

        first = await h.client.delete(
            purge_path(alice.account_id), headers={DEMO_SEED_HEADER: SEED}
        )
        second = await h.client.delete(
            purge_path(alice.account_id), headers={DEMO_SEED_HEADER: SEED}
        )
        listed = await h.client.get(f"{API}/mowers", headers=bob.headers)

        assert first.status_code == 200, first.text
        # Every row of the account plus the two "mower.removed" audit rows written on the way.
        assert first.json() == {"deleted": expected + len(alice_mowers)}
        assert second.status_code == 200 and second.json() == {"deleted": 0}
        assert set(rows_of(h.store, alice.account_id).values()) == {0}
        assert all(not h.tuples_on(o) for o in alice_objects)
        removed = removed_events(h)
        assert sorted(str(e.payload.mower_id) for e in removed) == sorted(alice_mowers)
        assert {e.account_id for e in removed} == {alice.account_id}

        assert rows_of(h.store, bob.account_id) == bob_rows
        assert {o: h.tuples_on(o) for o in bob_objects} == bob_tuples
        assert {m["id"] for m in listed.json()["items"]} == set(bob_mowers)


async def test_purge_of_an_account_without_data_answers_zero() -> None:
    async with harness(**DEMO) as h:
        response = await h.client.delete(purge_path(uuid4()), headers={DEMO_SEED_HEADER: SEED})
    assert response.status_code == 200 and response.json() == {"deleted": 0}


async def test_purge_removes_the_default_site_of_a_new_account() -> None:
    async with harness(**DEMO) as h:
        account_id = uuid4()
        default = await h.app.state.fleet.sites.ensure_default(account_id)
        response = await h.client.delete(purge_path(account_id), headers={DEMO_SEED_HEADER: SEED})
    assert response.json() == {"deleted": 1}
    assert not h.tuples_on(f"site:{default.id}")
