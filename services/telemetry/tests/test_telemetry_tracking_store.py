"""Tracking store contract on the memory store and on PostgreSQL (migration included)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from mowdeck_service_kit.db import async_database_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_telemetry_db import migrate, needs_db, temporary_database

from telemetry.adapters.db.tracking_store import SqlTrackingStore
from telemetry.adapters.memory_tracking import MemoryTrackingStore
from telemetry.application.tracking_ports import TrackingStore
from telemetry.domain.tracking import TrackingLink, TrackingPhoto, TrackingSession

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def session(account_id=None, mower_id=None, **changes) -> TrackingSession:  # type: ignore[no-untyped-def]
    values = {
        "id": uuid4(),
        "account_id": account_id or uuid4(),
        "mower_id": mower_id or uuid4(),
        "trigger": "manual",
        "started_at": NOW,
        "ends_at": NOW + timedelta(hours=24),
        "interval_seconds": 10.0,
        "mower_name": "Front",
        **changes,
    }
    return TrackingSession(**values)


def link_of(s: TrackingSession, token_hash: str, **changes) -> TrackingLink:  # type: ignore[no-untyped-def]
    values = {
        "id": uuid4(),
        "session_id": s.id,
        "account_id": s.account_id,
        "mower_id": s.mower_id,
        "token_hash": token_hash,
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=48),
        **changes,
    }
    return TrackingLink(**values)


@asynccontextmanager
async def store_of(kind: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TrackingStore]:
    if kind == "memory":
        yield MemoryTrackingStore()
        return
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            yield SqlTrackingStore(engine)
        finally:
            await engine.dispose()


KINDS = [pytest.param("memory"), pytest.param("sql", marks=needs_db)]


@pytest.mark.parametrize("kind", KINDS)
async def test_tracking_store_sessions_links_photos_and_retention(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with store_of(kind, monkeypatch) as store:
        first = session()
        assert await store.add_session(first)
        twin = session(first.account_id, first.mower_id)
        assert not await store.add_session(twin)  # one active session per mower
        assert (await store.active_of(first.account_id, first.mower_id)) == first
        assert await store.active_of(uuid4(), first.mower_id) is None  # account scoped
        assert await store.session_of(uuid4(), first.id) is None

        changed = first.evolve(last_lat=48.1, last_lon=11.5, stale_notified=True, points=4)
        await store.put_session(changed)
        assert (await store.session_of(first.account_id, first.id)) == changed
        assert [s.id for s in await store.active_sessions()] == [first.id]

        a = link_of(first, "a" * 64)
        b = link_of(first, "b" * 64, created_at=NOW + timedelta(minutes=1))
        await store.add_link(a)
        await store.add_link(b)
        assert [link.id for link in await store.links_of(first.id)] == [b.id, a.id]
        assert (await store.link_by_hash("a" * 64)) == a
        assert await store.link_by_hash("c" * 64) is None
        assert await store.revoke_link(first.account_id, a.id, NOW)
        assert not await store.revoke_link(first.account_id, a.id, NOW)  # already revoked
        assert not await store.revoke_link(uuid4(), b.id, NOW)  # other account
        await store.touch_link(b.id, NOW)
        touched = await store.link_by_hash("b" * 64)
        assert touched is not None and touched.uses == 1 and touched.last_used_at == NOW

        await store.add_photo(
            TrackingPhoto(
                id=uuid4(),
                session_id=first.id,
                account_id=first.account_id,
                taken_at=NOW,
                media_type="image/jpeg",
                data=b"\xff\xd8jpeg",
            )
        )
        photos = await store.photos_of(first.id)
        assert [p.data for p in photos] == [b"\xff\xd8jpeg"]

        assert await store.update_active(first.id, {"points": 5, "stale_notified": False})
        changed = changed.evolve(points=5, stale_notified=False)
        assert (await store.session_of(first.account_id, first.id)) == changed

        assert not await store.delete_session(first.account_id, first.id)  # still active
        ended = changed.evolve(ended_at=NOW + timedelta(hours=1), end_reason="stopped")
        await store.put_session(ended)
        # A background write never undoes the end: only an active session takes changes.
        assert not await store.update_active(first.id, {"last_lat": 1.0})
        assert not await store.update_active(uuid4(), {"last_lat": 1.0})
        assert (await store.session_of(first.account_id, first.id)) == ended
        assert await store.active_of(first.account_id, first.mower_id) is None
        assert (await store.latest_of(first.account_id, first.mower_id)) == ended
        again = session(first.account_id, first.mower_id, started_at=NOW + timedelta(hours=2))
        assert await store.add_session(again)  # allowed once the first ended
        assert [s.id for s in await store.sessions_of(first.account_id, first.mower_id, 10)] == [
            again.id,
            first.id,
        ]
        assert await store.delete_ended_before(NOW) == 0
        assert await store.delete_ended_before(NOW + timedelta(days=1)) == 1  # `first` only
        assert await store.link_by_hash("b" * 64) is None  # links and photos went with it
        assert await store.photos_of(first.id) == []
        assert not await store.delete_session(first.account_id, again.id)
        async with store.leader() as leader:
            assert leader


@needs_db
async def test_tracking_store_leader_is_exclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    async with store_of("sql", monkeypatch) as store:
        async with store.leader() as one:
            assert one
            async with store.leader() as two:
                assert not two
        async with store.leader() as again:
            assert again


@needs_db
async def test_directory_columns_exist_after_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'mower_directory'"
                    )
                )
                assert {"name", "serial"} <= {r[0] for r in rows}
        finally:
            await engine.dispose()
