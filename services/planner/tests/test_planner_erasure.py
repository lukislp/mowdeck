# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure (ADR 0082): ``account.erase.planner`` removes everything the Planner holds
of a deleted account, outside the demo, and reports honestly when it could not.

The requests go over the in-memory bus to the real app, as Identity sends them. The store is
the memory store and, with ``MOWDECK_TEST_DATABASE_URL``, PostgreSQL (see ``test_planner_db``).
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import AccountEraseReply, AccountEraseRequest
from mowdeck_contracts.v1.subjects import account_erase_subject
from mowdeck_service_kit.db import account_tables
from mowdeck_service_kit.erasure import ErasureIncomplete
from planner_support import Harness, harness
from test_planner_db import STORES, _template, store_of

from planner.adapters.db.tables import metadata
from planner.adapters.memory import MemoryTx
from planner.application.ports import Store

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
ACCOUNT_TABLES = {table.name for table in account_tables(metadata)}
"""Every table that can hold rows of an account, read from the schema."""


async def erase(h: Harness, account_id: UUID) -> AccountEraseReply:
    """What Identity's ``BusAccountEraser`` sends, and the decoded reply."""
    request = AccountEraseRequest(account_id=account_id, requested_at=NOW)
    message = await h.bus.request(account_erase_subject("planner"), request, timeout=5.0)
    return message.model(AccountEraseReply)


async def rows_of(store: Store, account_id: UUID) -> dict[str, int]:
    """The rows of the account per table, as the port reports them."""
    async with store.transaction() as tx:
        return await tx.account_rows(account_id)


async def fill(store: Store, account_id: UUID) -> UUID:
    """Two templates and the map and capability facts of one mower; the mower id."""
    mower_id = uuid4()
    async with store.transaction() as tx:
        await tx.put_template(_template(account_id, "a"))
        await tx.put_template(_template(account_id, "b"))
        await tx.put_map(mower_id, account_id, NOW, {"areas": [1]})
        await tx.put_capabilities(mower_id, account_id, NOW, {"custom_paths": True})
    return mower_id


# --- the schema ------------------------------------------------------------------------------


def test_every_table_of_the_planner_belongs_to_an_account() -> None:
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
        await fill(store, gone)
        kept_mower = await fill(store, kept)
        before = await rows_of(store, gone)
        kept_rows = await rows_of(store, kept)
        # The seed reaches every table the schema gives an account, so none is checked empty.
        assert set(before) == ACCOUNT_TABLES

        first = await erase(h, gone)
        second = await erase(h, gone)
        unknown = await erase(h, uuid4())

        assert first == AccountEraseReply(ok=True, deleted=3)
        assert second == AccountEraseReply(ok=True, deleted=0)
        assert unknown == AccountEraseReply(ok=True, deleted=0)
        assert await rows_of(store, gone) == {}
        assert await rows_of(store, kept) == kept_rows
        async with store.transaction() as tx:
            assert await tx.get_facts(kept_mower) is not None


async def test_erasure_reports_rows_that_remain_and_a_retry_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A purge that leaves rows behind is never answered as a success."""

    async def purge_nothing(self: MemoryTx, account_id: UUID) -> int:
        del self, account_id
        return 0

    account_id = uuid4()
    async with harness() as h:
        await fill(h.store, account_id)
        with monkeypatch.context() as patched:
            patched.setattr(MemoryTx, "purge_account", purge_nothing)
            failed = await erase(h, account_id)
        left = await rows_of(h.store, account_id)
        retried = await erase(h, account_id)

        assert left == {"templates": 2, "mower_facts": 1}
        assert failed == AccountEraseReply(ok=False, deleted=0, error=str(ErasureIncomplete(left)))
        assert retried == AccountEraseReply(ok=True, deleted=3)
        assert await rows_of(h.store, account_id) == {}


async def test_erasure_reports_a_failing_purge(monkeypatch: pytest.MonkeyPatch) -> None:
    async def purge_fails(self: MemoryTx, account_id: UUID) -> int:
        del self, account_id
        raise ConnectionError("database is down")

    account_id = uuid4()
    async with harness() as h:
        await fill(h.store, account_id)
        monkeypatch.setattr(MemoryTx, "purge_account", purge_fails)

        failed = await erase(h, account_id)

        assert failed == AccountEraseReply(
            ok=False, deleted=0, error="ConnectionError: see the log of planner"
        )
        assert await rows_of(h.store, account_id) != {}
