# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure (ADR 0082): ``account.erase.fleet`` removes everything Fleet holds of a
deleted account, outside the demo, and reports honestly when it could not.

The requests go over the in-memory bus to the real app, as Identity sends them. The store is
the memory store and, with ``MOWDECK_TEST_DATABASE_URL``, PostgreSQL (see ``test_fleet_db``).
"""

import dataclasses
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fleet_support import API, FlakyBackend, Harness, harness
from mowdeck_contracts.v1 import AccountEraseReply, AccountEraseRequest
from mowdeck_contracts.v1.subjects import account_erase_subject, command_subject
from mowdeck_service_kit.db import account_tables
from mowdeck_service_kit.erasure import ErasureIncomplete
from test_fleet_db import STORES, store_of
from test_fleet_demo_purge import object_ids, removed_events, seed_account

from fleet.adapters.db.store import SqlStore
from fleet.adapters.db.tables import metadata
from fleet.adapters.memory import MemoryTables
from fleet.application.demo_purge import AccountPurge
from fleet.application.ports import Store, Tx
from fleet.application.throttle import LoginThrottle

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


async def erase(h: Harness, account_id: UUID) -> AccountEraseReply:
    """What Identity's ``BusAccountEraser`` sends, and the decoded reply."""
    request = AccountEraseRequest(account_id=account_id, requested_at=NOW)
    message = await h.bus.request(account_erase_subject("fleet"), request, timeout=5.0)
    return message.model(AccountEraseReply)


async def rows_of(store: Store, account_id: UUID) -> dict[str, int]:
    """The rows of the account per table, as the port reports them."""
    async with store.transaction() as tx:
        return await tx.account_rows(account_id)


def account_table_names(store: Store) -> set[str]:
    """Every table that can hold rows of an account: read from the schema for PostgreSQL."""
    if isinstance(store, SqlStore):
        return {table.name for table in account_tables(metadata)}
    return {field.name for field in dataclasses.fields(MemoryTables)}


# --- the schema ------------------------------------------------------------------------------


def test_every_table_of_fleet_belongs_to_an_account() -> None:
    """The erasure counts the tables with an ``account_id`` column. A table without one would
    be invisible to it: this fails when one is added, so its rows get an owner or a decision."""
    without = {table.name for table in metadata.sorted_tables} - {
        table.name for table in account_tables(metadata)
    }
    assert without == set()


# --- the request -----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", STORES)
async def test_erasure_removes_every_row_of_one_account_and_keeps_the_other(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:  # not a demo
        alice, bob = await h.owner(), await h.owner()
        alice_mowers = await seed_account(h, alice)
        await seed_account(h, bob)
        memory = kind == "memory"  # the helper reads the object ids from the memory tables
        alice_objects = object_ids(h.store, alice.account_id) if memory else set()
        bob_objects = object_ids(h.store, bob.account_id) if memory else set()
        bob_tuples = {obj: h.tuples_on(obj) for obj in bob_objects}
        before = await rows_of(store, alice.account_id)
        bob_rows = await rows_of(store, bob.account_id)
        # The seed reaches every table the schema gives an account, so none is checked empty.
        assert set(before) == account_table_names(store)

        # The command of the seed is still in the COMMANDS stream.
        messages = sum([await h.bus.count_subject(command_subject(UUID(m))) for m in alice_mowers])
        assert messages == 1

        first = await erase(h, alice.account_id)
        second = await erase(h, alice.account_id)
        unknown = await erase(h, uuid4())

        assert first.ok and first.error is None
        # Every row of the account, the "mower.removed" audit rows written on the way, and the
        # messages the streams held of its mowers.
        assert first.deleted == sum(before.values()) + len(alice_mowers) + messages
        assert second == AccountEraseReply(ok=True, deleted=0)
        assert unknown == AccountEraseReply(ok=True, deleted=0)
        assert await rows_of(store, alice.account_id) == {}
        assert all(not h.tuples_on(obj) for obj in alice_objects)
        removed = removed_events(h)
        assert sorted(str(e.payload.mower_id) for e in removed) == sorted(alice_mowers)
        assert await rows_of(store, bob.account_id) == bob_rows
        assert {obj: h.tuples_on(obj) for obj in bob_objects} == bob_tuples
        listed = await h.client.get(f"{API}/mowers", headers=bob.headers)
        assert len(listed.json()["items"]) == 2


async def test_erasure_reports_rows_that_remain_and_a_retry_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A purge that leaves rows behind is never answered as a success."""

    async def skip_records(tx: Tx, account_id: UUID) -> int:
        del tx, account_id
        return 0

    async with harness() as h:
        alice = await h.owner()
        await seed_account(h, alice)
        with monkeypatch.context() as patched:
            patched.setattr(AccountPurge, "_remove_records", staticmethod(skip_records))
            failed = await erase(h, alice.account_id)
        left = await rows_of(h.store, alice.account_id)
        retried = await erase(h, alice.account_id)

        assert failed.ok is False and failed.deleted == 0
        assert failed.error == str(ErasureIncomplete(left))
        assert {"sites", "commands", "audit", "linked_accounts"} <= set(left)
        assert retried.ok and retried.deleted == sum(left.values())
        assert await rows_of(h.store, alice.account_id) == {}


async def test_erasure_reports_a_failing_purge_and_continues_on_the_retry() -> None:
    """OpenFGA is down while the mowers are removed: no success, and the retry finishes."""
    async with harness(backend=FlakyBackend) as h:
        alice = await h.owner()
        await seed_account(h, alice)
        h.backend.fail_writes = True
        failed = await erase(h, alice.account_id)
        assert await rows_of(h.store, alice.account_id) != {}
        h.backend.fail_writes = False
        retried = await erase(h, alice.account_id)

        assert failed == AccountEraseReply(
            ok=False, deleted=0, error="Unavailable: see the log of fleet"
        )
        assert retried.ok and retried.deleted > 0
        assert await rows_of(h.store, alice.account_id) == {}


# --- what a real account has and a demo sandbox does not --------------------------------------


def test_throttle_forgets_the_vendor_identities_of_one_account() -> None:
    throttle = LoginThrottle(lambda: NOW)
    gone, kept = uuid4(), uuid4()
    for account_id, email in ((gone, "acme:Ada@x.test"), (gone, "acme:bob@x.test"), (kept, "a@x")):
        throttle.failed(throttle.key(account_id, email), "wrong")

    assert throttle.forget_account(gone) == 2
    assert throttle.forget_account(gone) == 0
    assert list(throttle._states) == [throttle.key(kept, "a@x")]


async def test_erasure_forgets_the_failed_vendor_logins_of_the_account() -> None:
    """The throttle's key holds the vendor e-mail address: it goes with the account."""
    async with harness() as h:
        alice, bob = await h.owner(), await h.owner()
        throttle: LoginThrottle = h.app.state.fleet.linked_accounts.throttle
        for owner in (alice, bob):
            throttle.failed(throttle.key(owner.account_id, "acme:ada@x.test"), "wrong")

        reply = await erase(h, alice.account_id)

        assert reply == AccountEraseReply(ok=True, deleted=1)  # the throttle entry
        assert list(throttle._states) == [throttle.key(bob.account_id, "acme:ada@x.test")]
