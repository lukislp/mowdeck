# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: Apache-2.0

"""Account erasure (ADR 0082): the use case, the handler of ``account.erase.<service>`` and the
schema helpers that count what is left of an account."""

import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import AccountEraseReply, AccountEraseRequest
from mowdeck_contracts.v1.subjects import account_erase_subject
from mowdeck_service_kit.bus import InMemoryBus, NoRespondersError
from mowdeck_service_kit.db import (
    account_row_counts,
    account_tables,
    async_database_url,
)
from mowdeck_service_kit.erasure import (
    ERROR_MAX_LENGTH,
    AccountErasure,
    ErasureIncomplete,
    erasure_handler,
)
from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, Uuid, insert
from sqlalchemy.ext.asyncio import create_async_engine

SUBJECT = account_erase_subject("fleet")


class FakeService:
    """A service's parts as the use case sees them: rows per table, a purge that deletes them
    (or what ``keeps`` names, or raises), and something to forget."""

    def __init__(self, rows: dict[str, int] | None = None) -> None:
        self.rows: dict[UUID, dict[str, int]] = {}
        self.seed = rows or {}
        self.keeps: set[str] = set()
        self.fails: Exception | None = None
        self.calls: list[str] = []

    def add(self, account_id: UUID) -> None:
        self.rows[account_id] = dict(self.seed)

    async def purge(self, account_id: UUID) -> int:
        self.calls.append("purge")
        if self.fails is not None:
            raise self.fails
        rows = self.rows.get(account_id, {})
        deleted = sum(count for table, count in rows.items() if table not in self.keeps)
        self.rows[account_id] = {t: c for t, c in rows.items() if t in self.keeps}
        return deleted

    async def leftovers(self, account_id: UUID) -> dict[str, int]:
        self.calls.append("leftovers")
        return dict(self.rows.get(account_id, {}))

    def forget(self, account_id: UUID) -> None:
        del account_id
        self.calls.append("forget")

    def erasure(self) -> AccountErasure:
        return AccountErasure(self.purge, self.leftovers, forget=[self.forget])


# --- the use case ----------------------------------------------------------------------------


async def test_erase_purges_forgets_and_then_checks_what_is_left() -> None:
    service = FakeService({"mowers": 2, "sites": 1})
    account_id = uuid4()
    service.add(account_id)

    assert await service.erasure().erase(account_id) == 3
    assert service.calls == ["purge", "forget", "leftovers"]


async def test_erase_is_idempotent() -> None:
    service = FakeService({"mowers": 2})
    account_id = uuid4()
    service.add(account_id)
    erasure = service.erasure()

    assert await erasure.erase(account_id) == 2
    assert await erasure.erase(account_id) == 0  # the repeated request
    assert await erasure.erase(uuid4()) == 0  # an account the service never knew


async def test_erase_refuses_success_while_rows_remain() -> None:
    service = FakeService({"commands": 3, "audit_log": 1, "mowers": 2})
    service.keeps = {"commands", "audit_log"}
    account_id = uuid4()
    service.add(account_id)

    with pytest.raises(ErasureIncomplete) as caught:
        await service.erasure().erase(account_id)

    assert caught.value.leftovers == {"commands": 3, "audit_log": 1}
    assert str(caught.value) == "rows of the account remain (audit_log: 1, commands: 3)"


async def test_a_table_reported_with_zero_rows_is_not_a_leftover() -> None:
    async def purge(account_id: UUID) -> int:
        del account_id
        return 1

    async def leftovers(account_id: UUID) -> dict[str, int]:
        del account_id
        return {"mowers": 0}

    assert await AccountErasure(purge, leftovers).erase(uuid4()) == 1


async def test_a_failing_purge_is_raised_and_nothing_is_reported_as_checked() -> None:
    service = FakeService({"mowers": 1})
    service.fails = RuntimeError("database is down")

    with pytest.raises(RuntimeError, match="database is down"):
        await service.erasure().erase(uuid4())
    assert service.calls == ["purge"]


# --- the handler -----------------------------------------------------------------------------


async def ask(bus: InMemoryBus, account_id: UUID) -> AccountEraseReply:
    """What Identity's ``BusAccountEraser`` does: request and decode the reply."""
    request = AccountEraseRequest(
        account_id=account_id, requested_at=datetime(2026, 11, 10, 12, tzinfo=UTC)
    )
    message = await bus.request(SUBJECT, request, timeout=1.0)
    return message.model(AccountEraseReply)


async def serving(service: FakeService) -> InMemoryBus:
    bus = InMemoryBus()
    await bus.subscribe(SUBJECT, erasure_handler("fleet", service.erasure().erase), queue="fleet")
    return bus


async def test_handler_answers_the_contract_and_a_repeat_with_zero() -> None:
    service = FakeService({"mowers": 2, "sites": 1})
    account_id = uuid4()
    service.add(account_id)
    bus = await serving(service)

    first = await ask(bus, account_id)
    second = await ask(bus, account_id)
    unknown = await ask(bus, uuid4())

    assert first == AccountEraseReply(ok=True, deleted=3, error=None)
    assert second == AccountEraseReply(ok=True, deleted=0, error=None)
    assert unknown == AccountEraseReply(ok=True, deleted=0, error=None)


async def test_handler_reports_remaining_rows_as_a_failure_naming_the_tables() -> None:
    service = FakeService({"commands": 3})
    service.keeps = {"commands"}
    account_id = uuid4()
    service.add(account_id)

    reply = await ask(await serving(service), account_id)

    assert reply.ok is False
    assert reply.deleted == 0
    assert reply.error == "rows of the account remain (commands: 3)"


async def test_handler_reports_a_failing_purge_without_its_message() -> None:
    service = FakeService()
    service.fails = RuntimeError("DELETE FROM linked_accounts WHERE email = 'ada@example.org'")

    reply = await ask(await serving(service), uuid4())

    assert reply.ok is False
    assert reply.error == "RuntimeError: see the log of fleet"


async def test_handler_answers_an_invalid_request_instead_of_letting_it_time_out() -> None:
    service = FakeService()
    bus = await serving(service)

    message = await bus.request(SUBJECT, b'{"account_id": "not-a-uuid"}', timeout=1.0)
    reply = message.model(AccountEraseReply)

    assert reply.ok is False
    assert reply.error is not None and reply.error.startswith("invalid request")
    assert service.calls == []


async def test_a_long_leftover_list_still_fits_the_contract() -> None:
    service = FakeService({f"table_with_a_long_name_{index:03d}": 1 for index in range(60)})
    service.keeps = set(service.seed)
    account_id = uuid4()
    service.add(account_id)

    reply = await ask(await serving(service), account_id)

    assert reply.ok is False
    assert reply.error is not None and len(reply.error) == ERROR_MAX_LENGTH


async def test_without_a_handler_identity_gets_no_responders() -> None:
    with pytest.raises(NoRespondersError):
        await ask(InMemoryBus(), uuid4())


# --- the schema helpers ----------------------------------------------------------------------


def schema(*, temporary: bool = False) -> MetaData:
    """Two account tables (one depends on the other) and one table of the installation."""
    metadata = MetaData()
    prefixes = ["TEMPORARY"] if temporary else []
    Table(
        "erasure_runs",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("schedule_id", Integer, ForeignKey("erasure_schedules.id"), nullable=False),
        Column("account_id", Uuid, nullable=False),
        prefixes=prefixes,
    )
    Table(
        "erasure_schedules",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("account_id", Uuid, nullable=False),
        prefixes=prefixes,
    )
    Table(
        "erasure_plans",
        metadata,
        Column("key", String(16), primary_key=True),
        prefixes=prefixes,
    )
    return metadata


def test_account_tables_are_read_from_the_schema() -> None:
    names = [table.name for table in account_tables(schema())]
    assert names == ["erasure_schedules", "erasure_runs"]  # dependency order, no plans


DATABASE_URL = os.environ.get("MOWDECK_TEST_DATABASE_URL")


@pytest.mark.skipif(not DATABASE_URL, reason="needs MOWDECK_TEST_DATABASE_URL")
async def test_account_row_counts_name_the_tables_that_still_hold_rows() -> None:
    metadata = schema(temporary=True)
    tables = account_tables(metadata)
    mine, other = uuid4(), uuid4()
    engine = create_async_engine(async_database_url(DATABASE_URL or ""))
    try:
        async with engine.connect() as conn:  # temporary tables: gone with the connection
            await conn.run_sync(metadata.create_all)
            await conn.execute(
                insert(metadata.tables["erasure_schedules"]),
                [{"id": 1, "account_id": mine}, {"id": 2, "account_id": other}],
            )
            await conn.execute(
                insert(metadata.tables["erasure_runs"]),
                [
                    {"id": 1, "schedule_id": 1, "account_id": mine},
                    {"id": 2, "schedule_id": 1, "account_id": mine},
                ],
            )
            assert await account_row_counts(conn, tables, mine) == {
                "erasure_schedules": 1,
                "erasure_runs": 2,
            }
            assert await account_row_counts(conn, tables, other) == {"erasure_schedules": 1}
            assert await account_row_counts(conn, tables, uuid4()) == {}
            await conn.rollback()
    finally:
        await engine.dispose()
