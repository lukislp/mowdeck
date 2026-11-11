# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""A deleted user (ADR 0082, "delete me"): on ``identity.user.deleted`` Fleet removes the
person's name from its command and drive session records and keeps everything else.

The event goes over the in-memory bus to the real app, as Identity publishes it. The store is
the memory store and, with ``MOWDECK_TEST_DATABASE_URL``, PostgreSQL (see ``test_fleet_db``).
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fleet_support import Harness, harness
from mowdeck_contracts.v1 import (
    IDENTITY_USER_DELETED,
    EventEnvelope,
    PrincipalKind,
    PrincipalRef,
    UserChanged,
)
from mowdeck_contracts.v1.subjects import event_subject_for_type
from mowdeck_service_kit.db import principal_ref_columns, user_tables
from mowdeck_service_kit.user_erasure import UserErasureIncomplete
from test_fleet_db import STORES, store_of

from fleet.adapters.bus.user_erasure import USERS_CONSUMER
from fleet.adapters.db.tables import metadata
from fleet.adapters.memory import MemoryTx
from fleet.application.ports import Store
from fleet.domain.models import CommandRecord, DriveSession

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
SUBJECT = event_subject_for_type(IDENTITY_USER_DELETED)


def ref(user_id: UUID, name: str | None) -> dict[str, Any]:
    return PrincipalRef(subject=user_id, kind=PrincipalKind.SUBUSER, display_name=name).model_dump(
        mode="json"
    )


async def add_records(store: Store, account_id: UUID, user_id: UUID, name: str) -> None:
    """A command and a drive session of the user, as the use cases store them."""
    mower_id = uuid4()
    async with store.transaction() as tx:
        await tx.commands.add(
            CommandRecord(
                command_id=uuid4(),
                account_id=account_id,
                mower_id=mower_id,
                kind="pause_mow",
                command={"kind": "pause_mow"},
                priority="normal",
                issued_by_subject=user_id,
                issued_by=ref(user_id, name),
                issued_at=NOW,
                expires_at=NOW + timedelta(seconds=30),
                status="accepted",
                correlation_id="c-1",
            )
        )
        await tx.drive_sessions.add(
            DriveSession(
                uuid4(),
                account_id,
                mower_id,
                user_id,
                ref(user_id, name),
                NOW,
                NOW + timedelta(minutes=5),
            )
        )


async def records(store: Store, account_id: UUID) -> list[tuple[UUID, dict[str, Any]]]:
    """``(subject, reference)`` of every command and drive session of the account."""
    async with store.transaction() as tx:
        commands = await tx.commands.find(account_id=account_id)
        sessions = await tx.drive_sessions.find(account_id=account_id)
    found = [(c.issued_by_subject, c.issued_by) for c in commands]
    found += [(s.started_by_subject, s.started_by) for s in sessions]
    return found


async def deleted(h: Harness, user_id: UUID, account_id: UUID | None) -> None:
    """What Identity publishes when a user is deleted."""
    payload = UserChanged(user_id=user_id, account_id=account_id, username="anna", subuser=True)
    await h.bus.publish(
        SUBJECT,
        EventEnvelope[UserChanged](
            type=IDENTITY_USER_DELETED, producer="identity", account_id=account_id, payload=payload
        ),
    )


# --- the schema ------------------------------------------------------------------------------


def test_fleet_stores_a_persons_name_only_in_these_references() -> None:
    """The erasure scrubs every JSON column that stores a principal reference, read from the
    schema. This fails when one is added or removed, so the README's list stays true."""
    found = sorted(f"{c.table.name}.{c.name}" for c in principal_ref_columns(metadata))
    assert found == ["commands.issued_by", "drive_sessions.started_by"]


def test_fleet_keeps_no_row_that_is_a_users_own() -> None:
    """No table has a ``user_id`` column: nothing has to be deleted when a user goes. A table
    that gets one needs a decision (delete it in ``forget_user``), and this fails until then."""
    assert user_tables(metadata) == ()


# --- the event -------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", STORES)
async def test_a_deleted_users_name_goes_and_the_records_stay(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:
        alice, bob = await h.owner(), await h.owner()
        anna, ben, carl = uuid4(), uuid4(), uuid4()
        await add_records(store, alice.account_id, anna, "Anna")
        await add_records(store, alice.account_id, ben, "Ben")
        await add_records(store, bob.account_id, carl, "Carl")
        async with store.transaction() as tx:
            before = await tx.user_names(anna)
            rows_before = await tx.account_rows(alice.account_id)

        await deleted(h, anna, alice.account_id)
        await deleted(h, anna, alice.account_id)  # a redelivered event changes nothing

        async with store.transaction() as tx:
            after = await tx.user_names(anna)
            rows_after = await tx.account_rows(alice.account_id)
            again = await tx.forget_user(anna, alice.account_id)
        alices = await records(store, alice.account_id)
        bobs = await records(store, bob.account_id)
    assert sorted(before.values()) == [1, 1] and after == {}
    assert rows_after == rows_before  # nothing was deleted
    assert again == 0
    assert {subject for subject, _ in alices} == {anna, ben}
    # The record still says which subject it was, and nothing about who that is.
    assert sorted(
        (subject == anna, reference.get("display_name")) for subject, reference in alices
    ) == [(False, "Ben"), (False, "Ben"), (True, None), (True, None)]
    assert all(
        reference["subject"] == str(subject) and reference["kind"] == "subuser"
        for subject, reference in alices
    )
    assert all(reference["display_name"] == "Carl" for _, reference in bobs)
    assert h.bus.dropped == []


@pytest.mark.parametrize("kind", STORES)
async def test_the_account_of_the_event_bounds_the_scrub(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The event names the account the user was deleted in; a record that carries the same
    subject id in another account is not that account's to change. Without an account in the
    event the user is forgotten wherever they appear."""
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:
        alice, bob = await h.owner(), await h.owner()
        anna = uuid4()
        await add_records(store, alice.account_id, anna, "Anna")
        await add_records(store, bob.account_id, anna, "Anna")
        async with store.transaction() as tx:
            in_account = await tx.forget_user(anna, alice.account_id)
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
        alice = await h.owner()
        anna = uuid4()
        await add_records(h.store, alice.account_id, anna, "Anna")
        calls: list[int] = []
        real = MemoryTx.forget_user

        async def flaky(self: Any, user_id: UUID, account_id: UUID | None) -> int:
            calls.append(1)
            if len(calls) == 1:
                return 0  # the first attempt scrubs nothing
            return int(await real(self, user_id, account_id))

        monkeypatch.setattr(MemoryTx, "forget_user", flaky)
        with pytest.raises(UserErasureIncomplete) as raised:
            await h.app.state.fleet.user_erasure.erase(anna, alice.account_id)
        assert sorted(raised.value.leftovers.values()) == [1, 1]
        calls.clear()
        await deleted(h, anna, alice.account_id)  # fails once, the bus delivers it again
        async with h.store.transaction() as tx:
            left = await tx.user_names(anna)
    assert len(calls) == 2
    assert left == {} and h.bus.dropped == []


async def test_a_user_fleet_never_saw_and_a_malformed_event_are_no_error() -> None:
    async with harness() as h:
        await deleted(h, uuid4(), uuid4())
        await h.bus.publish(SUBJECT, b'{"type": "identity.user.deleted", "payload": {}}')
        dropped = [m.subject for m in h.bus.dropped]
    assert dropped == [SUBJECT]  # the malformed one, not redelivered for ever


def test_the_consumer_is_durable_and_shared_by_the_replicas() -> None:
    assert USERS_CONSUMER == "fleet-users"
