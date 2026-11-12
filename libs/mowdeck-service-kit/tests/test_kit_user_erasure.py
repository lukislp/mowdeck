# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: Apache-2.0

"""A deleted user (ADR 0082): the use case, the handler of ``identity.user.deleted`` and the
schema helpers that find, scrub and count the stored references to a person."""

import os
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import (
    IDENTITY_USER_DELETED,
    IDENTITY_USER_DISABLED,
    EventEnvelope,
    PrincipalKind,
    PrincipalRef,
    UserChanged,
)
from mowdeck_contracts.v1.subjects import event_subject_for_type
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.db import (
    async_database_url,
    delete_user_rows,
    is_principal_ref_column,
    named_principal_refs,
    principal_ref_columns,
    scrub_principal_refs,
    user_row_counts,
    user_tables,
)
from mowdeck_service_kit.user_erasure import (
    REF_NAME,
    REF_SUBJECT,
    UserErasure,
    UserErasureIncomplete,
    anonymous_ref,
    named_refs,
    names_user,
    scrub_refs,
    user_deleted_handler,
)
from sqlalchemy import Column, Integer, MetaData, String, Table, Uuid, insert, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine

SUBJECT = event_subject_for_type(IDENTITY_USER_DELETED)


class FakeService:
    """A service's parts as the use case sees them: named references per user, a ``forget``
    that scrubs them (or what ``keeps`` names, or raises), and something held in memory."""

    def __init__(self) -> None:
        self.refs: dict[UUID, dict[str, int]] = {}
        self.keeps: set[str] = set()
        self.fails: Exception | None = None
        self.calls: list[tuple[str, UUID, UUID | None]] = []
        self.cached: set[UUID] = set()

    async def forget(self, user_id: UUID, account_id: UUID | None) -> int:
        self.calls.append(("forget", user_id, account_id))
        if self.fails is not None:
            raise self.fails
        refs = self.refs.get(user_id, {})
        changed = sum(count for place, count in refs.items() if place not in self.keeps)
        self.refs[user_id] = {p: c for p, c in refs.items() if p in self.keeps}
        return changed

    async def leftovers(self, user_id: UUID) -> dict[str, int]:
        self.calls.append(("leftovers", user_id, None))
        return dict(self.refs.get(user_id, {}))

    def drop(self, user_id: UUID) -> None:
        self.cached.discard(user_id)

    def use_case(self) -> UserErasure:
        return UserErasure(self.forget, self.leftovers, [self.drop])


def event(
    user_id: UUID, account_id: UUID | None, event_type: str = IDENTITY_USER_DELETED
) -> EventEnvelope[UserChanged]:
    payload = UserChanged(user_id=user_id, account_id=account_id, username="anna", subuser=True)
    return EventEnvelope[UserChanged](
        type=event_type, producer="identity", account_id=account_id, payload=payload
    )


# --- the use case ----------------------------------------------------------------------------


async def test_erase_forgets_drops_from_memory_and_checks_what_is_left() -> None:
    service, anna, account = FakeService(), uuid4(), uuid4()
    service.refs[anna] = {"commands.issued_by": 3, "schedules.owner": 1}
    service.cached.add(anna)

    changed = await service.use_case().erase(anna, account)
    again = await service.use_case().erase(anna, account)

    assert (changed, again) == (4, 0)
    assert [name for name, _, _ in service.calls] == ["forget", "leftovers"] * 2
    assert service.calls[0] == ("forget", anna, account)
    assert anna not in service.cached


async def test_a_user_the_service_never_knew_is_no_error() -> None:
    assert await FakeService().use_case().erase(uuid4()) == 0


async def test_leftovers_fail_loudly_and_name_only_places_and_counts() -> None:
    service, anna = FakeService(), uuid4()
    service.refs[anna] = {"commands.issued_by": 3, "schedules.owner": 1}
    service.keeps = {"schedules.owner"}

    with pytest.raises(UserErasureIncomplete) as raised:
        await service.use_case().erase(anna)

    assert raised.value.leftovers == {"schedules.owner": 1}
    assert str(raised.value) == "personal references to the user remain (schedules.owner: 1)"


async def test_a_failing_forget_propagates_and_nothing_is_counted() -> None:
    service = FakeService()
    service.fails = RuntimeError("database down")
    with pytest.raises(RuntimeError, match="database down"):
        await service.use_case().erase(uuid4())
    assert [name for name, _, _ in service.calls] == ["forget"]


# --- one stored reference ----------------------------------------------------------------------


def test_a_reference_names_a_user_only_with_their_id_and_a_name() -> None:
    anna, other = uuid4(), uuid4()
    ref = PrincipalRef(subject=anna, kind=PrincipalKind.USER, display_name="Anna").model_dump(
        mode="json"
    )
    assert names_user(ref, anna)
    assert not names_user(ref, other)
    assert not names_user(None, anna)
    assert not names_user({**ref, REF_NAME: None}, anna)
    assert not names_user(anonymous_ref(ref), anna)


def test_an_anonymous_reference_keeps_everything_but_the_name() -> None:
    anna, token = uuid4(), uuid4()
    ref = PrincipalRef(
        subject=anna, kind=PrincipalKind.SUBUSER, display_name="Anna", token_id=token
    ).model_dump(mode="json")
    scrubbed = anonymous_ref(ref)
    assert scrubbed == {REF_SUBJECT: str(anna), "kind": "subuser", "token_id": str(token)}
    assert ref[REF_NAME] == "Anna"  # the input is not changed
    assert PrincipalRef.model_validate(scrubbed).display_name is None  # still a valid reference


@dataclass(slots=True)
class Row:
    """A row as a memory store keeps it."""

    account_id: UUID
    issued_by: dict[str, Any]
    approved_by: dict[str, Any] | None = None
    created_by: UUID | None = None
    command: dict[str, Any] | None = None


def test_rows_in_memory_are_scrubbed_like_the_columns() -> None:
    anna, ben, mine, other = uuid4(), uuid4(), uuid4(), uuid4()

    def ref(user_id: UUID, name: str | None = "A name") -> dict[str, Any]:
        return PrincipalRef(subject=user_id, kind=PrincipalKind.USER, display_name=name).model_dump(
            mode="json"
        )

    rows = [
        Row(mine, ref(anna), ref(ben), created_by=anna, command={"display_name": "no principal"}),
        Row(mine, ref(ben), ref(anna)),
        Row(other, ref(anna)),
        Row(mine, ref(anna, None)),
    ]
    before = named_refs(rows, anna)
    in_account = scrub_refs(rows, anna, mine)
    left = named_refs(rows, anna)
    everywhere = scrub_refs(rows, anna)

    assert before == {"issued_by": 2, "approved_by": 1}
    assert (in_account, left) == (2, {"issued_by": 1})
    assert (everywhere, scrub_refs(rows, anna), named_refs(rows, anna)) == (1, 0, {})
    assert rows[0].issued_by == {"subject": str(anna), "kind": "user", "token_id": None}
    assert rows[0].approved_by == ref(ben) and rows[0].created_by == anna
    assert rows[0].command == {"display_name": "no principal"}
    assert named_refs(rows, ben) == {"issued_by": 1, "approved_by": 1}


@dataclass
class ModelRow:
    """A domain row that keeps the contract type (Vision's proposals), not its JSON."""

    account_id: UUID
    requested_by: PrincipalRef
    decided_by: PrincipalRef | None = None


def test_rows_that_keep_a_principal_ref_model_are_scrubbed_in_their_type() -> None:
    anna, ben, mine = uuid4(), uuid4(), uuid4()

    def ref(user_id: UUID, name: str | None) -> PrincipalRef:
        return PrincipalRef(subject=user_id, kind=PrincipalKind.USER, display_name=name)

    rows = [ModelRow(mine, ref(anna, "Anna"), ref(ben, "Ben")), ModelRow(mine, ref(anna, None))]
    before = named_refs(rows, anna)
    changed = scrub_refs(rows, anna, mine)

    assert before == {"requested_by": 1}
    assert (changed, scrub_refs(rows, anna), named_refs(rows, anna)) == (1, 0, {})
    assert rows[0].requested_by == ref(anna, None)
    assert isinstance(rows[0].requested_by, PrincipalRef)
    assert rows[0].decided_by == ref(ben, "Ben")


# --- the handler -------------------------------------------------------------------------------


async def subscribed(service: FakeService) -> InMemoryBus:
    bus = InMemoryBus()
    await bus.subscribe(
        SUBJECT,
        user_deleted_handler("fleet", service.use_case().erase),
        durable="fleet-users",
        queue="fleet-users",
    )
    return bus


async def test_the_handler_forgets_the_user_of_the_event() -> None:
    service, anna, account = FakeService(), uuid4(), uuid4()
    service.refs[anna] = {"commands.issued_by": 2}
    bus = await subscribed(service)

    await bus.publish(SUBJECT, event(anna, account))

    assert service.refs[anna] == {}
    assert service.calls[0] == ("forget", anna, account)


async def test_the_handler_ignores_other_identity_events() -> None:
    service, anna = FakeService(), uuid4()
    handler = user_deleted_handler("fleet", service.use_case().erase)
    bus = InMemoryBus()
    await bus.subscribe("event.identity.>", handler, durable="fleet-users", queue="fleet-users")

    await bus.publish(
        event_subject_for_type(IDENTITY_USER_DISABLED),
        event(anna, uuid4(), IDENTITY_USER_DISABLED),
    )

    assert service.calls == []


async def test_a_failure_is_redelivered_until_it_works() -> None:
    """A raising handler gets the event again (the in-memory bus retries like JetStream)."""
    service, anna = FakeService(), uuid4()
    service.refs[anna] = {"commands.issued_by": 1}
    attempts: list[int] = []
    erase = service.use_case().erase

    async def flaky(user_id: UUID, account_id: UUID | None) -> int:
        attempts.append(1)
        if len(attempts) == 1:
            raise UserErasureIncomplete({"commands.issued_by": 1})
        return await erase(user_id, account_id)

    bus = InMemoryBus()
    await bus.subscribe(
        SUBJECT, user_deleted_handler("fleet", flaky), durable="fleet-users", queue="fleet-users"
    )
    await bus.publish(SUBJECT, event(anna, None))

    assert len(attempts) == 2
    assert service.refs[anna] == {}


async def test_a_malformed_event_is_dropped_not_redelivered() -> None:
    service = FakeService()
    bus = await subscribed(service)
    await bus.publish(SUBJECT, b'{"type": "identity.user.deleted", "payload": {"user_id": 1}}')
    assert service.calls == []


# --- the schema helpers ----------------------------------------------------------------------


def schema(*, temporary: bool = False) -> MetaData:
    """A table with two references and plain ids, a user's own table, and a table without."""
    metadata = MetaData()
    prefixes = ["TEMPORARY"] if temporary else []
    Table(
        "ue_commands",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("account_id", Uuid, nullable=False),
        Column("issued_by", JSONB, nullable=False),
        Column("approved_by", JSONB),
        Column("created_by", Uuid),  # an opaque id: not a reference with a name
        Column("command", JSONB, nullable=False),  # JSON, but no principal
        prefixes=prefixes,
    )
    Table(
        "ue_schedules",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("owner", JSONB, nullable=False),  # no account column
        Column("owner_subject", Uuid, nullable=False),
        prefixes=prefixes,
    )
    Table(
        "ue_preferences",
        metadata,
        Column("user_id", Uuid, primary_key=True),
        Column("account_id", Uuid, primary_key=True),
        Column("language", String(8)),
        prefixes=prefixes,
    )
    return metadata


def test_reference_columns_and_user_tables_are_read_from_the_schema() -> None:
    metadata = schema()
    found = sorted(f"{c.table.name}.{c.name}" for c in principal_ref_columns(metadata))
    assert found == ["ue_commands.approved_by", "ue_commands.issued_by", "ue_schedules.owner"]
    assert [table.name for table in user_tables(metadata)] == ["ue_preferences"]
    commands = metadata.tables["ue_commands"]
    assert not is_principal_ref_column(commands.c.created_by)
    assert not is_principal_ref_column(commands.c.command)


DATABASE_URL = os.environ.get("MOWDECK_TEST_DATABASE_URL")


def _ref(user_id: UUID, name: str | None) -> dict[str, object]:
    return PrincipalRef(subject=user_id, kind=PrincipalKind.USER, display_name=name).model_dump(
        mode="json"
    )


@pytest.mark.skipif(not DATABASE_URL, reason="needs MOWDECK_TEST_DATABASE_URL")
async def test_the_scrub_removes_one_users_name_and_keeps_the_rows() -> None:
    metadata = schema(temporary=True)
    columns = principal_ref_columns(metadata)
    commands, schedules = metadata.tables["ue_commands"], metadata.tables["ue_schedules"]
    anna, ben, mine, other = uuid4(), uuid4(), uuid4(), uuid4()
    engine = create_async_engine(async_database_url(DATABASE_URL or ""))
    try:
        async with engine.connect() as conn:  # temporary tables: gone with the connection
            await conn.run_sync(metadata.create_all)
            await conn.execute(
                insert(commands),
                [
                    {
                        "id": 1,
                        "account_id": mine,
                        "issued_by": _ref(anna, "Anna"),
                        "approved_by": _ref(ben, "Ben"),
                        "command": {"display_name": "not a principal"},
                    },
                    {
                        "id": 2,
                        "account_id": mine,
                        "issued_by": _ref(ben, "Ben"),
                        "approved_by": _ref(anna, "Anna"),
                        "command": {},
                    },
                    {
                        "id": 3,
                        "account_id": other,
                        "issued_by": _ref(anna, "Anna"),
                        "approved_by": None,
                        "command": {},
                    },
                    {
                        "id": 4,
                        "account_id": mine,
                        "issued_by": _ref(anna, None),  # already without a name
                        "approved_by": None,
                        "command": {},
                    },
                ],
            )
            await conn.execute(
                insert(schedules), [{"id": 1, "owner": _ref(anna, "Anna"), "owner_subject": anna}]
            )
            before = await named_principal_refs(conn, columns, anna)
            in_account = await scrub_principal_refs(conn, columns, anna, mine)
            left = await named_principal_refs(conn, columns, anna)
            everywhere = await scrub_principal_refs(conn, columns, anna)
            again = await scrub_principal_refs(conn, columns, anna)
            after = await named_principal_refs(conn, columns, anna)
            rows = (await conn.execute(select(commands).order_by(commands.c.id))).all()
            owner = (await conn.execute(select(schedules.c.owner))).scalar_one()
            bens = await named_principal_refs(conn, columns, ben)
            await conn.rollback()
    finally:
        await engine.dispose()
    assert before == {
        "ue_commands.issued_by": 2,
        "ue_commands.approved_by": 1,
        "ue_schedules.owner": 1,
    }
    # With the account: its rows, and the table without an account column.
    assert in_account == 3 and left == {"ue_commands.issued_by": 1}
    assert (everywhere, again, after) == (1, 0, {})
    assert len(rows) == 4
    assert rows[0].issued_by == {"subject": str(anna), "kind": "user", "token_id": None}
    assert rows[0].approved_by["display_name"] == "Ben"
    assert rows[0].command == {"display_name": "not a principal"}
    assert rows[1].issued_by["display_name"] == "Ben"
    assert "display_name" not in rows[1].approved_by and rows[1].approved_by["subject"] == str(anna)
    assert "display_name" not in rows[2].issued_by
    assert owner["subject"] == str(anna) and "display_name" not in owner
    assert bens == {"ue_commands.approved_by": 1, "ue_commands.issued_by": 1}


@pytest.mark.skipif(not DATABASE_URL, reason="needs MOWDECK_TEST_DATABASE_URL")
async def test_a_users_own_rows_are_deleted_and_counted_from_the_schema() -> None:
    metadata = schema(temporary=True)
    tables = user_tables(metadata)
    preferences = metadata.tables["ue_preferences"]
    anna, ben, mine, other = uuid4(), uuid4(), uuid4(), uuid4()
    engine = create_async_engine(async_database_url(DATABASE_URL or ""))
    try:
        async with engine.connect() as conn:
            await conn.run_sync(metadata.create_all)
            await conn.execute(
                insert(preferences),
                [
                    {"user_id": anna, "account_id": mine, "language": "de"},
                    {"user_id": anna, "account_id": other, "language": "en"},
                    {"user_id": ben, "account_id": mine, "language": "en"},
                ],
            )
            before = await user_row_counts(conn, tables, anna)
            in_account = await delete_user_rows(conn, tables, anna, mine)
            left = await user_row_counts(conn, tables, anna)
            everywhere = await delete_user_rows(conn, tables, anna)
            again = await delete_user_rows(conn, tables, anna)
            after = await user_row_counts(conn, tables, anna)
            bens = await user_row_counts(conn, tables, ben)
            await conn.rollback()
    finally:
        await engine.dispose()
    assert before == {"ue_preferences": 2}
    assert (in_account, left) == (1, {"ue_preferences": 1})
    assert (everywhere, again, after) == (1, 0, {})
    assert bens == {"ue_preferences": 1}
