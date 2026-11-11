# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""A deleted user (ADR 0082, "delete me"): on ``identity.user.deleted`` the Scheduler removes
the person's name from its schedules and runs and keeps them; a schedule whose owner is gone
stops by the rule that already holds for an owner without the right.

The event goes over the in-memory bus to the real apps (the Scheduler and Fleet), as Identity
publishes it. The store is the memory store and, with ``MOWDECK_TEST_DATABASE_URL``, PostgreSQL
(see ``test_scheduler_db``).
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from mowdeck_authz import account, principal_subject
from mowdeck_contracts.v1 import (
    IDENTITY_USER_DELETED,
    EventEnvelope,
    PrincipalKind,
    PrincipalRef,
    UserChanged,
)
from mowdeck_contracts.v1.subjects import event_subject_for_type
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.db import principal_ref_columns, user_tables
from mowdeck_service_kit.user_erasure import UserErasureIncomplete
from scheduler_support import API, harness
from test_scheduler_db import NOW, STORES, run_row, schedule_row, store_of

from scheduler.adapters.bus.user_erasure import USERS_CONSUMER
from scheduler.adapters.db.tables import metadata
from scheduler.adapters.memory import MemoryTx
from scheduler.application.ports import Store
from scheduler.domain.models import Schedule

FIRST = datetime(2026, 10, 9, 8, 30, tzinfo=UTC)  # Friday 10:30 Berlin
SUBJECT = event_subject_for_type(IDENTITY_USER_DELETED)


def ref(user_id: UUID, name: str | None) -> dict[str, Any]:
    return PrincipalRef(subject=user_id, kind=PrincipalKind.SUBUSER, display_name=name).model_dump(
        mode="json"
    )


async def add_schedule(store: Store, account_id: UUID, user_id: UUID, name: str) -> Schedule:
    """A schedule the user owns with one run issued for them, as the use cases store them."""
    item = schedule_row(
        account_id, f"Morning of {name}", owner=ref(user_id, name), owner_subject=user_id
    )
    run = run_row(item, NOW)
    run.issued_by = ref(user_id, name)
    async with store.transaction() as tx:
        await tx.add_schedule(item)
        await tx.add_run(run)
    return item


async def references(store: Store, item: Schedule) -> list[dict[str, Any]]:
    """The owner of the schedule and the subject of each of its runs."""
    async with store.transaction() as tx:
        found = await tx.get_schedule(item.id, item.account_id)
        runs = await tx.list_runs(item.id, item.account_id, limit=10, before=None)
    assert found is not None and found.owner_subject == item.owner_subject
    return [found.owner, *[run.issued_by or {} for run in runs]]


async def deleted(bus: InMemoryBus, user_id: UUID, account_id: UUID | None) -> None:
    """What Identity publishes when a user is deleted."""
    payload = UserChanged(user_id=user_id, account_id=account_id, username="anna", subuser=True)
    await bus.publish(
        SUBJECT,
        EventEnvelope[UserChanged](
            type=IDENTITY_USER_DELETED, producer="identity", account_id=account_id, payload=payload
        ),
    )


# --- the schema ------------------------------------------------------------------------------


def test_the_scheduler_stores_a_persons_name_only_in_these_references() -> None:
    """The erasure scrubs every JSON column that stores a principal reference, read from the
    schema. This fails when one is added or removed, so the README's list stays true."""
    found = sorted(f"{c.table.name}.{c.name}" for c in principal_ref_columns(metadata))
    assert found == ["schedule_runs.issued_by", "schedules.owner"]


def test_the_scheduler_keeps_no_row_that_is_a_users_own() -> None:
    """No table has a ``user_id`` column: nothing has to be deleted when a user goes."""
    assert user_tables(metadata) == ()


# --- the event -------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", STORES)
async def test_a_deleted_users_name_goes_and_the_schedules_stay(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:
        mine, other = uuid4(), uuid4()
        anna, ben, carl = uuid4(), uuid4(), uuid4()
        annas = await add_schedule(store, mine, anna, "Anna")
        bens = await add_schedule(store, mine, ben, "Ben")
        carls = await add_schedule(store, other, carl, "Carl")
        async with store.transaction() as tx:
            before = await tx.user_names(anna)
            rows_before = await tx.account_rows(mine)

        await deleted(h.bus, anna, mine)
        await deleted(h.bus, anna, mine)  # a redelivered event changes nothing

        async with store.transaction() as tx:
            after = await tx.user_names(anna)
            rows_after = await tx.account_rows(mine)
            again = await tx.forget_user(anna, mine)
        scrubbed = await references(store, annas)
        kept = [*await references(store, bens), *await references(store, carls)]
    assert sorted(before.values()) == [1, 1] and after == {}
    assert rows_after == rows_before  # nothing was deleted
    assert again == 0
    # The schedule still says which subject owns it, and nothing about who that is.
    assert len(scrubbed) == 2
    assert all(r["subject"] == str(anna) and r.get("display_name") is None for r in scrubbed)
    assert all(r["kind"] == "subuser" for r in scrubbed)
    assert sorted(r["display_name"] for r in kept) == ["Ben", "Ben", "Carl", "Carl"]
    assert h.bus.dropped == []


@pytest.mark.parametrize("kind", STORES)
async def test_the_account_of_the_event_bounds_the_scrub(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with store_of(kind, monkeypatch) as store:
        mine, other, anna = uuid4(), uuid4(), uuid4()
        await add_schedule(store, mine, anna, "Anna")
        await add_schedule(store, other, anna, "Anna")
        async with store.transaction() as tx:
            in_account = await tx.forget_user(anna, mine)
            left = await tx.user_names(anna)
            everywhere = await tx.forget_user(anna, None)
            none = await tx.user_names(anna)
    assert (in_account, everywhere) == (2, 2)
    assert sorted(left.values()) == [1, 1] and none == {}


async def test_names_that_remain_fail_the_event_and_a_redelivery_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scrub that leaves a name behind is never acknowledged as done."""
    async with harness() as h:
        mine, anna = uuid4(), uuid4()
        await add_schedule(h.store, mine, anna, "Anna")
        calls: list[int] = []
        real = MemoryTx.forget_user

        async def flaky(self: Any, user_id: UUID, account_id: UUID | None) -> int:
            calls.append(1)
            if len(calls) == 1:
                return 0  # the first attempt scrubs nothing
            return int(await real(self, user_id, account_id))

        monkeypatch.setattr(MemoryTx, "forget_user", flaky)
        with pytest.raises(UserErasureIncomplete) as raised:
            await h.services.user_erasure.erase(anna, mine)
        assert sorted(raised.value.leftovers.values()) == [1, 1]
        calls.clear()
        await deleted(h.bus, anna, mine)  # fails once, the bus delivers it again
        async with h.store.transaction() as tx:
            left = await tx.user_names(anna)
    assert len(calls) == 2
    assert left == {} and h.bus.dropped == []


async def test_a_schedule_of_a_deleted_owner_stops_and_somebody_else_takes_it_over() -> None:
    """End to end with Fleet: the deleted member holds no permission any more (Identity removed
    the tuples), so the schedule skips, is disabled with its reason, and nothing mows on behalf
    of somebody who is gone. Another member re-enables it and becomes its owner."""
    async with harness(scheduler_max_permission_skips=1) as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        manager = await h.member(owner, "manager")
        created = await h.schedule(manager, target_type="mower", target_id=str(mower_id))
        # What Identity does when the member deletes themselves: no tuples, then the event.
        subject = principal_subject(manager.principal)
        await h.revoke(manager)
        await h.identity.replace_subject_grants(subject, account(owner.account_id), [])
        await deleted(h.bus, manager.principal.subject, owner.account_id)
        h.clock.set(FIRST)
        await h.tick()
        runs = await h.runs(owner, created["id"])
        stopped = (
            await h.client.get(f"{API}/schedules/{created['id']}", headers=owner.headers)
        ).json()
        enabled = await h.client.patch(
            f"{API}/schedules/{created['id']}", headers=owner.headers, json={"enabled": True}
        )
        async with h.store.transaction() as tx:
            item = await tx.get_schedule(UUID(created["id"]), owner.account_id)
    assert [r["outcome"] for r in runs] == ["skipped_permission"]
    assert stopped["enabled"] is False and "lost can_control" in stopped["disabled_reason"]
    assert h.commands == []
    assert enabled.status_code == 200, enabled.text
    assert item is not None and item.owner_subject == owner.principal.subject


async def test_a_user_the_scheduler_never_saw_and_a_malformed_event_are_no_error() -> None:
    async with harness() as h:
        await deleted(h.bus, uuid4(), uuid4())
        await h.bus.publish(SUBJECT, b'{"type": "identity.user.deleted", "payload": {}}')
        scheduler_drops = [m for m in h.bus.dropped if m.subject == SUBJECT]
    # Dropped once per consumer (the Scheduler's and Fleet's in this harness), never retried.
    assert len(scheduler_drops) == 2


def test_the_consumer_is_durable_and_shared_by_the_replicas() -> None:
    assert USERS_CONSUMER == "scheduler-users"
