# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure (ADR 0082): ``account.erase.notifications`` removes everything Notifications
holds of a deleted account, outside the demo, and reports honestly when it could not.

The requests go over the in-memory bus to the real app, as Identity sends them. The store is
the memory store and, with ``MOWDECK_TEST_DATABASE_URL``, PostgreSQL (see
``test_notifications_db``).
"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import AccountEraseReply, AccountEraseRequest
from mowdeck_contracts.v1.subjects import account_erase_subject
from mowdeck_service_kit.db import account_tables
from mowdeck_service_kit.erasure import ErasureIncomplete
from notifications_support import T0, Harness, harness
from test_notifications_db import FENCE, KINDS, item, message, store_of
from test_notifications_demo_purge import fill, holdings

from notifications.adapters.db.store import SqlStore
from notifications.adapters.db.tables import metadata
from notifications.adapters.memory import ACCOUNT_DATA, MemoryStore
from notifications.application.ports import AccountPurge, CapturedPhoto, Store
from notifications.domain.models import Delivery, DeliveryOutcome, DeliveryResult, NewDelivery

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)

INSTALLATION_TABLES = {"maintenance_window"}
"""The tables without an ``account_id`` column: the maintenance window is one row of the
installation (deploys), it names no account, user or mower."""


async def erase(h: Harness, account_id: UUID) -> AccountEraseReply:
    """What Identity's ``BusAccountEraser`` sends, and the decoded reply."""
    request = AccountEraseRequest(account_id=account_id, requested_at=NOW)
    message_ = await h.bus.request(account_erase_subject("notifications"), request, timeout=5.0)
    return message_.model(AccountEraseReply)


def account_table_names(store: Store) -> set[str]:
    """Every table that can hold rows of an account: read from the schema for PostgreSQL."""
    if isinstance(store, SqlStore):
        return {table.name for table in account_tables(metadata)}
    return set(ACCOUNT_DATA)


class BlockingChannel:
    """A channel whose sends hang until released; it records what started, finished and was
    cancelled."""

    name = "log"

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started: list[UUID] = []
        self.sent: list[UUID] = []
        self.cancelled: list[UUID] = []

    async def send(self, delivery: Delivery) -> DeliveryResult:
        self.started.append(delivery.account_id)
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.append(delivery.account_id)
            raise
        self.sent.append(delivery.account_id)
        return DeliveryResult(DeliveryOutcome.SENT)


class BlockingCamera:
    """A photo source that answers only when released."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.asked: list[UUID] = []

    async def capture(self, mower_id: UUID, account_id: UUID) -> CapturedPhoto | None:
        del mower_id
        self.asked.append(account_id)
        await self.release.wait()
        return CapturedPhoto(data=b"\xff\xd8jpeg", media_type="image/jpeg")


async def settle() -> None:
    """Let the started tasks reach their first wait."""
    for _ in range(5):
        await asyncio.sleep(0)


# --- the schema ------------------------------------------------------------------------------


def test_every_table_of_notifications_belongs_to_an_account_or_the_installation() -> None:
    """The erasure counts the tables with an ``account_id`` column. A table without one would
    be invisible to it: this fails when one is added, so its rows get an owner or a decision."""
    without = {table.name for table in metadata.sorted_tables} - {
        table.name for table in account_tables(metadata)
    }
    assert without == INSTALLATION_TABLES


def test_no_table_of_notifications_is_keyed_by_the_user_alone() -> None:
    """Every table with a ``user_id`` also names the account, so nothing of a person can be
    left behind by a purge that goes by account."""
    by_user = {table.name for table in metadata.sorted_tables if "user_id" in table.c}
    assert by_user and by_user <= {table.name for table in account_tables(metadata)}


def test_the_memory_store_counts_every_table_but_the_installations() -> None:
    assert set(ACCOUNT_DATA) == set(vars(MemoryStore().data)) - {"maintenance"}


# --- the request -----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_erasure_removes_every_row_of_one_account_and_keeps_the_other(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone, kept = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:  # not a demo
        gone_rows = await fill(store, gone)
        kept_rows = await fill(store, kept)
        before = await store.account_rows(gone)
        kept_counts = await store.account_rows(kept)
        kept_holdings = await holdings(store, kept, *kept_rows)
        # The seed reaches every table the schema gives an account, so none is checked empty.
        assert set(before) == account_table_names(store)

        first = await erase(h, gone)
        second = await erase(h, gone)
        unknown = await erase(h, uuid4())

        assert first == AccountEraseReply(ok=True, deleted=sum(before.values()))
        assert second == AccountEraseReply(ok=True, deleted=0)
        assert unknown == AccountEraseReply(ok=True, deleted=0)
        assert await store.account_rows(gone) == {}
        found = await holdings(store, gone, *gone_rows)
        assert all(not value for value in found.values()), found
        assert await store.account_rows(kept) == kept_counts
        assert await holdings(store, kept, *kept_rows) == kept_holdings


async def test_erasure_reports_rows_that_remain_and_a_retry_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A purge that leaves rows behind is never answered as a success."""

    async def purge_nothing(self: MemoryStore, account_id: UUID) -> AccountPurge:
        del self, account_id
        return AccountPurge(deleted=0, mower_ids=frozenset())

    account_id = uuid4()
    async with harness() as h:
        await fill(h.store, account_id)
        left = await h.store.account_rows(account_id)
        with monkeypatch.context() as patched:
            patched.setattr(MemoryStore, "purge_account", purge_nothing)
            failed = await erase(h, account_id)
        assert await h.store.account_rows(account_id) == left
        retried = await erase(h, account_id)

        assert failed == AccountEraseReply(ok=False, deleted=0, error=str(ErasureIncomplete(left)))
        assert "push: 1" in (failed.error or "") and "apns: 1" in (failed.error or "")
        assert retried == AccountEraseReply(ok=True, deleted=sum(left.values()))
        assert await h.store.account_rows(account_id) == {}


async def test_erasure_reports_a_failing_purge(monkeypatch: pytest.MonkeyPatch) -> None:
    async def purge_fails(self: MemoryStore, account_id: UUID) -> AccountPurge:
        del self, account_id
        raise ConnectionError("database is down")

    account_id = uuid4()
    async with harness() as h:
        await fill(h.store, account_id)
        monkeypatch.setattr(MemoryStore, "purge_account", purge_fails)

        failed = await erase(h, account_id)

        assert failed == AccountEraseReply(
            ok=False, deleted=0, error="ConnectionError: see the log of notifications"
        )
        assert await h.store.account_rows(account_id) != {}


# --- what a real account has and a demo sandbox does not --------------------------------------


async def test_nothing_queued_is_sent_to_an_erased_account() -> None:
    """Pending deliveries go with the rows; the worker finds nothing of the account to send."""
    gone, kept = uuid4(), uuid4()
    async with harness() as h:
        for account_id in (gone, kept):
            user = uuid4()
            delivery = NewDelivery(user, account_id, "log", message())
            assert await h.store.record(item(user, account_id, "x"), [delivery], 200)

        reply = await erase(h, gone)
        claimed = await h.services.outbox.run_once()

        assert reply == AccountEraseReply(ok=True, deleted=2)  # the inbox item and its delivery
        assert claimed == 1
        assert [d.account_id for d in h.channel.sent] == [kept]


async def test_erasure_cancels_the_sends_the_outbox_already_claimed() -> None:
    """A delivery claimed before the purge is in memory only: its send is cancelled, the other
    account's goes on."""
    gone, kept = uuid4(), uuid4()
    channel = BlockingChannel()
    async with harness(channels={"log": channel}) as h:
        for account_id in (gone, kept):
            user = uuid4()
            delivery = NewDelivery(user, account_id, "log", message())
            assert await h.store.record(item(user, account_id, "x"), [delivery], 200)
        assert await h.services.outbox.dispatch() == 2
        await settle()
        assert sorted(channel.started, key=str) == sorted([gone, kept], key=str)

        reply = await erase(h, gone)
        channel.release.set()
        await h.services.outbox.drain()

        assert reply.ok
        assert channel.cancelled == [gone]
        assert channel.sent == [kept]
        assert await h.store.account_rows(gone) == {}
        assert h.services.outbox._sending == {}


async def test_erasure_cancels_the_photos_still_being_taken() -> None:
    """A photo that arrived after the purge would be a row of the erased account again."""
    gone, kept = uuid4(), uuid4()
    camera = BlockingCamera()
    async with harness() as h:
        fanout = h.services.fanout
        fanout._photos = camera
        for account_id in (gone, kept):
            fanout._take_photo(uuid4(), uuid4(), account_id)
        await settle()
        assert sorted(camera.asked, key=str) == sorted([gone, kept], key=str)

        reply = await erase(h, gone)
        camera.release.set()
        await fanout.drain()

        assert reply == AccountEraseReply(ok=True, deleted=0)
        assert [image.account_id for image in h.store.data.images.values()] == [kept]
        assert await h.store.account_rows(gone) == {}


async def test_a_removed_mower_is_forgotten_in_memory_before_the_account_is_erased() -> None:
    """Fleet is asked first and removes the mowers, so the purge of Notifications finds no row
    that names them: what the use cases keep per mower goes with ``mower.removed``."""
    async with harness() as h:
        alice, bob = await h.owner(), await h.owner()
        mowers = {owner.account_id: await h.add_mower(owner) for owner in (alice, bob)}
        services = h.services
        live = services.live
        assert live is not None
        for mower_id in mowers.values():
            services.fanout._photo_log[mower_id] = [T0]
            services.watcher._recorded[(mower_id, False)] = T0
            services.watcher._fences[mower_id] = (T0, FENCE)
            live.remember_areas(mower_id, {"1": "Front lawn"})
            live._last[mower_id] = "mowing"
            live._active.add(mower_id)
            live._checked.add(mower_id)
            live._reloaded[(mower_id, uuid4())] = T0
        gone, kept = mowers[alice.account_id], mowers[bob.account_id]

        await h.announce(gone, alice.account_id, removed=True)
        reply = await erase(h, alice.account_id)

        assert reply == AccountEraseReply(ok=True, deleted=0)  # the directory row went before
        assert set(services.fanout._photo_log) == {kept}
        assert set(services.watcher._recorded) == {(kept, False)}
        assert set(services.watcher._fences) == {kept}
        assert set(live._areas) == {kept}
        assert set(live._last) == {kept}
        assert live._active == {kept} and live._checked == {kept}
        assert {key[0] for key in live._reloaded} == {kept}


async def test_erasure_forgets_the_mowers_its_rows_still_named() -> None:
    """A mower whose ``mower.removed`` has not arrived yet is forgotten by the purge itself."""
    async with harness() as h:
        alice = await h.owner()
        mower_id = await h.add_mower(alice)
        h.services.watcher._fences[mower_id] = (T0, FENCE)
        assert h.services.live is not None
        h.services.live.remember_areas(mower_id, {"1": "Front lawn"})

        reply = await erase(h, alice.account_id)

        assert reply == AccountEraseReply(ok=True, deleted=1)  # the directory row
        assert h.services.watcher._fences == {}
        assert h.services.live._areas == {}
