# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure (ADR 0082): ``account.erase.scheduler`` removes everything the Scheduler
holds of a deleted account, outside the demo, and reports honestly when it could not.

The requests go over the in-memory bus to the real app, as Identity sends them. The store is
the memory store and, with ``MOWDECK_TEST_DATABASE_URL``, PostgreSQL (see
``test_scheduler_db``).
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import AccountEraseReply, AccountEraseRequest, MowerActivity
from mowdeck_contracts.v1.subjects import account_erase_subject
from mowdeck_service_kit.db import account_tables
from mowdeck_service_kit.erasure import ErasureIncomplete
from scheduler_support import Harness, harness
from test_scheduler_db import STORES, _fill, store_of

from scheduler.adapters.db.tables import metadata
from scheduler.adapters.memory import MemoryTx
from scheduler.application.ports import Store

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
ACCOUNT_TABLES = {table.name for table in account_tables(metadata)}
"""Every table that can hold rows of an account, read from the schema."""


async def erase(h: Harness, account_id: UUID) -> AccountEraseReply:
    """What Identity's ``BusAccountEraser`` sends, and the decoded reply."""
    request = AccountEraseRequest(account_id=account_id, requested_at=NOW)
    message = await h.bus.request(account_erase_subject("scheduler"), request, timeout=5.0)
    return message.model(AccountEraseReply)


async def rows_of(store: Store, account_id: UUID) -> dict[str, int]:
    """The rows of the account per table, as the port reports them."""
    async with store.transaction() as tx:
        return await tx.account_rows(account_id)


# --- the schema ------------------------------------------------------------------------------


def test_every_table_of_the_scheduler_belongs_to_an_account() -> None:
    """The erasure counts the tables with an ``account_id`` column. A table without one would
    be invisible to it: this fails when one is added, so its rows get an owner or a decision."""
    assert {table.name for table in metadata.sorted_tables} - ACCOUNT_TABLES == set()


# --- the request -----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", STORES)
async def test_erasure_removes_every_row_of_one_account_and_keeps_the_other(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone, kept = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:  # not a demo
        await _fill(store, gone)
        kept_schedule = await _fill(store, kept)
        before = await rows_of(store, gone)
        kept_rows = await rows_of(store, kept)
        # The seed reaches every table the schema gives an account, so none is checked empty.
        assert set(before) == ACCOUNT_TABLES

        first = await erase(h, gone)
        second = await erase(h, gone)
        unknown = await erase(h, uuid4())

        assert first == AccountEraseReply(ok=True, deleted=sum(before.values()))
        assert second == AccountEraseReply(ok=True, deleted=0)
        assert unknown == AccountEraseReply(ok=True, deleted=0)
        assert await rows_of(store, gone) == {}
        assert await rows_of(store, kept) == kept_rows
        async with store.transaction() as tx:
            assert await tx.get_schedule(kept_schedule.id) is not None


async def test_erasure_reports_rows_that_remain_and_a_retry_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A purge that leaves rows behind is never answered as a success."""

    async def purge_nothing(self: MemoryTx, account_id: UUID) -> int:
        del self, account_id
        return 0

    account_id = uuid4()
    async with harness() as h:
        await _fill(h.store, account_id)
        left = await rows_of(h.store, account_id)
        with monkeypatch.context() as patched:
            patched.setattr(MemoryTx, "purge_account", purge_nothing)
            failed = await erase(h, account_id)
        assert await rows_of(h.store, account_id) == left
        retried = await erase(h, account_id)

        assert failed == AccountEraseReply(ok=False, deleted=0, error=str(ErasureIncomplete(left)))
        assert "schedules: 1" in (failed.error or "")
        assert retried == AccountEraseReply(ok=True, deleted=sum(left.values()))
        assert await rows_of(h.store, account_id) == {}


async def test_erasure_reports_a_failing_purge(monkeypatch: pytest.MonkeyPatch) -> None:
    async def purge_fails(self: MemoryTx, account_id: UUID) -> int:
        del self, account_id
        raise ConnectionError("database is down")

    account_id = uuid4()
    async with harness() as h:
        await _fill(h.store, account_id)
        monkeypatch.setattr(MemoryTx, "purge_account", purge_fails)

        failed = await erase(h, account_id)

        assert failed == AccountEraseReply(
            ok=False, deleted=0, error="ConnectionError: see the log of scheduler"
        )
        assert await rows_of(h.store, account_id) != {}


# --- what a real account has and a demo sandbox does not --------------------------------------


async def test_erasure_forgets_the_statuses_the_ingest_remembers() -> None:
    """The ingest keeps the last written status (with the position) per mower in memory."""
    async with harness() as h:
        alice, bob = await h.owner(), await h.owner()
        mowers = {owner.account_id: await h.register(owner) for owner in (alice, bob)}
        for account_id, mower_id in mowers.items():
            await h.state(mower_id, account_id, MowerActivity.DOCKED)
        remembered = h.app.state.scheduler.ingest._written
        assert set(remembered) == set(mowers.values())

        reply = await erase(h, alice.account_id)

        assert reply.ok and reply.deleted == 1  # the status row
        assert set(remembered) == {mowers[bob.account_id]}
