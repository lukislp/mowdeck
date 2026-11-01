"""One pooled connection for all reads of a request (docs/ops/load-testing.md).

A read of the stores normally checks a connection out of the pool, pings it, runs one query and
returns it. A mower page asks several such reads per request (the zone view six), and under load
every checkout waits in the pool's queue again, so the request's latency grows with the number
of its queries. Inside :func:`shared_reads` the reads of the same asyncio task share one
connection instead: it is checked out on the first read (not before, so a request that is
refused by its authorization check never holds one) and returned when the scope ends.

Writes (``engine.begin()``), streamed reads and advisory locks keep their own connections. Other
tasks (``asyncio.gather``, background tasks spawned by the request) never use the shared
connection, since one asyncpg connection runs one statement at a time.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


@dataclass(slots=True)
class _Scope:
    owner: asyncio.Task[object] | None
    engine: AsyncEngine | None = None
    conn: AsyncConnection | None = None


_scope: ContextVar[_Scope | None] = ContextVar("telemetry_shared_reads", default=None)


@asynccontextmanager
async def shared_reads() -> AsyncIterator[None]:
    """Let the reads of the current task share one connection until the scope ends."""
    scope = _Scope(owner=asyncio.current_task())
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)
        if scope.conn is not None:
            await scope.conn.close()


@asynccontextmanager
async def read_connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """The scope's shared connection for ``engine``, else a pooled connection of its own."""
    scope = _scope.get()
    if (
        scope is None
        or scope.owner is not asyncio.current_task()
        or (scope.engine is not None and scope.engine is not engine)
    ):
        async with engine.connect() as conn:
            yield conn
        return
    if scope.conn is None:
        scope.conn = await engine.connect()
        scope.engine = engine
    try:
        yield scope.conn
    except Exception:
        # A failed statement aborts the transaction; the next read starts a fresh one.
        await scope.conn.rollback()
        raise
