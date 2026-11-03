"""Demo history from daily templates (ADR 0079): one simulation per garden and day, copies.

The copies must be what the ingestion would have stored for the mower itself: the store test
runs on the memory store and, with ``MOWDECK_TEST_DATABASE_URL``, on PostgreSQL.
"""

import dataclasses
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import MowerState
from mowdeck_control.history import simulated_history
from mowdeck_service_kit.db import async_database_url
from sqlalchemy.ext.asyncio import create_async_engine
from test_telemetry_db import migrate, needs_db, temporary_database

from telemetry.adapters.db.store import SqlStore
from telemetry.adapters.memory import MemoryStore
from telemetry.application.context import TelemetryConfig
from telemetry.application.demo_history import (
    TEMPLATE_ACCOUNT,
    DemoHistory,
    HistoryMower,
    template_id,
)
from telemetry.application.ports import Store
from telemetry.application.services import build_services
from telemetry.domain.errors import AccountMismatchError
from telemetry.domain.models import DirectoryEntry
from telemetry.domain.thinning import ThinPolicy

NOW = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
FAMILY: dict[str, Any] = {"garden": "family", "origin_lat": 48.137154, "origin_lon": 11.576124}
ALL_TIME = (datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC))


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class CountingSimulation:
    """``simulated_history`` that counts its calls."""

    def __init__(self) -> None:
        self.calls: list[UUID] = []

    async def __call__(self, **arguments: Any) -> list[MowerState]:
        self.calls.append(arguments["mower_id"])
        return await simulated_history(**arguments)


def history_of(store: Store, clock: Clock, simulate: CountingSimulation) -> DemoHistory:
    services = build_services(store, TelemetryConfig(), clock)
    return DemoHistory(store, services.ingest, clock, simulate)


def without_ids(row: Any, *more: str) -> dict[str, Any]:
    """A row as a dict without its mower and account (and ``more`` fields)."""
    values = dataclasses.asdict(row)
    for name in ("mower_id", "account_id", *more):
        values.pop(name, None)
    return values


async def stored(store: Store, account_id: UUID, mower_id: UUID) -> dict[str, Any]:
    """Everything the ingestion stores of one mower, with its ids taken out."""
    start, end = ALL_TIME
    latest = await store.latest_of(account_id, mower_id)
    health = await store.health_of(account_id, mower_id)
    async with store.retention() as scope:
        assert scope is not None
        watermark = await scope.thin_start(mower_id)
    return {
        "history": [
            without_ids(h)
            for h in await store.history(account_id, mower_id, start, end, None, 100_000)
        ],
        "positions": [
            without_ids(p) for p in await store.positions(account_id, mower_id, start, end, 100_000)
        ],
        "daily": [
            without_ids(d)
            for d in await store.daily_of(account_id, mower_id, start.date(), end.date())
        ],
        "health": None if health is None else without_ids(health, "updated_at"),
        "latest": None
        if latest is None
        else (
            latest.observed_at,
            latest.activity,
            latest.history_fingerprint,
            {k: v for k, v in latest.state.items() if k not in ("mower_id", "account_id")},
            (latest.state["mower_id"], latest.state["account_id"]),
        ),
        "watermark": watermark,
    }


@asynccontextmanager
async def store_of(kind: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Store]:
    if kind == "memory":
        yield MemoryStore()
        return
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            yield SqlStore(engine)
        finally:
            await engine.dispose()


KINDS = [pytest.param("memory"), pytest.param("sql", marks=needs_db)]


@pytest.mark.parametrize("kind", KINDS)
async def test_a_copy_is_what_the_ingestion_stores_for_the_mower_itself(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with store_of(kind, monkeypatch) as store:
        clock, simulate = Clock(), CountingSimulation()
        demo = history_of(store, clock, simulate)
        account, mower = uuid4(), uuid4()
        result = await demo.fill([HistoryMower(mower, account, FAMILY)], 2)
        template = template_id(FAMILY, 2, NOW.date())
        # The direct way, for comparison: the same simulation ingested for a mower of its own.
        direct_account, direct = uuid4(), uuid4()
        services = build_services(store, TelemetryConfig(), clock)
        await services.ingest.record_states(
            [
                s.model_copy(update={"mower_id": direct, "account_id": direct_account})
                for s in await simulated_history(
                    mower_id=template, account_id=TEMPLATE_ACCOUNT, options=FAMILY, end=NOW, days=2
                )
            ]
        )
        other_account, other = uuid4(), uuid4()
        await demo.fill([HistoryMower(other, other_account, FAMILY)], 2)

        copy = await stored(store, account, mower)
        second = await stored(store, other_account, other)
        expected = await stored(store, direct_account, direct)

    assert simulate.calls == [template]  # one simulation for both mowers
    assert result.states > 100
    assert result.stored == result.states
    assert copy["history"] and copy["positions"] and copy["daily"] and copy["health"]
    assert copy["latest"][4] == (str(mower), str(account))
    assert second["latest"][4] == (str(other), str(other_account))
    for found in (copy, second):
        assert {k: v for k, v in found.items() if k != "latest"} == {
            k: v for k, v in expected.items() if k != "latest"
        }
        assert found["latest"][:4] == expected["latest"][:4]


@pytest.mark.parametrize("kind", KINDS)
async def test_a_copy_keeps_the_thinning_of_the_template(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with store_of(kind, monkeypatch) as store:
        clock = Clock()
        demo = history_of(store, clock, CountingSimulation())
        await demo.fill([HistoryMower(uuid4(), uuid4(), FAMILY)], 2)
        template = template_id(FAMILY, 2, NOW.date())
        async with store.retention() as scope:
            assert scope is not None
            await scope.thin_slice(template, NOW - timedelta(days=3), NOW, ThinPolicy())
        account, mower = uuid4(), uuid4()
        await demo.fill([HistoryMower(mower, account, FAMILY)], 2)
        copy = await stored(store, account, mower)
        original = await stored(store, TEMPLATE_ACCOUNT, template)
    assert original["watermark"] is not None
    assert copy["watermark"] == original["watermark"]
    assert copy["positions"] == original["positions"]


@pytest.mark.parametrize("kind", KINDS)
async def test_the_rows_of_other_accounts_are_untouched_and_the_copy_is_idempotent(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with store_of(kind, monkeypatch) as store:
        clock, simulate = Clock(), CountingSimulation()
        demo = history_of(store, clock, simulate)
        account, mower = uuid4(), uuid4()
        await demo.fill([HistoryMower(mower, account, FAMILY)], 1)
        before = await stored(store, account, mower)
        again = await demo.fill([HistoryMower(mower, account, FAMILY)], 1)
        after = await stored(store, account, mower)
        nothing = await stored(store, uuid4(), mower)  # another account sees nothing
    assert again.stored == 0  # the mower has a state: the ingestion would store nothing
    assert after == before
    assert not any(v for k, v in nothing.items() if k != "watermark")  # per mower, no account


async def test_a_new_day_makes_a_new_template_and_drops_the_old_one() -> None:
    store, clock, simulate = MemoryStore(), Clock(), CountingSimulation()
    demo = history_of(store, clock, simulate)
    await demo.fill([HistoryMower(uuid4(), uuid4(), FAMILY)], 1)
    await demo.fill([HistoryMower(uuid4(), uuid4(), {"garden": "orchard"})], 1)
    yesterday = template_id(FAMILY, 1, NOW.date())
    clock.now = NOW + timedelta(days=1)
    await demo.fill([HistoryMower(uuid4(), uuid4(), FAMILY)], 1)
    today = template_id(FAMILY, 1, clock.now.date())
    assert simulate.calls == [yesterday, template_id({"garden": "orchard"}, 1, NOW.date()), today]
    assert yesterday not in store.data.latest
    assert today in store.data.latest
    assert not any(m == yesterday for m, _ in store.data.history)


async def test_a_restart_makes_the_template_again_from_scratch() -> None:
    store, clock = MemoryStore(), Clock()
    first = history_of(store, clock, CountingSimulation())
    await first.fill([HistoryMower(uuid4(), uuid4(), FAMILY)], 1)
    template = template_id(FAMILY, 1, NOW.date())
    rows = sum(1 for m, _ in store.data.history if m == template)
    simulate = CountingSimulation()
    restarted = history_of(store, clock, simulate)  # knows no complete template
    account, mower = uuid4(), uuid4()
    await restarted.fill([HistoryMower(mower, account, FAMILY)], 1)
    assert simulate.calls == [template]
    assert sum(1 for m, _ in store.data.history if m == template) == rows
    assert sum(1 for m, _ in store.data.history if m == mower) == rows


async def test_a_mower_of_another_account_is_refused() -> None:
    store, clock = MemoryStore(), Clock()
    demo = history_of(store, clock, CountingSimulation())
    mower, owner = uuid4(), uuid4()
    async with store.transaction() as tx:
        await tx.put_directory(DirectoryEntry(mower, owner, NOW, "Front"), replace=True)
    with pytest.raises(AccountMismatchError):
        await demo.fill([HistoryMower(mower, uuid4(), FAMILY)], 1)
    assert mower not in store.data.latest
