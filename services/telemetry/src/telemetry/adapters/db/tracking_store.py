"""PostgreSQL store of the theft tracking mode (SQLAlchemy Core, asyncpg)."""

import dataclasses
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from telemetry.adapters.db import tables
from telemetry.domain.tracking import TrackingLink, TrackingPhoto, TrackingSession

TRACKING_LOCK_KEY = 0x4D44_5445_4C45_0002
"""Key of the session level advisory lock that elects the replica that runs the tick."""

_S = tables.tracking_sessions.c
_L = tables.tracking_links.c


def _fields(row: Any) -> dict[str, Any]:
    return {f.name: getattr(row, f.name) for f in dataclasses.fields(row)}


def _session(row: Any) -> TrackingSession:
    return TrackingSession(**row._mapping)


def _link(row: Any) -> TrackingLink:
    return TrackingLink(**row._mapping)


class SqlTrackingStore:
    """Implements :class:`~telemetry.application.tracking_ports.TrackingStore`."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def add_session(self, session: TrackingSession) -> bool:
        try:
            async with self.engine.begin() as conn:
                await conn.execute(sa.insert(tables.tracking_sessions).values(**_fields(session)))
        except IntegrityError:
            return False  # the partial unique index: this mower is already tracked
        return True

    async def put_session(self, session: TrackingSession) -> None:
        values = _fields(session)
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.update(tables.tracking_sessions).where(_S.id == session.id).values(**values)
            )

    async def update_active(self, session_id: UUID, changes: Mapping[str, Any]) -> bool:
        async with self.engine.begin() as conn:
            result = await conn.execute(
                sa.update(tables.tracking_sessions)
                .where(_S.id == session_id, _S.ended_at.is_(None))
                .values(**changes)
            )
        return result.rowcount > 0

    async def _one(self, *conditions: Any, newest: bool = False) -> TrackingSession | None:
        statement = sa.select(tables.tracking_sessions).where(*conditions)
        if newest:
            statement = statement.order_by(_S.started_at.desc())
        async with self.engine.connect() as conn:
            row = (await conn.execute(statement.limit(1))).first()
        return _session(row) if row else None

    async def session_of(self, account_id: UUID, session_id: UUID) -> TrackingSession | None:
        return await self._one(_S.account_id == account_id, _S.id == session_id)

    async def active_of(self, account_id: UUID, mower_id: UUID) -> TrackingSession | None:
        return await self._one(
            _S.account_id == account_id, _S.mower_id == mower_id, _S.ended_at.is_(None)
        )

    async def latest_of(self, account_id: UUID, mower_id: UUID) -> TrackingSession | None:
        return await self._one(_S.account_id == account_id, _S.mower_id == mower_id, newest=True)

    async def sessions_of(
        self, account_id: UUID, mower_id: UUID, limit: int
    ) -> list[TrackingSession]:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                sa.select(tables.tracking_sessions)
                .where(_S.account_id == account_id, _S.mower_id == mower_id)
                .order_by(_S.started_at.desc())
                .limit(limit)
            )
            return [_session(r) for r in result]

    async def active_sessions(self) -> list[TrackingSession]:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                sa.select(tables.tracking_sessions).where(_S.ended_at.is_(None))
            )
            return [_session(r) for r in result]

    async def delete_session(self, account_id: UUID, session_id: UUID) -> bool:
        async with self.engine.begin() as conn:
            result = await conn.execute(
                sa.delete(tables.tracking_sessions).where(
                    _S.account_id == account_id, _S.id == session_id, _S.ended_at.is_not(None)
                )
            )
        return result.rowcount > 0

    async def delete_ended_before(self, cutoff: datetime) -> int:
        async with self.engine.begin() as conn:
            result = await conn.execute(
                sa.delete(tables.tracking_sessions).where(
                    _S.ended_at.is_not(None), _S.ended_at < cutoff
                )
            )
        return result.rowcount

    async def add_link(self, link: TrackingLink) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(sa.insert(tables.tracking_links).values(**_fields(link)))

    async def links_of(self, session_id: UUID) -> list[TrackingLink]:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                sa.select(tables.tracking_links)
                .where(_L.session_id == session_id)
                .order_by(_L.created_at.desc())
            )
            return [_link(r) for r in result]

    async def link_by_hash(self, token_hash: str) -> TrackingLink | None:
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.select(tables.tracking_links).where(_L.token_hash == token_hash)
                )
            ).first()
        return _link(row) if row else None

    async def revoke_link(self, account_id: UUID, link_id: UUID, now: datetime) -> bool:
        async with self.engine.begin() as conn:
            result = await conn.execute(
                sa.update(tables.tracking_links)
                .where(_L.account_id == account_id, _L.id == link_id, _L.revoked_at.is_(None))
                .values(revoked_at=now)
            )
        return result.rowcount > 0

    async def touch_link(self, link_id: UUID, now: datetime) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.update(tables.tracking_links)
                .where(_L.id == link_id)
                .values(last_used_at=now, uses=_L.uses + 1)
            )

    async def add_photo(self, photo: TrackingPhoto) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(sa.insert(tables.tracking_photos).values(**_fields(photo)))

    async def photos_of(self, session_id: UUID) -> list[TrackingPhoto]:
        photos = tables.tracking_photos.c
        async with self.engine.connect() as conn:
            result = await conn.execute(
                sa.select(tables.tracking_photos)
                .where(photos.session_id == session_id)
                .order_by(photos.taken_at)
            )
            return [TrackingPhoto(**r._mapping) for r in result]

    @asynccontextmanager
    async def leader(self) -> AsyncIterator[bool]:
        """A session level advisory lock on its own connection for the length of the tick."""
        async with self.engine.connect() as conn:
            got = bool(
                (
                    await conn.execute(
                        sa.text("SELECT pg_try_advisory_lock(:key)").bindparams(
                            key=TRACKING_LOCK_KEY
                        )
                    )
                ).scalar()
            )
            try:
                yield got
            finally:
                if got:
                    await conn.execute(
                        sa.text("SELECT pg_advisory_unlock(:key)").bindparams(key=TRACKING_LOCK_KEY)
                    )
