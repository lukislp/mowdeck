"""In-memory tracking store (tests) with the semantics of the PostgreSQL store."""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

from telemetry.domain.tracking import TrackingLink, TrackingPhoto, TrackingSession


class MemoryTrackingStore:
    """The :class:`~telemetry.application.tracking_ports.TrackingStore` port in memory."""

    def __init__(self) -> None:
        self.sessions: dict[UUID, TrackingSession] = {}
        self.links: dict[UUID, TrackingLink] = {}
        self.photos: list[TrackingPhoto] = []
        self.leader_elsewhere = False
        """Tests set this to simulate another replica running the tick."""

    async def add_session(self, session: TrackingSession) -> bool:
        if any(s.mower_id == session.mower_id and s.active for s in self.sessions.values()):
            return False
        self.sessions[session.id] = session
        return True

    async def put_session(self, session: TrackingSession) -> None:
        self.sessions[session.id] = session

    async def update_active(self, session_id: UUID, changes: Mapping[str, Any]) -> bool:
        found = self.sessions.get(session_id)
        if found is None or not found.active:
            return False
        self.sessions[session_id] = replace(found, **changes)
        return True

    async def session_of(self, account_id: UUID, session_id: UUID) -> TrackingSession | None:
        found = self.sessions.get(session_id)
        return found if found is not None and found.account_id == account_id else None

    async def active_of(self, account_id: UUID, mower_id: UUID) -> TrackingSession | None:
        for s in self.sessions.values():
            if s.account_id == account_id and s.mower_id == mower_id and s.active:
                return s
        return None

    async def sessions_of(
        self, account_id: UUID, mower_id: UUID, limit: int
    ) -> list[TrackingSession]:
        rows = [
            s
            for s in self.sessions.values()
            if s.account_id == account_id and s.mower_id == mower_id
        ]
        rows.sort(key=lambda s: s.started_at, reverse=True)
        return rows[:limit]

    async def latest_of(self, account_id: UUID, mower_id: UUID) -> TrackingSession | None:
        rows = await self.sessions_of(account_id, mower_id, 1)
        return rows[0] if rows else None

    async def active_sessions(self) -> list[TrackingSession]:
        return [s for s in self.sessions.values() if s.active]

    async def delete_session(self, account_id: UUID, session_id: UUID) -> bool:
        found = await self.session_of(account_id, session_id)
        if found is None or found.active:
            return False
        self._drop(session_id)
        return True

    def _drop(self, session_id: UUID) -> None:
        del self.sessions[session_id]
        self.links = {k: v for k, v in self.links.items() if v.session_id != session_id}
        self.photos = [p for p in self.photos if p.session_id != session_id]

    async def delete_ended_before(self, cutoff: datetime) -> int:
        old = [
            s.id for s in self.sessions.values() if s.ended_at is not None and s.ended_at < cutoff
        ]
        for session_id in old:
            self._drop(session_id)
        return len(old)

    async def add_link(self, link: TrackingLink) -> None:
        self.links[link.id] = link

    async def links_of(self, session_id: UUID) -> list[TrackingLink]:
        rows = [link for link in self.links.values() if link.session_id == session_id]
        rows.sort(key=lambda link: link.created_at, reverse=True)
        return rows

    async def link_by_hash(self, token_hash: str) -> TrackingLink | None:
        for link in self.links.values():
            if link.token_hash == token_hash:
                return link
        return None

    async def revoke_link(self, account_id: UUID, link_id: UUID, now: datetime) -> bool:
        link = self.links.get(link_id)
        if link is None or link.account_id != account_id or link.revoked_at is not None:
            return False
        self.links[link_id] = replace(link, revoked_at=now)
        return True

    async def touch_link(self, link_id: UUID, now: datetime) -> None:
        link = self.links.get(link_id)
        if link is not None:
            self.links[link_id] = replace(link, last_used_at=now, uses=link.uses + 1)

    async def add_photo(self, photo: TrackingPhoto) -> None:
        self.photos.append(photo)

    async def photos_of(self, session_id: UUID) -> list[TrackingPhoto]:
        return sorted(
            (p for p in self.photos if p.session_id == session_id), key=lambda p: p.taken_at
        )

    @asynccontextmanager
    async def leader(self) -> AsyncIterator[bool]:
        yield not self.leader_elsewhere
