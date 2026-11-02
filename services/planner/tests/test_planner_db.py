"""PostgreSQL: migrations up and down, schema drift and the store contract.

The PostgreSQL tests run when ``MOWDECK_TEST_DATABASE_URL`` points at a server where the user
may create databases (CI: ``postgresql://postgres:test@127.0.0.1:5433/postgres``). Each test
creates its own database and drops it afterwards. The store contract also runs against the
memory store, so both implementations behave the same. Locally::

    docker run -d --name planner-test-pg -p 5433:5432 -e POSTGRES_PASSWORD=test \\
        postgres:17.11-alpine
    MOWDECK_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/postgres \\
        uv run pytest services/planner -k db
"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from mowdeck_service_kit.db import async_database_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from planner.adapters.db.migrate import alembic_config
from planner.adapters.db.store import SqlStore
from planner.adapters.db.tables import metadata
from planner.adapters.memory import MemoryStore
from planner.application.ports import Store
from planner.domain.models import Template

ADMIN_URL = os.environ.get("MOWDECK_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not ADMIN_URL, reason="needs MOWDECK_TEST_DATABASE_URL")
NOW = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)


@asynccontextmanager
async def temporary_database() -> AsyncIterator[str]:
    """Create a fresh database and yield its URL; drop it afterwards."""
    assert ADMIN_URL
    name = f"planner_test_{uuid4().hex[:10]}"
    admin = create_async_engine(async_database_url(ADMIN_URL), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    base, _, _ = ADMIN_URL.rpartition("/")
    try:
        yield f"{base}/{name}"
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def migrate(
    url: str, monkeypatch: pytest.MonkeyPatch, revision: str = "head", *, down: bool = False
) -> None:
    monkeypatch.setenv("MOWDECK_DATABASE_URL", url)
    if down:
        await asyncio.to_thread(command.downgrade, alembic_config(), revision)
    else:
        await asyncio.to_thread(command.upgrade, alembic_config(), revision)


async def table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        return {row[0] for row in rows}


@needs_db
async def test_planner_db_migrations_up_down_and_no_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            assert set(metadata.tables) <= await table_names(engine)

            def diff(sync_conn: Any) -> list[Any]:
                context = MigrationContext.configure(sync_conn)
                return list(compare_metadata(context, metadata))

            async with engine.connect() as conn:
                drift = await conn.run_sync(diff)
            assert drift == []
            await migrate(url, monkeypatch, "base", down=True)
            assert await table_names(engine) == {"alembic_version"}
            await migrate(url, monkeypatch)
        finally:
            await engine.dispose()


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


STORES = [pytest.param("memory"), pytest.param("postgres", marks=needs_db)]


@pytest.mark.parametrize("kind", STORES)
async def test_planner_db_facts_never_go_back_in_time(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    mower_id, account_id = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        async with store.transaction() as tx:
            assert await tx.get_facts(mower_id) is None
            assert await tx.put_map(mower_id, account_id, NOW, {"areas": [1]})
            assert await tx.put_capabilities(mower_id, account_id, NOW, {"custom_paths": True})
            assert not await tx.put_map(mower_id, account_id, NOW - timedelta(1), {"areas": []})
            assert await tx.put_map(mower_id, account_id, NOW + timedelta(1), {"areas": [2]})
        async with store.transaction() as tx:
            facts = await tx.get_facts(mower_id)
    assert facts is not None
    assert facts.map == {"areas": [2]}
    assert facts.capabilities == {"custom_paths": True}
    assert facts.map_observed_at == NOW + timedelta(1)
    assert facts.account_id == account_id


def _template(account_id: Any, name: str, **values: Any) -> Template:
    base: dict[str, Any] = {
        "id": uuid4(),
        "account_id": account_id,
        "name": name,
        "chain": [{"pattern": "stripes", "settings": {}}],
        "created_by": uuid4(),
        "created_at": NOW,
        "updated_at": NOW,
        "description": None,
        **values,
    }
    return Template(**base)


@pytest.mark.parametrize("kind", STORES)
async def test_planner_db_templates_contract(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    account_id, other = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        async with store.transaction() as tx:
            await tx.put_template(_template(account_id, "b"))
            await tx.put_template(_template(account_id, "a"))
            await tx.put_template(_template(other, "a"))
            first = await tx.get_template(account_id, "a")
            assert first is not None
            await tx.put_template(
                _template(
                    account_id,
                    "a",
                    id=first.id,
                    description="new",
                    chain=[{"pattern": "spiral", "settings": {}}],
                    updated_at=NOW + timedelta(1),
                )
            )
        async with store.transaction() as tx:
            names = [t.name for t in await tx.list_templates(account_id)]
            count = await tx.count_templates(account_id)
            updated = await tx.get_template(account_id, "a")
            removed = await tx.delete_template(account_id, "b")
            again = await tx.delete_template(account_id, "b")
            missing = await tx.get_template(account_id, "b")
    assert names == ["a", "b"]
    assert count == 2
    assert updated is not None and updated.description == "new"
    assert updated.chain == [{"pattern": "spiral", "settings": {}}]
    assert updated.updated_at == NOW + timedelta(1)
    assert removed and not again and missing is None


async def test_planner_memory_store_rolls_back_on_error() -> None:
    store = MemoryStore()
    with pytest.raises(RuntimeError):
        async with store.transaction() as tx:
            await tx.put_template(_template(uuid4(), "x"))
            raise RuntimeError("boom")
    assert store.data.templates == {}


@pytest.mark.parametrize("kind", STORES)
async def test_planner_db_purge_account(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR 0076: ``purge_account`` deletes the account's templates and mower facts and nothing
    of another account; a second purge finds nothing."""
    account_id, other = uuid4(), uuid4()
    mowers = {account_id: uuid4(), other: uuid4()}
    async with store_of(kind, monkeypatch) as store:
        async with store.transaction() as tx:
            for owner, mower_id in mowers.items():
                await tx.put_template(_template(owner, "a"))
                await tx.put_template(_template(owner, "b"))
                await tx.put_map(mower_id, owner, NOW, {"areas": [1]})
                await tx.put_capabilities(mower_id, owner, NOW, {"custom_paths": True})
        async with store.transaction() as tx:
            assert await tx.purge_account(account_id) == 3
            assert await tx.purge_account(account_id) == 0
        async with store.transaction() as tx:
            assert await tx.count_templates(account_id) == 0
            assert await tx.get_facts(mowers[account_id]) is None
            assert await tx.count_templates(other) == 2
            assert await tx.get_facts(mowers[other]) is not None
