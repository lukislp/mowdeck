# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure (ADR 0082): ``account.erase.entitlements`` removes everything Entitlements
holds of a deleted account, outside the demo, and reports honestly when it could not.

The requests go over the in-memory bus to the real app, as Identity sends them. The store is
the memory store and, with ``MOWDECK_TEST_DATABASE_URL``, PostgreSQL (see
``test_entitlements_db``).
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from entitlements_support import Harness, harness
from mowdeck_contracts.v1 import (
    AccountEraseReply,
    AccountEraseRequest,
    EntitlementCheckRequest,
)
from mowdeck_contracts.v1.subjects import ENTITLEMENTS_CHECK, account_erase_subject
from mowdeck_service_kit.db import account_tables
from mowdeck_service_kit.erasure import ErasureIncomplete
from test_entitlements_db import STORES, store_of
from test_entitlements_demo_purge import fill

from entitlements.adapters.db.tables import metadata
from entitlements.adapters.memory import MemoryTx
from entitlements.application.ports import Store

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
ACCOUNT_TABLES = {table.name for table in account_tables(metadata)}
"""Every table that can hold rows of an account, read from the schema."""

INSTALLATION_TABLES = {"plans", "features"}
"""Tables without an ``account_id`` column: the plans and the feature catalogue belong to the
installation, hold nothing of an account and stay."""


async def erase(h: Harness, account_id: UUID) -> AccountEraseReply:
    """What Identity's ``BusAccountEraser`` sends, and the decoded reply."""
    request = AccountEraseRequest(account_id=account_id, requested_at=NOW)
    message = await h.bus.request(account_erase_subject("entitlements"), request, timeout=5.0)
    return message.model(AccountEraseReply)


async def rows_of(store: Store, account_id: UUID) -> dict[str, int]:
    """The rows of the account per table, as the port reports them."""
    async with store.transaction() as tx:
        return await tx.account_rows(account_id)


# --- the schema ------------------------------------------------------------------------------


def test_only_the_installation_tables_have_no_account() -> None:
    """The erasure counts the tables with an ``account_id`` column. A table without one is
    invisible to it: this fails when one is added, so its rows get an owner or a decision."""
    assert {"account_plans", "plan_history"} == ACCOUNT_TABLES
    assert {table.name for table in metadata.sorted_tables} - ACCOUNT_TABLES == INSTALLATION_TABLES


# --- the request -----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", STORES)
async def test_erasure_removes_every_row_of_one_account_and_keeps_the_other(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone, kept = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:  # not a demo
        await fill(store, gone)
        await fill(store, kept)
        before = await rows_of(store, gone)
        kept_rows = await rows_of(store, kept)
        async with store.transaction() as tx:
            plans = await tx.list_plans()
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
            assert await tx.list_plans() == plans  # the installation's plans stay


async def test_a_check_after_the_erasure_creates_no_row_again() -> None:
    """Another service may still ask about the account for a moment: that must not bring a
    row back."""
    account_id = uuid4()
    async with harness(entitlements_billing_enabled=True) as h:
        await h.account_created(account_id)
        assert await rows_of(h.store, account_id) != {}
        assert (await erase(h, account_id)).ok

        await h.bus.request(
            ENTITLEMENTS_CHECK, EntitlementCheckRequest(account_id=account_id), timeout=1.0
        )

        assert await rows_of(h.store, account_id) == {}


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

        assert left == {"account_plans": 1, "plan_history": 2}
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
            ok=False, deleted=0, error="ConnectionError: see the log of entitlements"
        )
        assert await rows_of(h.store, account_id) != {}
