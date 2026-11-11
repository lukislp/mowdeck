# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure (ADR 0082): ``account.erase.telemetry`` removes everything Telemetry holds
of a deleted account, outside the demo, and reports honestly when it could not.

The requests go over the in-memory bus to the real app, as Identity sends them. The stores are
the memory stores and, with ``MOWDECK_TEST_DATABASE_URL``, PostgreSQL (see
``test_telemetry_db``).
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import AccountEraseReply, AccountEraseRequest
from mowdeck_contracts.v1.subjects import account_erase_subject
from mowdeck_service_kit.db import account_tables
from mowdeck_service_kit.erasure import ErasureIncomplete
from telemetry_support import Harness, harness
from test_telemetry_demo_purge import KINDS, empty, everything, fill, stores_of

from telemetry.adapters.db import store as main_store
from telemetry.adapters.db import tracking_store, zone_store
from telemetry.adapters.db.tables import metadata
from telemetry.adapters.memory import MemoryStore
from telemetry.adapters.memory_tracking import MemoryTrackingStore
from telemetry.adapters.memory_zones import MemoryZoneStore
from telemetry.application.ports import Store
from telemetry.application.tracking_ports import TrackingStore
from telemetry.application.zone_ports import ZoneStore

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
ACCOUNT_TABLES = {table.name for table in account_tables(metadata)}
"""Every table that can hold rows of an account, read from the schema."""

MOWER_TABLES = {"mower_positions_thinning"}
"""Tables without an ``account_id`` column: the thinning watermark is keyed by the mower and
goes with the account's mowers (``purge_account`` of the store, covered below)."""


async def erase(h: Harness, account_id: UUID) -> AccountEraseReply:
    """What Identity's ``BusAccountEraser`` sends, and the decoded reply."""
    request = AccountEraseRequest(account_id=account_id, requested_at=NOW)
    message = await h.bus.request(account_erase_subject("telemetry"), request, timeout=10.0)
    return message.model(AccountEraseReply)


async def rows_of(
    store: Store, zones: ZoneStore, tracking: TrackingStore, account_id: UUID
) -> dict[str, int]:
    """The rows of the account per table, as the three ports report them."""
    return {
        **await store.account_rows(account_id),
        **await zones.account_rows(account_id),
        **await tracking.account_rows(account_id),
    }


# --- the schema ------------------------------------------------------------------------------


def test_the_three_stores_own_every_account_table_of_the_schema() -> None:
    """Each store deletes and counts its own tables. Together they must be every table with an
    ``account_id`` column: a new table that no store lists fails here, not in production."""
    owned = [
        table.name
        for module in (main_store, zone_store, tracking_store)
        for table in module._ACCOUNT_TABLES
    ]
    assert len(owned) == len(set(owned))  # no table has two owners
    assert set(owned) == ACCOUNT_TABLES


def test_only_the_thinning_watermark_has_no_account() -> None:
    """A table without ``account_id`` is invisible to the count: this fails when one is added,
    so its rows get an owner or a decision."""
    assert {table.name for table in metadata.sorted_tables} - ACCOUNT_TABLES == MOWER_TABLES


# --- the request -----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_erasure_removes_every_row_of_one_account_and_keeps_the_other(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone, kept = uuid4(), uuid4()
    async with (
        stores_of(kind, monkeypatch) as (store, zones, tracking),
        harness(store=store, zone_store=zones, tracking_store=tracking) as h,  # not a demo
    ):
        gone_mower = await fill(store, zones, tracking, gone)
        kept_mower = await fill(store, zones, tracking, kept)
        before = await rows_of(store, zones, tracking, gone)
        kept_rows = await rows_of(store, zones, tracking, kept)
        kept_view = await everything(store, zones, tracking, kept, kept_mower)
        # The seed reaches every table the schema gives an account, so none is checked empty.
        assert set(before) == ACCOUNT_TABLES

        first = await erase(h, gone)
        second = await erase(h, gone)
        unknown = await erase(h, uuid4())

        assert first.ok and first.error is None
        # Every row of the account and the thinning watermark of its mower.
        assert first.deleted == sum(before.values()) + 1
        assert second == AccountEraseReply(ok=True, deleted=0)
        assert unknown == AccountEraseReply(ok=True, deleted=0)
        assert await rows_of(store, zones, tracking, gone) == {}
        assert empty(await everything(store, zones, tracking, gone, gone_mower))
        assert await rows_of(store, zones, tracking, kept) == kept_rows
        assert await everything(store, zones, tracking, kept, kept_mower) == kept_view


@pytest.mark.parametrize(
    ("skipped", "tables"),
    [
        (MemoryStore, {"mower_directory", "mower_latest", "mower_positions"}),
        (MemoryZoneStore, {"virtual_zones", "zone_presence", "zone_days"}),
        (MemoryTrackingStore, {"tracking_sessions", "tracking_links", "tracking_photos"}),
    ],
)
async def test_erasure_reports_rows_that_remain_in_any_store_and_a_retry_finishes(
    skipped: type[Any], tables: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A purge that leaves rows behind, in whichever store, is never answered as a success."""

    async def purge_nothing(self: Any, account_id: UUID) -> int:
        del self, account_id
        return 0

    account_id = uuid4()
    store, zones, tracking = MemoryStore(), MemoryZoneStore(), MemoryTrackingStore()
    async with harness(store=store, zone_store=zones, tracking_store=tracking) as h:
        await fill(store, zones, tracking, account_id)
        with monkeypatch.context() as patched:
            patched.setattr(skipped, "purge_account", purge_nothing)
            failed = await erase(h, account_id)
        left = await rows_of(store, zones, tracking, account_id)
        retried = await erase(h, account_id)

        assert tables <= set(left)
        assert failed == AccountEraseReply(ok=False, deleted=0, error=str(ErasureIncomplete(left)))
        assert retried.ok and retried.deleted >= sum(left.values())
        assert await rows_of(store, zones, tracking, account_id) == {}


async def test_erasure_reports_a_failing_purge(monkeypatch: pytest.MonkeyPatch) -> None:
    async def purge_fails(self: Any, account_id: UUID) -> int:
        del self, account_id
        raise ConnectionError("database is down")

    account_id = uuid4()
    store, zones, tracking = MemoryStore(), MemoryZoneStore(), MemoryTrackingStore()
    async with harness(store=store, zone_store=zones, tracking_store=tracking) as h:
        await fill(store, zones, tracking, account_id)
        monkeypatch.setattr(MemoryStore, "purge_account", purge_fails)

        failed = await erase(h, account_id)

        assert failed == AccountEraseReply(
            ok=False, deleted=0, error="ConnectionError: see the log of telemetry"
        )
        assert await store.account_rows(account_id) != {}


# --- what a real account has and a demo sandbox does not --------------------------------------


async def test_erasure_empties_the_caches_that_cannot_be_searched_by_account() -> None:
    """Rendered maps, zone shapes and the map areas of tracking sessions are keyed by content,
    zone or session: the demo purge lets them age out, an erased account's garden must go."""
    async with harness() as h:
        services = h.app.state.telemetry
        services.widget._cache["etag"] = (NOW, object())
        services.zones._shapes[(uuid4(), NOW)] = object()
        services.tracking._areas[uuid4()] = (NOW, None)
        services.aerial._limiter._hits[uuid4()].append(1.0)
        account_id = uuid4()
        services.aerial._limiter._hits[account_id].append(1.0)

        reply = await erase(h, account_id)

        assert reply == AccountEraseReply(ok=True, deleted=0)
        assert not services.widget._cache
        assert not services.zones._shapes
        assert not services.tracking._areas
        assert account_id not in services.aerial._limiter._hits
        assert len(services.aerial._limiter._hits) == 1  # the other account's rate limit stays
