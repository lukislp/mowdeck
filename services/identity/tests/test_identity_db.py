"""PostgreSQL: migrations up and down, schema drift, the store contract, a full flow.

The PostgreSQL tests run when ``MOWDECK_TEST_DATABASE_URL`` points at a server where the user
may create databases (CI: ``postgresql://postgres:test@127.0.0.1:5433/postgres``). Each test
module run creates its own database and drops it afterwards. The store contract tests also run
against the memory store, so both implementations behave the same. Locally::

    docker run -d --name identity-test-pg -p 5433:5432 -e POSTGRES_PASSWORD=test \\
        public.ecr.aws/docker/library/postgres:17.11-alpine
    MOWDECK_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/postgres \\
        uv run pytest services/identity -k db
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
from identity_support import PASSWORD, harness
from mowdeck_service_kit.db import async_database_url
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from identity.adapters.db.migrate import alembic_config
from identity.adapters.db.store import SqlStore
from identity.adapters.db.tables import metadata
from identity.adapters.memory import MemoryStore
from identity.application.ports import Ge, Gt, In, Lt, Store
from identity.domain.errors import ConflictError
from identity.domain.models import (
    Account,
    AccountStatus,
    AuditEntry,
    KeyStatus,
    SigningKeyRecord,
    Status,
    User,
    UserKind,
)

ADMIN_URL = os.environ.get("MOWDECK_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not ADMIN_URL, reason="needs MOWDECK_TEST_DATABASE_URL")
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


@asynccontextmanager
async def temporary_database() -> AsyncIterator[str]:
    """Create a fresh database and yield its URL; drop it afterwards."""
    assert ADMIN_URL
    name = f"identity_test_{uuid4().hex[:10]}"
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
async def test_migrations_up_down_and_no_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            tables = await table_names(engine)
            assert set(metadata.tables) <= tables

            def diff(sync_conn: Any) -> list[Any]:
                context = MigrationContext.configure(sync_conn)
                return list(compare_metadata(context, metadata))

            async with engine.connect() as conn:
                drift = await conn.run_sync(diff)
            assert drift == []
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO audit_log (at, action, outcome, details) "
                        "VALUES (now(), 'x.y', 'success', '{}')"
                    )
                )
            for statement in ("UPDATE audit_log SET action = 'z.z'", "DELETE FROM audit_log"):
                with pytest.raises(DBAPIError, match="append-only"):
                    async with engine.begin() as conn:
                        await conn.execute(text(statement))
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


STORES = [
    pytest.param("memory"),
    pytest.param("postgres", marks=needs_db),
]


def an_account(**overrides: Any) -> Account:
    values: dict[str, Any] = {
        "id": uuid4(),
        "name": "Home",
        "slug": f"home-{uuid4().hex[:8]}",
        "kind": "household",
        "status": AccountStatus.ACTIVE,
        "plan": "unlimited",
        "created_at": NOW,
        **overrides,
    }
    return Account(**values)


def a_user(name: str, **overrides: Any) -> User:
    values: dict[str, Any] = {
        "id": uuid4(),
        "username": name,
        "username_norm": name,
        "display_name": name.title(),
        "kind": UserKind.PERSON,
        "status": Status.ACTIVE,
        "created_at": NOW,
        **overrides,
    }
    return User(**values)


@pytest.mark.parametrize("kind", STORES)
async def test_store_contract(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async with store_of(kind, monkeypatch) as store:
        acct = an_account()
        async with store.transaction() as tx:
            await tx.accounts.add(acct)
            await tx.users.add(a_user("anna", owner_account_id=acct.id, kind=UserKind.SUBUSER))
            await tx.users.add(a_user("bob", email="bob@x.org", email_norm="bob@x.org"))
            with pytest.raises(ConflictError):
                await tx.users.add(a_user("anna"))
            with pytest.raises(ConflictError):
                await tx.users.add(a_user("carl", email_norm="bob@x.org"))
            await tx.users.add(a_user("dora"))  # the transaction survives a conflict
        async with store.transaction() as tx:
            anna = await tx.users.get(username_norm="anna")
            assert anna is not None and anna.kind is UserKind.SUBUSER
            assert anna.owner_account_id == acct.id
            assert await tx.users.count() == 3
            assert await tx.users.count(email_norm=None) == 2
            assert [
                u.username
                for u in await tx.users.find(order_by="username", descending=True, limit=2)
            ] == [
                "dora",
                "bob",
            ]
            assert {u.username for u in await tx.users.find(username_norm=In(["anna", "bob"]))} == {
                "anna",
                "bob",
            }
            anna.status = Status.DISABLED
            anna.last_login_at = NOW
            await tx.users.update(anna)
        async with store.transaction() as tx:
            reloaded = await tx.users.get(id=anna.id)
            assert reloaded is not None and reloaded.status is Status.DISABLED
            assert reloaded.last_login_at == NOW
            with pytest.raises(LookupError):
                await tx.users.get(kind=UserKind.PERSON.value)
            with pytest.raises(LookupError):
                await tx.users.update(a_user("ghost"))
            assert await tx.users.delete(username_norm="dora") == 1
        with pytest.raises(RuntimeError):
            async with store.transaction() as tx:
                await tx.users.add(a_user("erik"))
                raise RuntimeError("roll back")
        async with store.transaction() as tx:
            assert await tx.users.get(username_norm="erik") is None
            for minutes in (1, 2, 3):
                await tx.audit.add(
                    AuditEntry(
                        at=NOW + timedelta(minutes=minutes),
                        action="x.y",
                        actor_subject=None,
                        actor_kind=None,
                        account_id=acct.id,
                        details={"n": minutes, "list": [1, 2]},
                    )
                )
            entries = await tx.audit.find(order_by="id", account_id=acct.id)
            assert [e.details["n"] for e in entries] == [1, 2, 3]
            first_id = entries[0].id
            assert first_id is not None
            assert len(await tx.audit.find(id=Gt(first_id))) == 2
            assert len(await tx.audit.find(id=Lt(first_id + 1))) == 1
            assert len(await tx.audit.find(at=Ge(NOW + timedelta(minutes=2)))) == 2
            record = SigningKeyRecord(
                "kid1", "EdDSA", {"kty": "OKP"}, b"\x00\x01", KeyStatus.ACTIVE, NOW
            )
            await tx.signing_keys.add(record)
        async with store.transaction() as tx:
            key = await tx.signing_keys.get(kid="kid1")
            assert key is not None and key.private_key_enc == b"\x00\x01"
            assert key.status is KeyStatus.ACTIVE and key.public_jwk == {"kty": "OKP"}


@needs_db
async def test_full_flow_on_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    async with store_of("postgres", monkeypatch) as store, harness(store=store) as h:
        lukas = await h.register("lukas")
        operator = await h.role_id(lukas, "operator")
        anna_id, anna_token = await h.subuser(
            lukas,
            "anna",
            [{"role_id": str(operator), "level": {"type": "account", "id": str(lukas.account_id)}}],
        )
        refreshed = await h.client.post(
            "/api/v1/identity/session/refresh", json={"refresh_token": lukas.refresh_token}
        )
        disabled = await h.client.post(
            lukas.url(f"/users/{anna_id}/disable"), headers=lukas.headers
        )
        login = await h.client.post(
            "/api/v1/identity/session", json={"username": "anna", "password": PASSWORD}
        )
        removed = await h.client.delete(lukas.url(f"/users/{anna_id}"), headers=lukas.headers)
        level = {"type": "account", "id": str(lukas.account_id)}
        token_ids = []
        for name in ("old", "older", "kept"):
            made = await h.client.post(
                lukas.url("/api-tokens"),
                headers=lukas.headers,
                json={"name": name, "grants": [{"level": level, "permissions": ["mower.view"]}]},
            )
            token_ids.append(made.json()["id"])
        for token_id in token_ids[:2]:
            await h.client.delete(lukas.url(f"/api-tokens/{token_id}"), headers=lukas.headers)
        purge_one = await h.client.delete(
            lukas.url(f"/api-tokens/{token_ids[0]}"),
            headers=lukas.headers,
            params={"purge": "true"},
        )
        purge_active = await h.client.delete(
            lukas.url(f"/api-tokens/{token_ids[2]}"),
            headers=lukas.headers,
            params={"purge": "true"},
        )
        purge_rest = await h.client.post(
            lukas.url("/api-tokens/purge-revoked"), headers=lukas.headers
        )
        tokens_left = await h.client.get(lukas.url("/api-tokens"), headers=lukas.headers)
        audit = await h.audit(lukas)
        deleted = await h.client.request(
            "DELETE", lukas.url(), headers=lukas.headers, json={"password": PASSWORD}
        )
    assert anna_token
    assert refreshed.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert login.status_code == 401
    assert removed.status_code == 204
    assert purge_one.status_code == 204
    assert purge_active.status_code == 409
    assert purge_rest.json() == {"deleted": 1, "ids": [token_ids[1]]}
    assert [t["name"] for t in tokens_left.json()] == ["kept"]
    assert {
        "account.registered",
        "user.created",
        "role.assigned",
        "user.disabled",
        "user.deleted",
        "api_token.revoked",
        "api_token.purged",
    } <= {e["action"] for e in audit}
    assert deleted.status_code == 202
