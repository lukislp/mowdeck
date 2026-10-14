"""PostgreSQL: migrations up and down, schema drift, and the store contract.

The PostgreSQL tests run when ``MOWDECK_TEST_DATABASE_URL`` points at a server where the user
may create databases (CI: ``postgresql://postgres:test@127.0.0.1:5433/postgres``). Each test
creates its own database and drops it afterwards. The store contract also runs against the
memory store, so both implementations behave the same. Locally::

    docker run -d --name entitlements-test-pg -p 5433:5432 -e POSTGRES_PASSWORD=test \\
        postgres:17.11-alpine
    MOWDECK_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/postgres \\
        uv run pytest services/entitlements -k db
"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from entitlements_support import T0, harness, repository_plans
from mowdeck_contracts.v1 import PlanLimits, PlanSource, PlanStatus
from mowdeck_service_kit.db import async_database_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from entitlements.adapters.db.migrate import alembic_config
from entitlements.adapters.db.store import SqlStore
from entitlements.adapters.db.tables import metadata
from entitlements.adapters.memory import MemoryStore
from entitlements.application.ports import Store
from entitlements.domain.models import AccountPlan, FeatureEntry, Plan, PlanHistory

ADMIN_URL = os.environ.get("MOWDECK_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not ADMIN_URL, reason="needs MOWDECK_TEST_DATABASE_URL")


@asynccontextmanager
async def temporary_database() -> AsyncIterator[str]:
    """Create a fresh database and yield its URL; drop it afterwards."""
    assert ADMIN_URL
    name = f"entitlements_test_{uuid4().hex[:10]}"
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
async def test_entitlements_db_migrations_up_down_and_no_drift(
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
            assert set(metadata.tables) <= await table_names(engine)
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


def _plan(key: str, **values: Any) -> Plan:
    base: dict[str, Any] = {
        "key": key,
        "name": key.title(),
        "limits": PlanLimits(max_mowers=4),
        "features": ["view", "control"],
        "created_at": T0,
        "updated_at": T0,
    }
    return Plan(**{**base, **values})


@pytest.mark.parametrize("kind", STORES)
async def test_entitlements_store_contract(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async with store_of(kind, monkeypatch) as store:
        async with store.transaction() as tx:
            await tx.save_plan(_plan("home", is_default=True, file_digest="a" * 64))
            await tx.save_plan(_plan("fleet", limits=PlanLimits(), trial_days=14))
            await tx.save_plan(_plan("fleet", name="Fleet 2", limits=PlanLimits(), is_default=True))
            await tx.clear_default("fleet")
        async with store.transaction() as tx:
            plans = {p.key: p for p in await tx.list_plans()}
            assert sorted(plans) == ["fleet", "home"]
            assert plans["fleet"].name == "Fleet 2" and plans["fleet"].is_default
            assert plans["home"].is_default is False
            assert plans["home"].limits.max_mowers == 4
            assert plans["home"].file_digest == "a" * 64
            assert plans["fleet"].limits.max_mowers is None
            assert await tx.get_plan("gold") is None
        account_ids = [uuid4(), uuid4()]
        async with store.transaction() as tx:
            for index, account_id in enumerate(account_ids):
                row = AccountPlan(
                    account_id=account_id,
                    plan_key="home",
                    status=PlanStatus.TRIAL,
                    since=T0 + timedelta(minutes=index),
                    source=PlanSource.DEFAULT,
                    updated_at=T0,
                    account_name=f"A{index}",
                    trial_until=T0 + timedelta(days=30),
                )
                assert await tx.add_account_if_absent(row) is True
                assert await tx.add_account_if_absent(row) is False
            first = await tx.get_account(account_ids[0])
            assert first is not None
            first.plan_key, first.status, first.source = (
                "fleet",
                PlanStatus.GRACE,
                PlanSource.MANUAL,
            )
            first.grace_until = T0 + timedelta(days=14)
            await tx.save_account(first)
        async with store.transaction() as tx:
            assert await tx.count_accounts() == 2
            page = await tx.list_accounts(0, 1)
            assert [r.account_id for r in page] == [account_ids[0]]
            assert [r.account_id for r in await tx.list_accounts(1, 5)] == [account_ids[1]]
            again = await tx.get_account(account_ids[0])
            assert again is not None
            assert (again.plan_key, again.status, again.source) == (
                "fleet",
                PlanStatus.GRACE,
                PlanSource.MANUAL,
            )
            assert again.grace_until == T0 + timedelta(days=14)
            assert await tx.get_account(uuid4()) is None
            for offset in range(3):
                await tx.add_history(
                    PlanHistory(
                        id=uuid4(),
                        account_id=account_ids[0],
                        at=T0 + timedelta(hours=offset),
                        plan_key="home",
                        status=PlanStatus.ACTIVE,
                        source=PlanSource.MANUAL,
                        reason=f"r{offset}",
                        actor="user:x",
                    )
                )
        async with store.transaction() as tx:
            history = await tx.history(account_ids[0], 2)
            assert [h.reason for h in history] == ["r2", "r1"]
            assert await tx.history(account_ids[1], 5) == []
            await tx.save_feature(FeatureEntry("manual_drive", "Drive", "realtime", T0, T0))
            await tx.save_feature(
                FeatureEntry("manual_drive", "Driving", "realtime", T0, T0 + timedelta(1), True)
            )
        async with store.transaction() as tx:
            [feature] = await tx.list_features()
            assert (feature.description, feature.retired) == ("Driving", True)
            assert feature.first_seen_at == T0


@pytest.mark.parametrize("kind", STORES)
async def test_entitlements_store_rolls_back_on_error(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with store_of(kind, monkeypatch) as store:
        with pytest.raises(RuntimeError):
            async with store.transaction() as tx:
                await tx.save_plan(_plan("home"))
                raise RuntimeError("boom")
        async with store.transaction() as tx:
            assert await tx.list_plans() == []


@needs_db
async def test_entitlements_db_app_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    async with (
        store_of("postgres", monkeypatch) as store,
        harness(store=store, entitlements_billing_enabled=True) as h,
    ):
        owner, operator = await h.owner(), await h.operator()
        await h.account_created(owner.account_id, name="Garden")
        assert (await h.me(owner))["status"] == "trial"
        response = await h.assign(operator, owner.account_id, plan="fleet", reason="pg")
        assert response.status_code == 200, response.text
        assert response.json()["effective"]["plan"] == "fleet"
        assert sorted(p.key for p in await h.services.entitlements.plans()) == [
            "fleet",
            "home",
            "unlimited",
        ]
        assert await h.services.entitlements.seed(repository_plans()) == []
