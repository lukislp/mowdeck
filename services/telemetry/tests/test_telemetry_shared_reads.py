"""One pooled connection per request for the reads (docs/ops/load-testing.md)."""

import asyncio
from collections.abc import Generator
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from telemetry.adapters.db.reads import read_connection, shared_reads


class FakeConnection:
    def __init__(self, engine: "FakeEngine") -> None:
        self.engine = engine
        self.closed = False
        self.rollbacks = 0

    def __await__(self) -> Generator[Any, None, "FakeConnection"]:
        if False:  # pragma: no cover - makes this a generator
            yield
        return self

    async def __aenter__(self) -> "FakeConnection":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        self.closed = True

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeEngine:
    def __init__(self) -> None:
        self.connections: list[FakeConnection] = []

    def connect(self) -> FakeConnection:
        conn = FakeConnection(self)
        self.connections.append(conn)
        return conn


def _engine() -> tuple[FakeEngine, AsyncEngine]:
    fake = FakeEngine()
    return fake, cast(AsyncEngine, fake)


async def test_reads_outside_a_scope_use_a_connection_each() -> None:
    fake, engine = _engine()
    async with read_connection(engine) as first:
        pass
    async with read_connection(engine) as second:
        pass
    assert first is not second
    assert all(c.closed for c in fake.connections)


async def test_reads_in_a_scope_share_one_connection_checked_out_lazily() -> None:
    fake, engine = _engine()
    async with shared_reads():
        assert fake.connections == []  # nothing checked out before the first read
        async with read_connection(engine) as first:
            pass
        async with read_connection(engine) as second:
            pass
        assert first is second
        assert not first.closed  # held until the scope ends
    assert len(fake.connections) == 1
    assert fake.connections[0].closed


async def test_a_scope_without_reads_checks_out_nothing() -> None:
    fake, _ = _engine()
    async with shared_reads():
        pass
    assert fake.connections == []


async def test_other_tasks_never_use_the_shared_connection() -> None:
    """asyncpg runs one statement per connection: gathered or spawned tasks get their own."""
    fake, engine = _engine()
    async with shared_reads():
        async with read_connection(engine) as mine:
            pass

        async def other() -> FakeConnection:
            async with read_connection(engine) as conn:
                return cast(FakeConnection, conn)

        theirs = await asyncio.gather(other(), other())
    assert all(conn is not cast(FakeConnection, mine) for conn in theirs)
    assert len(fake.connections) == 3


async def test_another_engine_gets_its_own_connection() -> None:
    _, first_engine = _engine()
    second_fake, second_engine = _engine()
    async with shared_reads():
        async with read_connection(first_engine) as first:
            pass
        async with read_connection(second_engine) as second:
            pass
    assert first is not second
    assert second_fake.connections[0].closed


async def test_the_route_dependency_shares_and_returns_the_connection() -> None:
    import httpx
    from fastapi import FastAPI

    from telemetry.adapters.api.deps import SharedReads

    fake, engine = _engine()
    app = FastAPI()
    seen: list[FakeConnection] = []

    @app.get("/page", dependencies=[SharedReads])
    async def page() -> dict[str, int]:
        for _ in range(3):
            async with read_connection(engine) as conn:
                seen.append(cast(FakeConnection, conn))
        return {"reads": len(seen)}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/page")
    assert response.json() == {"reads": 3}
    assert len(fake.connections) == 1
    assert seen[0] is seen[1] is seen[2]
    assert fake.connections[0].closed


async def test_a_failed_read_rolls_back_the_shared_connection() -> None:
    fake, engine = _engine()
    async with shared_reads():
        with pytest.raises(RuntimeError):
            async with read_connection(engine):
                raise RuntimeError("statement failed")
        async with read_connection(engine) as again:
            pass
    assert cast(FakeConnection, again).rollbacks == 1
    assert len(fake.connections) == 1
